"""Compare the build result against each target branch and deploy the diff via PR or direct push."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from github import Github, GithubException, InputGitTreeElement
from github.GitCommit import GitCommit
from github.Repository import Repository

from ghfanout.builder import BuildResult, build_per_variant, variant_key
from ghfanout.config import BranchSpec, Manifest, RootConfig, load_manifest
from ghfanout.errors import DeployError

logger = logging.getLogger(__name__)

WORK_BRANCH_PREFIX = "ghfanout/update-"
COMMIT_MESSAGE = "chore: sync shared config files via ghfanout"
PR_TITLE = "chore: sync shared config files (ghfanout)"

HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE_ENTITY = 422


@dataclass(frozen=True)
class BranchDiff:
    """Diff for a single target branch.

    Attributes:
        branch: Target branch name.
        added: Relative paths that do not yet exist on the branch.
        updated: Relative paths whose content differs from the build result.
    """

    branch: str
    added: tuple[str, ...]
    updated: tuple[str, ...]

    @property
    def has_changes(self) -> bool:
        """Whether there is at least one file to add or update."""
        return bool(self.added or self.updated)


@dataclass(frozen=True)
class BranchOutcome:
    """Deploy result for a single target branch.

    Attributes:
        branch: Target branch name.
        diff: Diff against the branch at deploy time.
        dry_run: Whether this was a dry run (nothing was sent to GitHub).
        pr_url: URL of the created or reused PR, if any.
        is_pr_reused: Whether an existing open PR was reused instead of created.
        pushed_commit_sha: SHA of the directly pushed commit, if any.
    """

    branch: str
    diff: BranchDiff
    dry_run: bool
    pr_url: str | None = None
    is_pr_reused: bool = False
    pushed_commit_sha: str | None = None


def resolve_target_branches(repo: Repository, manifest: Manifest) -> list[BranchSpec]:
    """Return the manifest's branches, or the repository's default branch if omitted."""
    if manifest.branches:
        return list(manifest.branches)
    return [BranchSpec(name=repo.default_branch)]


def compute_branch_diff(repo: Repository, branch: str, build: BuildResult) -> BranchDiff:
    """Compare the build result against the current files on the target branch.

    Returns:
        The files to add or update (unchanged files are excluded).

    Raises:
        DeployError: If a build output path conflicts with an existing
            directory on the branch.
    """
    added: list[str] = []
    updated: list[str] = []
    for rel_path, content in sorted(build.files.items()):
        try:
            current = repo.get_contents(rel_path, ref=branch)
        except GithubException as exc:
            if exc.status == HTTP_NOT_FOUND:
                added.append(rel_path)
                continue
            raise
        if isinstance(current, list):
            raise DeployError(
                f"{repo.full_name}@{branch}: {rel_path} conflicts with an existing directory."
            )
        if current.decoded_content != content:
            updated.append(rel_path)
    return BranchDiff(branch=branch, added=tuple(added), updated=tuple(updated))


def _create_commit(
    repo: Repository,
    branch: str,
    diff: BranchDiff,
    build: BuildResult,
) -> GitCommit:
    """Create a commit that reflects the diff files, with the branch's tip as parent.

    Git Data API: build up in the order blob -> tree -> commit (no local git clone needed).
    """
    base_ref = repo.get_git_ref(f"heads/{branch}")
    base_commit = repo.get_git_commit(base_ref.object.sha)

    tree_elements: list[InputGitTreeElement] = []
    for rel_path in (*diff.added, *diff.updated):
        blob = repo.create_git_blob(
            base64.b64encode(build.files[rel_path]).decode("ascii"), "base64"
        )
        tree_elements.append(
            InputGitTreeElement(path=rel_path, mode="100644", type="blob", sha=blob.sha)
        )
    new_tree = repo.create_git_tree(tree_elements, base_tree=base_commit.tree)
    return repo.create_git_commit(COMMIT_MESSAGE, new_tree, [base_commit])


def _push_branch(
    repo: Repository,
    branch: str,
    diff: BranchDiff,
    build: BuildResult,
) -> str:
    """Push a single commit directly to the target branch and return the commit SHA.

    Does not force-push (fast-forward only). If another push interrupts after the
    diff was computed, the GitHub API returns 422, so let that surface as an error.
    """
    new_commit = _create_commit(repo, branch, diff, build)
    repo.get_git_ref(f"heads/{branch}").edit(new_commit.sha)
    return new_commit.sha


def _publish_branch(
    repo: Repository,
    org: str,
    branch: str,
    diff: BranchDiff,
    build: BuildResult,
) -> tuple[str, bool]:
    """Push a single commit to the work branch and create a PR (reusing an existing PR if present).

    Returns:
        Tuple of (PR URL, whether an existing open PR was reused).
    """
    new_commit = _create_commit(repo, branch, diff, build)

    work_branch = f"{WORK_BRANCH_PREFIX}{branch}"
    try:
        repo.create_git_ref(ref=f"refs/heads/{work_branch}", sha=new_commit.sha)
    except GithubException as exc:
        if exc.status != HTTP_UNPROCESSABLE_ENTITY:
            raise
        # If the work branch still exists, rebuild it with this commit
        logger.info("%s: updating existing work branch %s", repo.full_name, work_branch)
        repo.get_git_ref(f"heads/{work_branch}").edit(new_commit.sha, force=True)

    # Note: get_pulls' head must be in "owner:branch" form, otherwise it matches every PR
    for pr in repo.get_pulls(state="open", head=f"{org}:{work_branch}", base=branch):
        logger.info("%s: reusing existing PR: %s", repo.full_name, pr.html_url)
        return pr.html_url, True

    pr = repo.create_pull(
        base=branch,
        head=work_branch,
        title=PR_TITLE,
        body=_build_pr_body(diff),
    )
    return pr.html_url, False


def _build_pr_body(diff: BranchDiff) -> str:
    """Assemble the PR body listing the added / updated files."""
    lines = [
        "This PR was automatically created by ghfanout.",
        "Syncs shared config files from the config repository's base profile.",
        "",
    ]
    lines.extend(f"- `{path}` (new)" for path in diff.added)
    lines.extend(f"- `{path}` (updated)" for path in diff.updated)
    return "\n".join(lines)


def deploy_overlay(
    config_root: Path,
    root_config: RootConfig,
    overlay: str,
    gh: Github,
    *,
    dry_run: bool,
) -> list[BranchOutcome]:
    """Deploy a single overlay (= one repository) to each target branch.

    The distributed content is built with each branch's effective bases / values
    (inheriting the top level when there is no branch-specific override), and the
    distribution method follows the manifest's deploy_mode (falling back to the
    ghfanout.yaml value when unspecified).

    Args:
        config_root: Config repository root.
        root_config: Parsed ghfanout.yaml.
        overlay: Directory name under overlays/ (= target repository name).
        gh: Authenticated GitHub client.
        dry_run: If True, report diffs without creating a PR or pushing.

    Returns:
        One outcome per target branch (empty if there is nothing to distribute).

    Raises:
        DeployError: If the repository cannot be fetched or a path conflicts
            with a directory on a target branch.
    """
    manifest = load_manifest(config_root, overlay)
    builds = build_per_variant(config_root, manifest, repo=overlay, org=root_config.org)
    repo_full_name = f"{root_config.org}/{overlay}"
    deploy_mode = manifest.deploy_mode or root_config.deploy_mode

    if not any(build.files for build in builds.values()):
        logger.warning("%s: skipping because there are 0 files to distribute", repo_full_name)
        return []

    try:
        repo = gh.get_repo(repo_full_name)
    except GithubException as exc:
        raise DeployError(f"Failed to fetch repository {repo_full_name}: {exc}") from exc

    outcomes: list[BranchOutcome] = []
    for spec in resolve_target_branches(repo, manifest):
        branch = spec.name
        build = builds[variant_key(manifest, spec)]
        if not build.files:
            logger.warning(
                "%s@%s: skipping because there are 0 files to distribute", repo_full_name, branch
            )
            continue
        diff = compute_branch_diff(repo, branch, build)
        if not diff.has_changes:
            logger.info("%s@%s: no diff", repo_full_name, branch)
            outcomes.append(BranchOutcome(branch=branch, diff=diff, dry_run=dry_run))
            continue
        if dry_run:
            outcomes.append(BranchOutcome(branch=branch, diff=diff, dry_run=True))
            continue
        if deploy_mode == "push":
            pushed_commit_sha = _push_branch(repo, branch, diff, build)
            outcomes.append(
                BranchOutcome(
                    branch=branch,
                    diff=diff,
                    dry_run=False,
                    pushed_commit_sha=pushed_commit_sha,
                )
            )
            continue
        pr_url, is_pr_reused = _publish_branch(repo, root_config.org, branch, diff, build)
        outcomes.append(
            BranchOutcome(
                branch=branch,
                diff=diff,
                dry_run=False,
                pr_url=pr_url,
                is_pr_reused=is_pr_reused,
            )
        )
    return outcomes

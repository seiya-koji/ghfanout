"""Tests for the deploy module.

Mock only the GitHub API (PyGithub's Github / Repository objects), and verify
through the public function deploy_overlay that the parameters passed to the
API are correct.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from github import GithubException, UnknownObjectException

from ghfanout.config import RootConfig
from ghfanout.deploy import COMMIT_MESSAGE, PR_TITLE, deploy_overlay
from ghfanout.errors import DeployError

ROOT_CONFIG = RootConfig(org="myorg")

# The full set of files distributed as a result of building the config_repo
# fixture's overlays/user-service (bases: [java-service])
BUILT_FILES = {
    ".github/CODEOWNERS": b"* @myorg/platform\n",
    ".gitignore": b"target/\n",
    "pom.xml": b"<project/>\n",
}


def make_fake_repo(
    *,
    default_branch: str = "main",
    existing_files: dict[tuple[str, str], bytes],
) -> MagicMock:
    """Build a mock that mimics PyGithub's Repository.

    existing_files: (path, branch name) -> content of the file that currently
    exists in the target repository
    """
    repo = MagicMock(name="Repository")
    repo.full_name = "myorg/user-service"
    repo.default_branch = default_branch

    def get_contents(path: str, ref: str) -> MagicMock:
        if (path, ref) in existing_files:
            content = MagicMock(name=f"ContentFile({path}@{ref})")
            content.decoded_content = existing_files[(path, ref)]
            return content
        raise UnknownObjectException(404, {"message": "Not Found"}, None)

    repo.get_contents.side_effect = get_contents

    base_commit = MagicMock(name="GitCommit(base)")
    base_ref = MagicMock(name="GitRef(base)")
    base_ref.object.sha = "base-sha"
    repo.get_git_ref.return_value = base_ref
    repo.get_git_commit.return_value = base_commit

    repo.create_git_blob.side_effect = lambda content, _encoding: MagicMock(
        sha=f"blob-{content[:8]}"
    )
    new_commit = MagicMock(name="GitCommit(new)")
    new_commit.sha = "new-commit-sha"
    repo.create_git_commit.return_value = new_commit

    repo.get_pulls.return_value = []
    created_pr = MagicMock(name="PullRequest")
    created_pr.html_url = "https://github.com/myorg/user-service/pull/1"
    repo.create_pull.return_value = created_pr

    return repo


def make_fake_gh(repo: MagicMock) -> MagicMock:
    gh = MagicMock(name="Github")
    gh.get_repo.return_value = repo
    return gh


class TestDeployOverlayApiCalls:
    def test_commits_only_diff_files_via_git_data_api_and_creates_pr(
        self, config_repo: Path
    ) -> None:
        # CODEOWNERS: identical content (excluded) / .gitignore: content diff (updated) /
        # pom.xml: does not exist (new)
        repo = make_fake_repo(
            existing_files={
                (".github/CODEOWNERS", "main"): b"* @myorg/platform\n",
                (".gitignore", "main"): b"*.log\n",
            }
        )
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        gh.get_repo.assert_called_once_with("myorg/user-service")

        # blobs are created with base64 encoding (binary-safe)
        pom_b64 = base64.b64encode(b"<project/>\n").decode("ascii")
        gitignore_b64 = base64.b64encode(b"target/\n").decode("ascii")
        blob_calls = [c.args for c in repo.create_git_blob.call_args_list]
        assert blob_calls == [
            (pom_b64, "base64"),
            (gitignore_b64, "base64"),
        ]

        # tree elements: order is new (pom.xml) -> updated (.gitignore), with correct mode/type/sha
        tree_call = repo.create_git_tree.call_args
        identities = [element._identity for element in tree_call.args[0]]
        assert identities == [
            {
                "path": "pom.xml",
                "mode": "100644",
                "type": "blob",
                "sha": f"blob-{pom_b64[:8]}",
            },
            {
                "path": ".gitignore",
                "mode": "100644",
                "type": "blob",
                "sha": f"blob-{gitignore_b64[:8]}",
            },
        ]
        assert tree_call.kwargs["base_tree"] is repo.get_git_commit.return_value.tree

        # the commit has the base commit as its parent
        repo.get_git_ref.assert_any_call("heads/main")
        repo.get_git_commit.assert_called_once_with("base-sha")
        repo.create_git_commit.assert_called_once_with(
            COMMIT_MESSAGE,
            repo.create_git_tree.return_value,
            [repo.get_git_commit.return_value],
        )

        # work branch creation uses the full refs/heads/ specification
        repo.create_git_ref.assert_called_once_with(
            ref="refs/heads/ghfanout/update-main", sha="new-commit-sha"
        )

        # the head for existing PR search is in owner:branch format
        repo.get_pulls.assert_called_once_with(
            state="open", head="myorg:ghfanout/update-main", base="main"
        )
        pull_kwargs = repo.create_pull.call_args.kwargs
        assert pull_kwargs["base"] == "main"
        assert pull_kwargs["head"] == "ghfanout/update-main"
        assert pull_kwargs["title"] == PR_TITLE
        assert "`pom.xml` (new)" in pull_kwargs["body"]
        assert "`.gitignore` (updated)" in pull_kwargs["body"]

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.branch == "main"
        assert outcome.diff.added == ("pom.xml",)
        assert outcome.diff.updated == (".gitignore",)
        assert outcome.pr_url == "https://github.com/myorg/user-service/pull/1"
        assert outcome.is_pr_reused is False

    def test_targets_default_branch_when_branches_omitted(self, config_repo: Path) -> None:
        repo = make_fake_repo(default_branch="develop", existing_files={})
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=True)

        assert [outcome.branch for outcome in outcomes] == ["develop"]
        assert {c.kwargs["ref"] for c in repo.get_contents.call_args_list} == {"develop"}

    def test_creates_separate_pr_per_branch_when_branches_specified(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - main\n  - release-1.x\n",
            encoding="utf-8",
        )
        # main has all files present with identical content (no diff);
        # release-1.x is empty (all files new)
        repo = make_fake_repo(
            existing_files={(path, "main"): content for path, content in BUILT_FILES.items()}
        )
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        assert [outcome.branch for outcome in outcomes] == ["main", "release-1.x"]
        assert outcomes[0].diff.has_changes is False
        assert outcomes[0].pr_url is None
        assert outcomes[1].diff.added == tuple(sorted(BUILT_FILES))

        # a PR is only created for release-1.x, which had a diff
        assert repo.create_pull.call_count == 1
        pull_kwargs = repo.create_pull.call_args.kwargs
        assert pull_kwargs["base"] == "release-1.x"
        assert pull_kwargs["head"] == "ghfanout/update-release-1.x"
        repo.create_git_ref.assert_called_once_with(
            ref="refs/heads/ghfanout/update-release-1.x", sha="new-commit-sha"
        )

    def test_distributes_different_content_per_branch_with_branch_specific_bases(
        self, config_repo: Path
    ) -> None:
        # only release-1.x is swapped to the legacy profile
        legacy_dir = config_repo / "base" / "java-legacy"
        legacy_dir.mkdir()
        (legacy_dir / "pom.xml").write_bytes(b"<legacy/>\n")

        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "branches:\n  - main\n  - name: release-1.x\n    bases: [java-legacy]\n",
            encoding="utf-8",
        )
        # main has the full java-service version present with identical content
        # (no diff); release-1.x is empty
        repo = make_fake_repo(
            existing_files={(path, "main"): content for path, content in BUILT_FILES.items()}
        )
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        assert [outcome.branch for outcome in outcomes] == ["main", "release-1.x"]
        # main is identical to the top-level bases (java-service version) -> no diff
        assert outcomes[0].diff.has_changes is False
        # release-1.x gets the common + java-legacy version newly distributed
        assert outcomes[1].diff.added == (".github/CODEOWNERS", ".gitignore", "pom.xml")

        blob_contents = {c.args[0] for c in repo.create_git_blob.call_args_list}
        # pom.xml is the java-legacy version
        assert base64.b64encode(b"<legacy/>\n").decode("ascii") in blob_contents
        # .gitignore is the common version (java-legacy does not override it, so
        # it's not the java-service version)
        assert base64.b64encode(b"*.log\n").decode("ascii") in blob_contents
        assert base64.b64encode(b"target/\n").decode("ascii") not in blob_contents

        # a PR is only created for release-1.x, which had a diff
        assert repo.create_pull.call_count == 1
        assert repo.create_pull.call_args.kwargs["base"] == "release-1.x"

    def test_distributes_different_content_per_branch_with_branch_specific_values(
        self, config_repo: Path
    ) -> None:
        # only release-1.x overrides values.version, confirming the rendered
        # result changes
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.jinja").write_text(
            "<version>{{ values.version }}</version>\n", encoding="utf-8"
        )
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            'values:\n  version: "1.0"\n'
            'branches:\n  - main\n  - name: release-1.x\n    values:\n      version: "0.9"\n',
            encoding="utf-8",
        )
        # main has the full top-level values version present with identical
        # content (no diff); release-1.x is empty
        main_files = {
            ".github/CODEOWNERS": b"* @myorg/platform\n",
            ".gitignore": b"target/\n",
            "pom.xml": b"<version>1.0</version>\n",
        }
        repo = make_fake_repo(
            existing_files={(path, "main"): content for path, content in main_files.items()}
        )
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        assert [outcome.branch for outcome in outcomes] == ["main", "release-1.x"]
        # main is identical to the top-level values (version 1.0) -> no diff
        assert outcomes[0].diff.has_changes is False
        # release-1.x gets the branch-specific values (version 0.9) newly distributed
        assert outcomes[1].diff.added == (".github/CODEOWNERS", ".gitignore", "pom.xml")

        blob_contents = {c.args[0] for c in repo.create_git_blob.call_args_list}
        assert base64.b64encode(b"<version>0.9</version>\n").decode("ascii") in blob_contents
        assert base64.b64encode(b"<version>1.0</version>\n").decode("ascii") not in blob_contents

        # a PR is only created for release-1.x, which had a diff
        assert repo.create_pull.call_count == 1
        assert repo.create_pull.call_args.kwargs["base"] == "release-1.x"

    def test_skips_branch_with_zero_build_files_and_deploys_others(self, tmp_path: Path) -> None:
        # no common/, empty top-level bases -> main's build results in 0 files
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\n", encoding="utf-8")
        java_dir = tmp_path / "base" / "java-service"
        java_dir.mkdir(parents=True)
        (java_dir / "pom.xml").write_bytes(b"<project/>\n")
        overlay_dir = tmp_path / "overlays" / "user-service"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "manifest.yaml").write_text(
            "bases: []\nbranches:\n  - main\n  - name: release-1.x\n    bases: [java-service]\n",
            encoding="utf-8",
        )
        repo = make_fake_repo(existing_files={})
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(tmp_path, ROOT_CONFIG, "user-service", gh, dry_run=True)

        # main with 0 files is skipped; only release-1.x is targeted
        assert [outcome.branch for outcome in outcomes] == ["release-1.x"]
        assert outcomes[0].diff.added == ("pom.xml",)

    def test_dry_run_does_not_call_any_write_apis(self, config_repo: Path) -> None:
        repo = make_fake_repo(existing_files={})
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=True)

        assert outcomes[0].diff.added == tuple(sorted(BUILT_FILES))
        assert outcomes[0].pr_url is None
        repo.create_git_blob.assert_not_called()
        repo.create_git_tree.assert_not_called()
        repo.create_git_commit.assert_not_called()
        repo.create_git_ref.assert_not_called()
        repo.create_pull.assert_not_called()

    def test_does_not_call_write_apis_when_no_branch_has_diff(self, config_repo: Path) -> None:
        repo = make_fake_repo(
            existing_files={(path, "main"): content for path, content in BUILT_FILES.items()}
        )
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        assert outcomes[0].diff.has_changes is False
        repo.create_git_blob.assert_not_called()
        repo.create_pull.assert_not_called()


class TestDeployOverlayDirectPushMode:
    PUSH_ROOT_CONFIG = RootConfig(org="myorg", deploy_mode="push")

    def test_push_mode_updates_target_branch_ref_directly_without_creating_pr(
        self, config_repo: Path
    ) -> None:
        repo = make_fake_repo(existing_files={})
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(
            config_repo, self.PUSH_ROOT_CONFIG, "user-service", gh, dry_run=False
        )

        # directly update the target branch's ref with the new commit
        # (no force = fast-forward only)
        repo.get_git_ref.return_value.edit.assert_called_once_with("new-commit-sha")
        # creates neither a work branch nor a PR
        repo.create_git_ref.assert_not_called()
        repo.get_pulls.assert_not_called()
        repo.create_pull.assert_not_called()

        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.pushed_commit_sha == "new-commit-sha"
        assert outcome.pr_url is None
        assert outcome.is_pr_reused is False

    def test_manifest_deploy_mode_overrides_root_setting(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\ndeploy_mode: push\n", encoding="utf-8"
        )
        repo = make_fake_repo(existing_files={})
        gh = make_fake_gh(repo)

        # even though root stays at the default (pr), the manifest's push takes priority
        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        repo.get_git_ref.return_value.edit.assert_called_once_with("new-commit-sha")
        repo.create_pull.assert_not_called()
        assert outcomes[0].pushed_commit_sha == "new-commit-sha"

    def test_push_mode_dry_run_does_not_call_any_write_apis(self, config_repo: Path) -> None:
        repo = make_fake_repo(existing_files={})
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(
            config_repo, self.PUSH_ROOT_CONFIG, "user-service", gh, dry_run=True
        )

        assert outcomes[0].pushed_commit_sha is None
        repo.create_git_blob.assert_not_called()
        repo.create_git_tree.assert_not_called()
        repo.create_git_commit.assert_not_called()
        repo.get_git_ref.return_value.edit.assert_not_called()

    def test_push_mode_does_not_write_to_branch_without_diff(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - main\n  - release-1.x\n",
            encoding="utf-8",
        )
        # main has all files present with identical content (no diff);
        # release-1.x is empty (all files new)
        repo = make_fake_repo(
            existing_files={(path, "main"): content for path, content in BUILT_FILES.items()}
        )
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(
            config_repo, self.PUSH_ROOT_CONFIG, "user-service", gh, dry_run=False
        )

        assert outcomes[0].pushed_commit_sha is None
        assert outcomes[1].pushed_commit_sha == "new-commit-sha"
        # the ref is fetched/updated only for release-1.x, which had a diff
        repo.get_git_ref.return_value.edit.assert_called_once_with("new-commit-sha")
        assert repo.get_git_ref.call_args_list == [
            call("heads/release-1.x"),
            call("heads/release-1.x"),
        ]


class TestDeployOverlayErrorsAndReuse:
    def test_force_updates_work_branch_on_422_when_it_already_exists(
        self, config_repo: Path
    ) -> None:
        repo = make_fake_repo(existing_files={})
        base_ref = repo.get_git_ref.return_value
        work_ref = MagicMock(name="GitRef(work)")

        def get_git_ref(ref: str) -> MagicMock:
            return work_ref if ref == "heads/ghfanout/update-main" else base_ref

        repo.get_git_ref.side_effect = get_git_ref
        repo.create_git_ref.side_effect = GithubException(
            422, {"message": "Reference already exists"}, None
        )
        gh = make_fake_gh(repo)

        deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        work_ref.edit.assert_called_once_with("new-commit-sha", force=True)

    def test_reuses_existing_open_pr_instead_of_creating_new_one(self, config_repo: Path) -> None:
        repo = make_fake_repo(existing_files={})
        existing_pr = MagicMock(name="PullRequest(existing)")
        existing_pr.html_url = "https://github.com/myorg/user-service/pull/7"
        repo.get_pulls.return_value = [existing_pr]
        gh = make_fake_gh(repo)

        outcomes = deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        repo.create_pull.assert_not_called()
        assert outcomes[0].pr_url == "https://github.com/myorg/user-service/pull/7"
        assert outcomes[0].is_pr_reused is True

    def test_converts_repository_fetch_failure_to_deploy_error(self, config_repo: Path) -> None:
        gh = MagicMock(name="Github")
        gh.get_repo.side_effect = GithubException(404, {"message": "Not Found"}, None)

        with pytest.raises(DeployError, match="myorg/user-service"):
            deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=True)

    def test_does_not_swallow_non_404_get_contents_errors(self, config_repo: Path) -> None:
        repo = make_fake_repo(existing_files={})
        repo.get_contents.side_effect = GithubException(500, {"message": "boom"}, None)
        gh = make_fake_gh(repo)

        with pytest.raises(GithubException):
            deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=True)

    def test_raises_deploy_error_when_path_conflicts_with_directory(
        self, config_repo: Path
    ) -> None:
        repo = make_fake_repo(existing_files={})
        # get_contents returns a list when the path is a directory on the branch
        repo.get_contents.side_effect = None
        repo.get_contents.return_value = [MagicMock(name="ContentFile(child)")]
        gh = make_fake_gh(repo)

        with pytest.raises(DeployError, match="conflicts with an existing directory"):
            deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=True)

    def test_does_not_swallow_non_422_create_git_ref_errors(self, config_repo: Path) -> None:
        repo = make_fake_repo(existing_files={})
        repo.create_git_ref.side_effect = GithubException(403, {"message": "Forbidden"}, None)
        gh = make_fake_gh(repo)

        with pytest.raises(GithubException):
            deploy_overlay(config_repo, ROOT_CONFIG, "user-service", gh, dry_run=False)

        repo.create_pull.assert_not_called()

    def test_does_not_access_repository_when_zero_files_to_distribute(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\n", encoding="utf-8")
        overlay_dir = tmp_path / "overlays" / "empty-service"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "manifest.yaml").write_text("bases: []\n", encoding="utf-8")
        gh = MagicMock(name="Github")

        outcomes = deploy_overlay(tmp_path, ROOT_CONFIG, "empty-service", gh, dry_run=False)

        assert outcomes == []
        gh.get_repo.assert_not_called()

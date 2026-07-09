"""Typer-based CLI entry point."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Annotated

import typer
from github import GithubException

from ghfanout.builder import (
    BuildResult,
    FileProvenance,
    build_per_variant,
    variant_key,
    write_build_output,
)
from ghfanout.config import (
    ROOT_CONFIG_FILENAME,
    RootConfig,
    find_config_root,
    list_overlays,
    load_manifest,
    load_root_config,
)
from ghfanout.deploy import BranchOutcome, deploy_overlay
from ghfanout.errors import ConfigError, GhfanoutError
from ghfanout.github_client import create_github_client
from ghfanout.scaffold import EXAMPLE_OVERLAY, init_config_repo

app = typer.Typer(
    no_args_is_help=True,
    help=(
        "A tool for distributing shared configuration files to multiple GitHub "
        "repositories via PR or direct push"
    ),
)

logger = logging.getLogger(__name__)

_JSON_OPTION_HELP = (
    "Print a machine-readable JSON report to stdout instead of the human-readable output"
)


def _version_callback(value: bool | None) -> None:
    if value:
        typer.echo(f"ghfanout {_pkg_version('ghfanout')}")
        raise typer.Exit()


@app.callback()
def main(  # noqa: D103 -- help is set via typer.Typer(help=...) (docstring would leak into CLI help)
    ctx: typer.Context,
    config_dir: Annotated[
        Path | None,
        typer.Option("--config-dir", "-C", help="Explicitly specify the config repository root"),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Output verbose (DEBUG) logs")
    ] = False,
    _version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = None,
) -> None:
    # force=True: reattach the handler to the current stderr on every CLI invocation
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        force=True,
    )
    ctx.obj = config_dir


def _resolve_config_root(config_dir: Path | None) -> Path:
    """When -C is given, validate and use it; otherwise search upward from the current directory."""
    if config_dir is not None:
        if not (config_dir / ROOT_CONFIG_FILENAME).is_file():
            raise ConfigError(
                f"{ROOT_CONFIG_FILENAME} was not found in {config_dir} specified via --config-dir."
            )
        return config_dir.resolve()
    return find_config_root(Path.cwd())


def _fail(message: str) -> typer.Exit:
    """Print an error message in red to stderr and return a typer.Exit(1) to raise."""
    typer.secho(f"Error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code=1)


def _plural(count: int, singular: str, plural: str) -> str:
    """Return the count followed by the grammatically matching noun form."""
    return f"{count} {singular if count == 1 else plural}"


@app.command()
def init(
    path: Annotated[
        Path | None,
        typer.Argument(
            help="Target directory to generate into (defaults to the current directory)"
        ),
    ] = None,
    org: Annotated[
        str | None,
        typer.Option("--org", help="GitHub org name to fill into ghfanout.yaml"),
    ] = None,
) -> None:
    """Generate a config repository scaffold (with a working example).

    Creates ghfanout.yaml, a sample base profile, and an example overlay.
    Existing files are never overwritten, and nothing is sent to GitHub.
    """
    target = (path or Path.cwd()).resolve()
    try:
        created = init_config_repo(target, org)
    except GhfanoutError as exc:
        raise _fail(str(exc)) from exc

    typer.echo(f"Generated {len(created)} file(s) in {target}")
    typer.secho("Next steps:", fg=typer.colors.GREEN)
    typer.echo(f"  1. Change the org in {ROOT_CONFIG_FILENAME} to your actual GitHub org")
    typer.echo(f"  2. Run ghfanout build {EXAMPLE_OVERLAY} to check the build output")
    typer.echo(f"  3. Set GHFANOUT_TOKEN and run ghfanout deploy {EXAMPLE_OVERLAY} --dry-run")


def _resolve_build_targets(
    config_root: Path, root_config: RootConfig, overlay: str | None, output: Path | None
) -> dict[str, Path]:
    """Resolve each target overlay to its output directory for the build command."""
    # A relative output_dir is resolved against the config repository root
    # (an absolute path is used as-is)
    default_base = config_root / root_config.output_dir
    if overlay is not None:
        return {overlay: output if output is not None else default_base / overlay}
    base_dir = output if output is not None else default_base
    return {name: base_dir / name for name in list_overlays(config_root)}


@dataclass(frozen=True)
class _BuiltVariant:
    """A build variant written to disk.

    Attributes:
        branch: Branch name, or None when the build is branch-independent.
        output_dir: Directory the variant was written to.
        result: The build result (files and provenance).
    """

    branch: str | None
    output_dir: Path
    result: BuildResult


def _build_one(
    config_root: Path, root_config: RootConfig, overlay: str, output_dir: Path
) -> list[_BuiltVariant]:
    """Build a single overlay, write the output to output_dir, and return the variants."""
    if output_dir.exists():
        shutil.rmtree(output_dir)
    # org is needed to resolve the built-in {{ org }} variable in templates (*.jinja)
    manifest = load_manifest(config_root, overlay)
    builds = build_per_variant(config_root, manifest, repo=overlay, org=root_config.org)

    if not manifest.has_branch_specific_build:
        # Without overrides there is only one variant (identical content for all branches)
        result = next(iter(builds.values()))
        write_build_output(result, output_dir)
        return [_BuiltVariant(branch=None, output_dir=output_dir, result=result)]

    variants: list[_BuiltVariant] = []
    for spec in manifest.branches:
        branch_dir = output_dir / spec.name
        result = builds[variant_key(manifest, spec)]
        write_build_output(result, branch_dir)
        variants.append(_BuiltVariant(branch=spec.name, output_dir=branch_dir, result=result))
    return variants


def _transform_counts(provenance: dict[str, FileProvenance]) -> str:
    """Summarize transformations as ': 1 rendered, 2 remapped, 1 override', or ''."""
    counts = (
        (sum(1 for prov in provenance.values() if prov.rendered), "rendered"),
        (sum(1 for prov in provenance.values() if prov.remapped_from is not None), "remapped"),
        (sum(1 for prov in provenance.values() if prov.overrides is not None), "override"),
    )
    parts = [f"{count} {label}" for count, label in counts if count]
    return f": {', '.join(parts)}" if parts else ""


def _provenance_notes(prov: FileProvenance) -> str:
    """Format per-file notes as ' (overrides common, rendered, remapped)', or ''."""
    notes: list[str] = []
    if prov.overrides is not None:
        notes.append(f"overrides {prov.overrides}")
    if prov.rendered:
        notes.append("rendered")
    if prov.remapped_from is not None:
        notes.append("remapped")
    return f" ({', '.join(notes)})" if notes else ""


def _echo_variant(overlay: str, variant: _BuiltVariant, *, detail: bool) -> None:
    """Display a variant's one-line summary, plus a per-file listing with --detail."""
    heading = overlay if variant.branch is None else f"{overlay}@{variant.branch}"
    provenance = variant.result.provenance
    counts = "" if detail else _transform_counts(provenance)
    files = _plural(len(provenance), "file", "files")
    typer.echo(f"{heading} -> {variant.output_dir} ({files}{counts})")
    if not detail:
        return
    width = max((len(path) for path in provenance), default=0)
    for path in sorted(provenance):
        prov = provenance[path]
        typer.echo(f"  {path:<{width}}  from {prov.origin}{_provenance_notes(prov)}")


def _echo_summary_rows(rows: list[tuple[str, list[str], str]]) -> None:
    """Display aligned '  N category  name, ...' rows, omitting empty categories."""
    visible = [(f"{len(names)} {label}", names, color) for label, names, color in rows if names]
    width = max((len(head) for head, _names, _color in visible), default=0)
    for head, names, color in visible:
        typer.secho(f"  {head:<{width}}  {', '.join(names)}", fg=color)


def _echo_build_summary(built: list[str], failed: list[str]) -> None:
    """Display the trailing build summary across every targeted overlay."""
    typer.echo()
    typer.echo("Summary")
    _echo_summary_rows([("built", built, typer.colors.GREEN), ("failed", failed, typer.colors.RED)])


def _build_report(built: dict[str, list[_BuiltVariant]], failed: list[str]) -> dict[str, object]:
    """Assemble the machine-readable report for build --json."""
    overlays = [
        {
            "name": name,
            "variants": [
                {
                    "branch": variant.branch,
                    "output_dir": str(variant.output_dir),
                    "files": [
                        {
                            "path": path,
                            "from": prov.origin,
                            "overrides": prov.overrides,
                            "rendered": prov.rendered,
                            "remapped_from": prov.remapped_from,
                        }
                        for path, prov in sorted(variant.result.provenance.items())
                    ],
                }
                for variant in variants
            ],
        }
        for name, variants in built.items()
    ]
    return {
        "command": "build",
        "overlays": overlays,
        "summary": {"built": list(built), "failed": failed},
    }


@app.command()
def build(
    ctx: typer.Context,
    overlay: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Directory name under overlays/ (= target repository name; "
                "if omitted, all repositories)"
            )
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help=(
                "Output directory (default: <config root>/<output_dir>/<overlay>/. "
                "If overlay is omitted, each repository is written to <--output>/<overlay>/)"
            ),
        ),
    ] = None,
    detail: Annotated[
        bool,
        typer.Option(
            "--detail",
            help="List every distributed file with its origin profile and transformations",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_OPTION_HELP)] = False,
) -> None:
    """Build (compose) base and overlay locally, with no git operations.

    If overlay is omitted, all repositories under overlays/ are built.
    If there is a branch-specific override for bases or values, each branch
    is written to <output>/<branch name>/.
    """
    if detail and json_output:
        raise _fail("--detail cannot be combined with --json (the JSON report is always detailed).")
    try:
        config_root = _resolve_config_root(ctx.obj)
        root_config = load_root_config(config_root)
        targets = _resolve_build_targets(config_root, root_config, overlay, output)
    except GhfanoutError as exc:
        raise _fail(str(exc)) from exc

    if not targets:
        if json_output:
            typer.echo(json.dumps(_build_report({}, []), indent=2))
        else:
            typer.echo("No build targets found under overlays/")
        return

    built: dict[str, list[_BuiltVariant]] = {}
    failed: list[str] = []
    for name, target_dir in targets.items():
        try:
            variants = _build_one(config_root, root_config, name, target_dir)
        except GhfanoutError as exc:
            # When overlay is omitted (build all), don't stop on the first failure;
            # try all and exit non-zero at the end
            logger.error("%s: %s", name, exc)
            failed.append(name)
            continue
        built[name] = variants
        if not json_output:
            for variant in variants:
                _echo_variant(name, variant, detail=detail)

    if json_output:
        typer.echo(json.dumps(_build_report(built, failed), indent=2))
    else:
        _echo_build_summary(list(built), failed)

    if failed:
        raise _fail(f"Build failed for {len(failed)} repository(ies): {', '.join(failed)}")


def _report_outcome(target: str, outcome: BranchOutcome) -> None:
    """Display the deploy result for a single target branch."""
    if not outcome.diff.has_changes:
        typer.echo(f"{target}: no changes")
        return

    prefix = "[dry-run] " if outcome.dry_run else ""
    typer.echo(f"{prefix}{target}:")
    for path in outcome.diff.added:
        typer.echo(f"  + {path} (new)")
    for path in outcome.diff.updated:
        typer.echo(f"  ~ {path} (updated)")
    if outcome.pr_url is not None:
        label = "Updated existing PR" if outcome.is_pr_reused else "Created PR"
        typer.echo(f"  => {label}: {outcome.pr_url}")
    if outcome.pushed_commit_sha is not None:
        typer.echo(f"  => Pushed directly: {outcome.pushed_commit_sha}")


def _report_outcomes(repo_full_name: str, outcomes: list[BranchOutcome]) -> None:
    """Display the deploy results for every target branch of a repository."""
    for outcome in outcomes:
        _report_outcome(f"{repo_full_name}@{outcome.branch}", outcome)


def _classify_outcome(outcome: BranchOutcome) -> str:
    """Classify a branch outcome into a summary category.

    A branch with changes outside a dry run always carries either a pushed
    commit SHA or a PR URL (deploy_overlay guarantees it), so the PR check is
    the final fallback.
    """
    if not outcome.diff.has_changes:
        return "no_change"
    if outcome.dry_run:
        return "would_change"
    if outcome.pushed_commit_sha is not None:
        return "pushed"
    return "reused" if outcome.is_pr_reused else "created"


def _classify_repository(outcomes: list[BranchOutcome]) -> str:
    """Classify a repository by its most significant branch outcome.

    A repository with no outcomes (nothing to distribute) counts as no_change.
    """
    statuses = {_classify_outcome(outcome) for outcome in outcomes}
    for category in ("created", "pushed", "reused", "would_change"):
        if category in statuses:
            return category
    return "no_change"


def _categorize_repositories(
    deployed: dict[str, list[BranchOutcome]], failed: list[str]
) -> dict[str, list[str]]:
    """Group repository names into summary categories, preserving deploy order."""
    categories: dict[str, list[str]] = {
        "created": [],
        "pushed": [],
        "reused": [],
        "would_change": [],
        "no_change": [],
        "failed": list(failed),
    }
    for name, outcomes in deployed.items():
        categories[_classify_repository(outcomes)].append(name)
    return categories


# Deploy summary rows: (category key, human-readable label, color), in display order
_DEPLOY_SUMMARY_ROWS = (
    ("created", "created", typer.colors.GREEN),
    ("pushed", "pushed", typer.colors.GREEN),
    ("reused", "reused", typer.colors.CYAN),
    ("would_change", "would-change", typer.colors.GREEN),
    ("no_change", "no-change", typer.colors.BRIGHT_BLACK),
    ("failed", "failed", typer.colors.RED),
)


def _echo_deploy_summary(
    deployed: dict[str, list[BranchOutcome]], failed: list[str], *, dry_run: bool
) -> None:
    """Display the trailing deploy summary across every targeted repository."""
    categories = _categorize_repositories(deployed, failed)
    typer.echo()
    typer.echo("Summary (dry-run)" if dry_run else "Summary")
    typer.echo(f"  {_plural(len(deployed) + len(failed), 'repository', 'repositories')}")
    _echo_summary_rows(
        [(label, categories[key], color) for key, label, color in _DEPLOY_SUMMARY_ROWS]
    )


def _deploy_report(
    org: str,
    deployed: dict[str, list[BranchOutcome]],
    failed: list[str],
    *,
    dry_run: bool,
) -> dict[str, object]:
    """Assemble the machine-readable report for deploy --json."""
    repositories = [
        {
            "name": f"{org}/{name}",
            "branches": [
                {
                    "branch": outcome.branch,
                    "status": _classify_outcome(outcome),
                    "added": list(outcome.diff.added),
                    "updated": list(outcome.diff.updated),
                    "pr_url": outcome.pr_url,
                    "commit_sha": outcome.pushed_commit_sha,
                }
                for outcome in outcomes
            ],
        }
        for name, outcomes in deployed.items()
    ]
    return {
        "command": "deploy",
        "dry_run": dry_run,
        "repositories": repositories,
        "summary": _categorize_repositories(deployed, failed),
    }


@app.command()
def deploy(
    ctx: typer.Context,
    overlay: Annotated[
        str | None,
        typer.Argument(help="Directory name under overlays/ (omit when using --all)"),
    ] = None,
    deploy_all: Annotated[
        bool, typer.Option("--all", help="Deploy to all repositories under overlays/")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the diff only, without creating a PR or pushing")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_OPTION_HELP)] = False,
) -> None:
    """Compare the build output against each branch of the target repo and open a PR if it differs.

    If deploy_mode in the config is push, push directly to the target branch instead.
    """
    if (overlay is None) == (not deploy_all):
        raise _fail("Specify either an overlay name or --all.")

    try:
        config_root = _resolve_config_root(ctx.obj)
        root_config = load_root_config(config_root)
        gh = create_github_client(root_config)
        targets = list_overlays(config_root) if deploy_all else [overlay or ""]
    except GhfanoutError as exc:
        raise _fail(str(exc)) from exc

    deployed: dict[str, list[BranchOutcome]] = {}
    failed: list[str] = []
    for name in targets:
        try:
            outcomes = deploy_overlay(config_root, root_config, name, gh, dry_run=dry_run)
        except (GhfanoutError, GithubException) as exc:
            # With --all, don't stop on the first failure; try all repositories
            # and exit non-zero at the end
            logger.error("%s/%s: %s", root_config.org, name, exc)
            failed.append(name)
            continue
        deployed[name] = outcomes
        if not json_output:
            _report_outcomes(f"{root_config.org}/{name}", outcomes)

    if json_output:
        report = _deploy_report(root_config.org, deployed, failed, dry_run=dry_run)
        typer.echo(json.dumps(report, indent=2))
    else:
        _echo_deploy_summary(deployed, failed, dry_run=dry_run)

    if failed:
        raise _fail(f"Failed for {len(failed)} repository(ies): {', '.join(failed)}")

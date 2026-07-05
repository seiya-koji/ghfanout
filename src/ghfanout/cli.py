"""Typer-based CLI entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from github import GithubException

from ghfanout.builder import build_per_variant, variant_key, write_build_output
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


def _build_one(config_root: Path, root_config: RootConfig, overlay: str, output_dir: Path) -> None:
    """Build a single overlay and write the output to output_dir."""
    # org is needed to resolve the built-in {{ org }} variable in templates (*.tmpl)
    manifest = load_manifest(config_root, overlay)
    builds = build_per_variant(config_root, manifest, repo=overlay, org=root_config.org)

    if not manifest.has_branch_specific_build:
        # Without overrides there is only one variant (identical content for all branches)
        written = write_build_output(next(iter(builds.values())), output_dir)
        typer.echo(f"{overlay}: wrote {len(written)} file(s) to {output_dir}")
        return

    for spec in manifest.branches:
        branch_dir = output_dir / spec.name
        written = write_build_output(builds[variant_key(manifest, spec)], branch_dir)
        typer.echo(f"{overlay}@{spec.name}: wrote {len(written)} file(s) to {branch_dir}")


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
) -> None:
    """Build (compose) base and overlay locally, with no git operations.

    If overlay is omitted, all repositories under overlays/ are built.
    If there is a branch-specific override for bases or values, each branch
    is written to <output>/<branch name>/.
    """
    try:
        config_root = _resolve_config_root(ctx.obj)
        root_config = load_root_config(config_root)
        # A relative output_dir is resolved against the config repository root
        # (an absolute path is used as-is)
        default_base = config_root / root_config.output_dir
        if overlay is not None:
            targets = {overlay: output if output is not None else default_base / overlay}
        else:
            base_dir = output if output is not None else default_base
            targets = {name: base_dir / name for name in list_overlays(config_root)}
    except GhfanoutError as exc:
        raise _fail(str(exc)) from exc

    if not targets:
        typer.echo("No build targets found under overlays/")
        return

    failed: list[str] = []
    for name, target_dir in targets.items():
        try:
            _build_one(config_root, root_config, name, target_dir)
        except GhfanoutError as exc:
            # When overlay is omitted (build all), don't stop on the first failure;
            # try all and exit non-zero at the end
            logger.error("%s: %s", name, exc)
            failed.append(name)

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

    failed: list[str] = []
    for name in targets:
        try:
            outcomes = deploy_overlay(config_root, root_config, name, gh, dry_run=dry_run)
            _report_outcomes(f"{root_config.org}/{name}", outcomes)
        except (GhfanoutError, GithubException) as exc:
            # With --all, don't stop on the first failure; try all repositories
            # and exit non-zero at the end
            logger.error("%s/%s: %s", root_config.org, name, exc)
            failed.append(name)

    if failed:
        raise _fail(f"Failed for {len(failed)} repository(ies): {', '.join(failed)}")

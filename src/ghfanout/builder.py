"""Compose base profiles into the set of files to distribute."""

from __future__ import annotations

import logging
from collections.abc import Hashable
from dataclasses import dataclass, replace
from pathlib import Path

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from pathspec import GitIgnoreSpec

from ghfanout.config import IGNORE_FILENAME, BranchSpec, Manifest
from ghfanout.errors import BuildError

logger = logging.getLogger(__name__)

BASE_DIR_NAME = "base"
COMMON_PROFILE = "common"

# Only files with this suffix are subject to variable expansion; the suffix is
# stripped when distributed. Because this is opt-in, ordinary files containing
# GitHub Actions' ${{ }} syntax etc. are distributed unmodified.
TEMPLATE_SUFFIX = ".tmpl"

# keep_trailing_newline: preserve the trailing newline (dropped by default,
# which would produce needless diffs at the distribution destination).
# trim_blocks / lstrip_blocks: don't leave block lines such as {% if %} as
# blank lines in the output.
# StrictUndefined: always raise an error for undefined variables, including
# when used in output or truthiness checks.
# autoescape is unnecessary (we're generating config files, not HTML, and
# escaping would corrupt the content).
_TEMPLATE_ENV = Environment(  # noqa: S701
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class BuildResult:
    """Set of files produced by composing base profiles.

    Attributes:
        files: Mapping of POSIX-style relative path -> file content.
    """

    files: dict[str, bytes]


def _render_template(
    rel_path: str, content: bytes, *, repo: str, org: str, values: dict[str, object]
) -> bytes:
    """Render a single *.tmpl file. Failures are converted into BuildError."""
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(
            f"{rel_path}: failed to decode template as UTF-8 (do not use the"
            f" {TEMPLATE_SUFFIX} suffix on binary files)."
        ) from exc
    try:
        rendered = _TEMPLATE_ENV.from_string(source).render(values=values, repo=repo, org=org)
    except TemplateSyntaxError as exc:
        raise BuildError(
            f"{rel_path}:{exc.lineno}: invalid template syntax: {exc.message}"
        ) from exc
    except UndefinedError as exc:
        raise BuildError(f"{rel_path}: reference to an undefined variable: {exc.message}") from exc
    return rendered.encode("utf-8")


def _render_templates(
    files: dict[str, bytes], *, repo: str, org: str, values: dict[str, object]
) -> dict[str, bytes]:
    """Render *.tmpl files in the build result and replace them with the extension-stripped name."""
    result: dict[str, bytes] = {}
    for rel_path, content in files.items():
        target = rel_path[: -len(TEMPLATE_SUFFIX)]
        # If the filename is exactly ".tmpl", treat it as a hidden file, not a template.
        if not rel_path.endswith(TEMPLATE_SUFFIX) or not target or target.endswith("/"):
            result[rel_path] = content
            continue
        if target in files:
            raise BuildError(f"Both {target} and {rel_path} exist. Please keep only one of them.")
        result[target] = _render_template(rel_path, content, repo=repo, org=org, values=values)
    return result


def _load_ignore_spec(config_root: Path) -> GitIgnoreSpec:
    """Load the .ghfanoutignore at the config repository root.

    Returns an empty spec (matching nothing) when the file is absent. Patterns
    use .gitignore syntax and are matched against each file's path relative to
    its profile directory (i.e. the distribution path, before the .tmpl suffix
    is stripped).
    """
    ignore_path = config_root / IGNORE_FILENAME
    if not ignore_path.is_file():
        return GitIgnoreSpec.from_lines([])
    lines = ignore_path.read_text(encoding="utf-8").splitlines()
    return GitIgnoreSpec.from_lines(lines)


def build_overlay_files(
    config_root: Path, manifest: Manifest, *, repo: str, org: str
) -> BuildResult:
    """Compose common/ with the profiles listed in the manifest's bases:.

    Precedence is lowest for common, and increases for profiles listed later
    in bases: (the same relative path is fully overwritten by the later one,
    and an info log records the override). Files matched by the config
    repository's .ghfanoutignore are excluded before composition. After
    composition, *.tmpl files are rendered and lose their extension.

    Args:
        config_root: Config repository root.
        manifest: Manifest whose bases / values are already effective.
        repo: Fills the built-in {{ repo }} template variable.
        org: Fills the built-in {{ org }} template variable.

    Returns:
        The composed and rendered set of files.

    Raises:
        BuildError: If a profile is missing or a template fails to render.
    """
    ignore_spec = _load_ignore_spec(config_root)

    # Process each profile only once even if specified multiple times (order preserved)
    profiles = list(dict.fromkeys([COMMON_PROFILE, *manifest.bases]))

    files: dict[str, bytes] = {}
    origins: dict[str, str] = {}
    for profile in profiles:
        profile_dir = config_root / BASE_DIR_NAME / profile
        if not profile_dir.is_dir():
            if profile == COMMON_PROFILE:
                # common/ is distributed unconditionally, but it's fine if it doesn't exist
                logger.debug("base/%s does not exist, skipping", COMMON_PROFILE)
                continue
            raise BuildError(
                f"Profile base/{profile} specified in bases was not found under {config_root}."
            )

        for file_path in sorted(p for p in profile_dir.rglob("*") if p.is_file()):
            rel_path = file_path.relative_to(profile_dir).as_posix()
            if ignore_spec.match_file(rel_path):
                logger.debug("%s: excluded by %s", rel_path, IGNORE_FILENAME)
                continue
            if rel_path in files:
                logger.info(
                    "%s: overriding base/%s with base/%s",
                    rel_path,
                    origins[rel_path],
                    profile,
                )
            files[rel_path] = file_path.read_bytes()
            origins[rel_path] = profile

    return BuildResult(files=_render_templates(files, repo=repo, org=org, values=manifest.values))


# Key identifying a unique combination of build inputs (bases and values) effective for a branch.
BuildVariantKey = tuple[tuple[str, ...], Hashable]


def _freeze(obj: object) -> Hashable:
    """Convert a value with nested dicts/lists into a hashable form usable as a dict key."""
    if isinstance(obj, dict):
        # The nested parts of values don't validate key types, so use an order-independent frozenset
        return frozenset((_freeze(key), _freeze(value)) for key, value in obj.items())
    if isinstance(obj, list):
        return tuple(_freeze(item) for item in obj)
    return obj


def variant_key(manifest: Manifest, spec: BranchSpec) -> BuildVariantKey:
    """Return a cache key identifying the (bases, values) effective for spec."""
    return (manifest.bases_for(spec), _freeze(manifest.values_for(spec)))


def build_per_variant(
    config_root: Path, manifest: Manifest, *, repo: str, org: str
) -> dict[BuildVariantKey, BuildResult]:
    """Build each unique combination of effective (bases, values) per branch exactly once.

    When branches is omitted (default branch only), build just the one
    top-level combination.

    Returns:
        Mapping of variant_key() -> build result.
    """
    # The dummy spec has both bases / values as None (= inherit top-level), so
    # it produces the same key as the BranchSpec(name=<branch name>) that
    # deploy uses for the default branch.
    specs = manifest.branches or (BranchSpec(name=""),)
    builds: dict[BuildVariantKey, BuildResult] = {}
    for spec in specs:
        key = variant_key(manifest, spec)
        if key not in builds:
            effective = replace(
                manifest, bases=manifest.bases_for(spec), values=manifest.values_for(spec)
            )
            builds[key] = build_overlay_files(config_root, effective, repo=repo, org=org)
    return builds


def write_build_output(result: BuildResult, output_dir: Path) -> list[Path]:
    """Write the build result to a local directory.

    Existing files are overwritten, but unrelated files left in the output
    directory are not deleted.

    Returns:
        The written file paths, sorted by relative path.
    """
    written: list[Path] = []
    for rel_path, content in sorted(result.files.items()):
        dest = output_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        written.append(dest)
    return written

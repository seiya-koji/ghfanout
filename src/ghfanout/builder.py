"""Compose base profiles into the set of files to distribute."""

from __future__ import annotations

import logging
from collections.abc import Hashable
from dataclasses import dataclass, replace
from pathlib import Path

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, UndefinedError
from pathspec import GitIgnoreSpec

from ghfanout.config import IGNORE_FILENAME, BranchSpec, Manifest, is_valid_remap_path
from ghfanout.errors import BuildError

logger = logging.getLogger(__name__)

BASE_DIR_NAME = "base"
COMMON_PROFILE = "common"

# Only files with this suffix are subject to variable expansion; the suffix is
# stripped when distributed. Because this is opt-in, ordinary files containing
# GitHub Actions' ${{ }} syntax etc. are distributed unmodified.
TEMPLATE_SUFFIX = ".jinja"

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
        unmatched_path_sources: Manifest paths: sources that matched no file in
            this build. Not an error by itself — build_per_variant judges
            across variants and rejects sources that match nowhere at all.
    """

    files: dict[str, bytes]
    unmatched_path_sources: frozenset[str] = frozenset()


def _render_template(
    rel_path: str, content: bytes, *, repo: str, org: str, values: dict[str, object]
) -> bytes:
    """Render a single *.jinja file. Failures are converted into BuildError."""
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
    """Render *.jinja files and replace them with the extension-stripped name."""
    result: dict[str, bytes] = {}
    for rel_path, content in files.items():
        target = rel_path[: -len(TEMPLATE_SUFFIX)]
        # If the filename is exactly ".jinja", treat it as a hidden file, not a template.
        if not rel_path.endswith(TEMPLATE_SUFFIX) or not target or target.endswith("/"):
            result[rel_path] = content
            continue
        if target in files:
            raise BuildError(f"Both {target} and {rel_path} exist. Please keep only one of them.")
        result[target] = _render_template(rel_path, content, repo=repo, org=org, values=values)
    return result


def _render_remap_dest(
    source: str, dest_template: str, *, repo: str, org: str, values: dict[str, object]
) -> str:
    """Render a paths: destination template and validate the result.

    Destinations use the same variables as file templates (values / repo /
    org). Static path validation happens at manifest load time for plain
    destinations; the rendered result is (re)validated here because a value
    can smuggle in '/', '..', or a trailing slash that changes the entry kind.
    Failures are converted into BuildError.
    """
    try:
        rendered = _TEMPLATE_ENV.from_string(dest_template).render(
            values=values, repo=repo, org=org
        )
    except TemplateSyntaxError as exc:
        raise BuildError(
            f"manifest.yaml paths['{source}']: invalid template syntax in destination:"
            f" {exc.message}"
        ) from exc
    except UndefinedError as exc:
        raise BuildError(
            f"manifest.yaml paths['{source}']: reference to an undefined variable in"
            f" destination: {exc.message}"
        ) from exc
    if not is_valid_remap_path(rendered):
        raise BuildError(
            f"manifest.yaml paths['{source}']: rendered destination {rendered!r} must be a"
            " relative POSIX path without backslashes, '.', '..', or empty segments."
        )
    if source.endswith("/") != rendered.endswith("/"):
        raise BuildError(
            f"manifest.yaml paths['{source}']: rendered destination {rendered!r} must map a"
            " directory to a directory (both ending in '/') or a file to a file."
        )
    return rendered


def _resolve_remap(rel_path: str, paths: dict[str, str]) -> tuple[str, str] | None:
    """Resolve the paths: entry matching rel_path.

    An exact file entry wins over directory entries (sources ending in "/");
    among directory entries the longest (most specific) prefix wins. File
    paths never end in "/", so an exact lookup cannot hit a directory entry.

    Returns:
        (new path, matched source), or None when no entry matches.
    """
    dest = paths.get(rel_path)
    if dest is not None:
        return dest, rel_path
    best: str | None = None
    for source in paths:
        if not source.endswith("/") or not rel_path.startswith(source):
            continue
        if best is None or len(source) > len(best):
            best = source
    if best is None:
        return None
    return paths[best] + rel_path[len(best) :], best


def _apply_path_remaps(
    files: dict[str, bytes],
    paths: dict[str, str],
    *,
    repo: str,
    org: str,
    values: dict[str, object],
) -> tuple[dict[str, bytes], frozenset[str]]:
    """Apply the manifest's paths: remaps to the rendered file set.

    Destinations are first rendered as Jinja templates (values / repo / org),
    so combined with per-branch values a distributed file's name can differ
    per branch. Sources match the distribution path (after the .jinja suffix
    is stripped): a plain source matches exactly one file, and a source ending
    in "/" matches every file under that directory, moving it to the
    destination directory with its structure preserved. Each file is moved at
    most once — matching is against the original path, never against the
    result of another remap — so swaps (a -> b, b -> a) and chains (a -> b,
    b -> c) are fine, for directories as well as files.

    Returns:
        The remapped files, and the sources that matched no file (judged
        across variants by build_per_variant, not an error here).

    Raises:
        BuildError: If a destination fails to render or is invalid after
            rendering, or two files would end up at the same distribution path.
    """
    if not paths:
        return files, frozenset()
    rendered_paths = {
        source: _render_remap_dest(source, dest, repo=repo, org=org, values=values)
        for source, dest in paths.items()
    }
    matched_sources: set[str] = set()
    matched_files: set[str] = set()
    new_paths: dict[str, str] = {}
    for rel_path in files:
        resolved = _resolve_remap(rel_path, rendered_paths)
        if resolved is None:
            new_paths[rel_path] = rel_path
            continue
        new_path, source = resolved
        new_paths[rel_path] = new_path
        matched_sources.add(source)
        matched_files.add(rel_path)

    origins: dict[str, str] = {}
    result: dict[str, bytes] = {}
    for rel_path, new_path in new_paths.items():
        other = origins.get(new_path)
        if other is not None:
            if other in matched_files and rel_path in matched_files:
                raise BuildError(
                    f"manifest.yaml paths: '{other}' and '{rel_path}' both map to '{new_path}'."
                )
            remapped = rel_path if rel_path in matched_files else other
            raise BuildError(
                f"manifest.yaml paths: destination '{new_path}' for '{remapped}' collides"
                " with a file that is not remapped."
            )
        origins[new_path] = rel_path
        result[new_path] = files[rel_path]
    return result, frozenset(paths) - matched_sources


def _load_ignore_spec(config_root: Path) -> GitIgnoreSpec:
    """Load the .ghfanoutignore at the config repository root.

    Returns an empty spec (matching nothing) when the file is absent. Patterns
    use .gitignore syntax and are matched against each file's path relative to
    its profile directory (i.e. the distribution path, before the .jinja suffix
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
    repository's .ghfanoutignore, or by the manifest's excludes: (already
    resolved to its effective per-branch value by the caller), are excluded
    before composition — the two are evaluated as independent gitignore specs
    so their '!' negations cannot interact. After composition, *.jinja files
    are rendered and lose their extension, then the manifest's paths: remaps
    are applied to the resulting distribution paths.

    Args:
        config_root: Config repository root.
        manifest: Manifest whose bases / values / paths are already effective.
        repo: Fills the built-in {{ repo }} template variable.
        org: Fills the built-in {{ org }} template variable.

    Returns:
        The composed and rendered set of files.

    Raises:
        BuildError: If a profile is missing or a template fails to render.
    """
    ignore_spec = _load_ignore_spec(config_root)
    exclude_spec = GitIgnoreSpec.from_lines(manifest.excludes)

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
            if exclude_spec.match_file(rel_path):
                logger.debug("%s: excluded by manifest.yaml excludes", rel_path)
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

    rendered = _render_templates(files, repo=repo, org=org, values=manifest.values)
    remapped, unmatched = _apply_path_remaps(
        rendered, manifest.paths, repo=repo, org=org, values=manifest.values
    )
    return BuildResult(files=remapped, unmatched_path_sources=unmatched)


# Key identifying a unique combination of build inputs (bases, values, paths,
# and excludes) effective for a branch.
BuildVariantKey = tuple[tuple[str, ...], Hashable, Hashable, tuple[str, ...]]


def _freeze(obj: object) -> Hashable:
    """Convert a value with nested dicts/lists into a hashable form usable as a dict key."""
    if isinstance(obj, dict):
        # The nested parts of values don't validate key types, so use an order-independent frozenset
        return frozenset((_freeze(key), _freeze(value)) for key, value in obj.items())
    if isinstance(obj, list):
        return tuple(_freeze(item) for item in obj)
    return obj


def variant_key(manifest: Manifest, spec: BranchSpec) -> BuildVariantKey:
    """Return a cache key identifying the (bases, values, paths, excludes) effective for spec."""
    return (
        manifest.bases_for(spec),
        _freeze(manifest.values_for(spec)),
        _freeze(manifest.paths_for(spec)),
        manifest.excludes_for(spec),
    )


def build_per_variant(
    config_root: Path, manifest: Manifest, *, repo: str, org: str
) -> dict[BuildVariantKey, BuildResult]:
    """Build each unique combination of (bases, values, paths, excludes) per branch exactly once.

    When branches is omitted (default branch only), build just the one
    top-level combination.

    A paths: source that matches no file in some variants (e.g. because a
    branch overrides bases) is skipped there with an info log, but a source
    that matches nowhere at all is reported as an error — a typo would never
    match anything.

    Returns:
        Mapping of variant_key() -> build result.

    Raises:
        BuildError: If a variant fails to build, or a paths: source matched no
            distributed file in any variant it applies to.
    """
    # The dummy spec has bases / values / paths all None (= inherit top-level),
    # so it produces the same key as the BranchSpec(name=<branch name>) that
    # deploy uses for the default branch.
    specs = manifest.branches or (BranchSpec(name=""),)
    builds: dict[BuildVariantKey, BuildResult] = {}
    seen_sources: set[str] = set()
    matched_sources: set[str] = set()
    unmatched_sources: set[str] = set()
    for spec in specs:
        key = variant_key(manifest, spec)
        if key not in builds:
            effective = replace(
                manifest,
                bases=manifest.bases_for(spec),
                values=manifest.values_for(spec),
                paths=manifest.paths_for(spec),
                excludes=manifest.excludes_for(spec),
            )
            build = build_overlay_files(config_root, effective, repo=repo, org=org)
            builds[key] = build
            seen_sources.update(effective.paths)
            unmatched_sources.update(build.unmatched_path_sources)
            matched_sources.update(
                source for source in effective.paths if source not in build.unmatched_path_sources
            )
    never_matched = sorted(seen_sources - matched_sources)
    if never_matched:
        raise BuildError(
            "manifest.yaml paths: these sources matched no distributed file in any build"
            f" variant: {', '.join(never_matched)} (sources are matched against the"
            f" distribution path, after the {TEMPLATE_SUFFIX} suffix is stripped)."
        )
    for source in sorted(matched_sources & unmatched_sources):
        logger.info(
            "paths: source %s is not present in some build variants; remap skipped there", source
        )
    return builds


def write_build_output(result: BuildResult, output_dir: Path) -> list[Path]:
    """Write the build result to a local directory.

    The output directory is expected to be empty (or not yet exist); existing
    files are overwritten but unrelated files are not deleted.

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

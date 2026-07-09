"""Discovery of the config repository and loading of ghfanout.yaml / manifest.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from ghfanout.errors import ConfigError

# Deploy distribution method. pr: open a pull request (default) / push: push directly to the
# target branch
DeployMode = Literal["pr", "push"]
DEFAULT_DEPLOY_MODE: DeployMode = "pr"

DEFAULT_HOST = "github.com"
DEFAULT_OUTPUT_DIR = "dist"
ROOT_CONFIG_FILENAME = "ghfanout.yaml"
MANIFEST_FILENAME = "manifest.yaml"
OVERLAYS_DIR_NAME = "overlays"
# Ignore file at the config repository root: a .gitignore-syntax list of files
# under base/ that must not be distributed.
IGNORE_FILENAME = ".ghfanoutignore"


@dataclass(frozen=True)
class AppAuthConfig:
    """The auth: section of ghfanout.yaml (non-secret GitHub App authentication settings).

    Secrets such as tokens or private key contents cannot be placed in the YAML
    file and can only be provided via environment variables. When the same item
    is specified in both, the environment variable takes precedence.

    Attributes:
        app_id: GitHub App ID (normalized to a string).
        installation_id: Installation ID of the App on the target org.
        private_key_file: Path to the App's private key PEM file.
    """

    app_id: str | None = None
    installation_id: int | None = None
    private_key_file: Path | None = None


@dataclass(frozen=True)
class RootConfig:
    """Contents of the ghfanout.yaml file at the root of the config repository.

    Attributes:
        org: GitHub org that owns the target repositories.
        host: GitHub host (github.com, or a GitHub Enterprise Server host).
        deploy_mode: Default distribution method ("pr" or "push").
        output_dir: Build output destination; a relative path resolves against
            the config repository root (a CLI --output value takes precedence).
        auth: Non-secret GitHub App authentication settings.
    """

    org: str
    host: str = DEFAULT_HOST
    deploy_mode: DeployMode = DEFAULT_DEPLOY_MODE
    output_dir: str = DEFAULT_OUTPUT_DIR
    auth: AppAuthConfig = field(default_factory=AppAuthConfig)

    @property
    def is_enterprise(self) -> bool:
        """Whether this points to a GitHub Enterprise host other than github.com."""
        return self.host != DEFAULT_HOST


@dataclass(frozen=True)
class BranchSpec:
    """A single element of branches: in manifest.yaml.

    Attributes:
        name: Target branch name.
        bases: Branch-specific bases; None inherits the top-level bases (an
            explicit empty tuple means "distribute only common/").
        values: Branch-specific values; None uses the top-level values as-is,
            otherwise deep-merged into them (unlike bases, which is replaced).
        paths: Branch-specific path remaps; None uses the top-level paths
            as-is, otherwise shallow-merged into them (a null destination
            removes the inherited remap for that source).
        excludes: Branch-specific exclude patterns; None means no branch
            override (the top-level excludes apply as-is). Otherwise these
            patterns are added to the top-level excludes (a union, not a
            replacement).
    """

    name: str
    bases: tuple[str, ...] | None = None
    values: dict[str, object] | None = None
    paths: dict[str, str | None] | None = None
    excludes: tuple[str, ...] | None = None


def _deep_merge(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    """Return a new dict with override deep-merged into base.

    Dicts are recursively merged key by key; anything else (scalars, lists, or
    mismatched types) is replaced wholesale by the override side (lists are not
    concatenated).
    """
    merged: dict[str, object] = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class Manifest:
    """Contents of overlays/<repo>/manifest.yaml.

    Attributes:
        bases: Profiles under base/ to compose, in increasing precedence.
        branches: Target branches; an empty tuple means "only the target
            repository's default branch".
        deploy_mode: Per-repository override of ghfanout.yaml's deploy_mode;
            None inherits it.
        values: Values (optionally nested) referenced from templates (*.jinja)
            via {{ values.xxx }}.
        paths: Distribution path remaps (source -> destination). Sources are
            matched against the distribution path, after the .jinja suffix is
            stripped.
        excludes: Exclude patterns (.gitignore syntax, same as
            .ghfanoutignore) scoped to this overlay, matched at the same
            point as .ghfanoutignore (the profile-relative path, before the
            .jinja suffix is stripped).
    """

    bases: tuple[str, ...] = ()
    branches: tuple[BranchSpec, ...] = ()
    deploy_mode: DeployMode | None = None
    values: dict[str, object] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    excludes: tuple[str, ...] = ()

    def bases_for(self, spec: BranchSpec) -> tuple[str, ...]:
        """Effective bases for the branch (inherited when there is no branch override)."""
        return spec.bases if spec.bases is not None else self.bases

    def values_for(self, spec: BranchSpec) -> dict[str, object]:
        """Effective values for the branch (branch override deep-merged into the top level)."""
        if spec.values is None:
            return self.values
        return _deep_merge(self.values, spec.values)

    def paths_for(self, spec: BranchSpec) -> dict[str, str]:
        """Effective paths for the branch (branch override shallow-merged into the top level).

        Unlike values (a recursive deep merge), paths is a flat source ->
        destination mapping, so the merge is per source; a null destination in
        the branch override removes the inherited remap for that source.
        """
        if spec.paths is None:
            return self.paths
        merged: dict[str, str | None] = {**self.paths, **spec.paths}
        return {source: dest for source, dest in merged.items() if dest is not None}

    def excludes_for(self, spec: BranchSpec) -> tuple[str, ...]:
        """Effective excludes for the branch (branch excludes added to the top-level ones).

        Unlike paths (per-source override), excludes is a union: the branch
        patterns are appended after the top-level ones, never removing them.
        Order is preserved (not deduplicated as a set) because .gitignore
        syntax is order-sensitive — a branch can write a '!pattern' to negate
        a top-level exclude.
        """
        if spec.excludes is None:
            return self.excludes
        return self.excludes + spec.excludes

    @property
    def has_branch_specific_build(self) -> bool:
        """Whether any branch overrides bases, values, paths, or excludes.

        A branch-specific build is needed when this is true.
        """
        return any(
            spec.bases is not None
            or spec.values is not None
            or spec.paths is not None
            or spec.excludes is not None
            for spec in self.branches
        )


def find_config_root(start_dir: Path) -> Path:
    """Search upward from start_dir for ghfanout.yaml.

    Returns:
        The closest directory containing ghfanout.yaml.

    Raises:
        ConfigError: If no ghfanout.yaml is found up to the filesystem root.
    """
    current = start_dir.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ROOT_CONFIG_FILENAME).is_file():
            return candidate
    raise ConfigError(
        f"{ROOT_CONFIG_FILENAME} not found "
        f"(searched upward from {start_dir}). "
        "Run this inside a config repository, or specify the root with -C/--config-dir."
    )


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    """Read a YAML file and return its top-level mapping (an empty dict for an empty file)."""
    if not path.is_file():
        raise ConfigError(f"{path} not found.")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse YAML in {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the top level must be a mapping (key: value).")
    return raw


def _parse_deploy_mode(path: Path, data: dict[str, object]) -> DeployMode | None:
    """Validate and return the 'deploy_mode' key. Returns None if unspecified."""
    raw = data.get("deploy_mode")
    if raw is None:
        return None
    if raw == "pr":
        return "pr"
    if raw == "push":
        return "push"
    raise ConfigError(f"{path}: 'deploy_mode' must be either 'pr' or 'push'.")


def _parse_output_dir(path: Path, data: dict[str, object]) -> str:
    """Validate and return the 'output_dir' key. Defaults to 'dist' if unspecified."""
    raw = data.get("output_dir", DEFAULT_OUTPUT_DIR)
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{path}: 'output_dir' must be a non-empty string.")
    return raw


# Keys allowed under auth: (unknown keys are rejected to catch typos)
_AUTH_KEYS = frozenset({"app_id", "installation_id", "private_key_file"})


def _parse_app_id(path: Path, raw: object) -> str | None:
    """Validate 'auth.app_id' and normalize it to a string to match environment variables."""
    if raw is None:
        return None
    # bool is a subclass of int, so it must be rejected explicitly (otherwise YAML's
    # true would pass through as a number)
    if isinstance(raw, bool) or not isinstance(raw, int | str) or not str(raw):
        raise ConfigError(f"{path}: 'auth.app_id' must be a number or a non-empty string.")
    return str(raw)


def _parse_installation_id(path: Path, raw: object) -> int | None:
    """Validate 'auth.installation_id' as a positive integer and return it."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ConfigError(f"{path}: 'auth.installation_id' must be a positive integer.")
    return raw


def _parse_private_key_file(path: Path, raw: object, config_root: Path) -> Path | None:
    """Validate and resolve 'auth.private_key_file' as a path, then return it."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ConfigError(f"{path}: 'auth.private_key_file' must be a non-empty string.")
    # Expand ~, and resolve relative paths against the config repository root
    # (absolute paths are left as-is)
    return config_root / Path(raw).expanduser()


def _parse_auth(path: Path, data: dict[str, object], config_root: Path) -> AppAuthConfig:
    """Validate and return the 'auth' section. All fields are None if unspecified."""
    raw = data.get("auth")
    if raw is None:
        return AppAuthConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 'auth' must be a mapping (key: value).")
    unknown_keys = sorted(str(key) for key in raw if key not in _AUTH_KEYS)
    if unknown_keys:
        raise ConfigError(f"{path}: 'auth' has unknown keys: {', '.join(unknown_keys)}")
    return AppAuthConfig(
        app_id=_parse_app_id(path, raw.get("app_id")),
        installation_id=_parse_installation_id(path, raw.get("installation_id")),
        private_key_file=_parse_private_key_file(path, raw.get("private_key_file"), config_root),
    )


def load_root_config(config_root: Path) -> RootConfig:
    """Load ghfanout.yaml from the config repository root.

    Returns:
        The parsed config ('org' is required; every other key has a default).

    Raises:
        ConfigError: If the file is missing or a key fails validation.
    """
    path = config_root / ROOT_CONFIG_FILENAME
    data = _load_yaml_mapping(path)

    org = data.get("org")
    if not isinstance(org, str) or not org:
        raise ConfigError(f"{path}: the required key 'org' must be a non-empty string.")

    host = data.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or not host:
        raise ConfigError(f"{path}: 'host' must be a non-empty string.")

    return RootConfig(
        org=org,
        host=host,
        deploy_mode=_parse_deploy_mode(path, data) or DEFAULT_DEPLOY_MODE,
        output_dir=_parse_output_dir(path, data),
        auth=_parse_auth(path, data, config_root),
    )


def _parse_bases(path: Path, raw: object, label: str) -> tuple[str, ...]:
    """Validate a 'bases'-like key as a list of non-empty strings and return it."""
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ConfigError(f"{path}: '{label}' must be a list of non-empty strings.")
    return tuple(raw)


# Keys allowed on object elements of branches: (unknown keys are rejected to catch typos)
_BRANCH_SPEC_KEYS = frozenset({"name", "bases", "values", "paths", "excludes"})


def _parse_branch_spec(path: Path, item: object) -> BranchSpec:
    """Convert a single 'branches' element (a string or a mapping) into a BranchSpec."""
    if isinstance(item, str) and item:
        return BranchSpec(name=item)
    if isinstance(item, dict):
        unknown_keys = sorted(str(key) for key in item if key not in _BRANCH_SPEC_KEYS)
        if unknown_keys:
            raise ConfigError(
                f"{path}: a 'branches' element has unknown keys: {', '.join(unknown_keys)}"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"{path}: an object element of 'branches' must specify 'name' as a "
                "non-empty string."
            )
        bases = _parse_bases(path, item["bases"], "branches[].bases") if "bases" in item else None
        values = (
            _parse_values_mapping(path, item["values"], "branches[].values")
            if "values" in item
            else None
        )
        paths = (
            _parse_paths_mapping(path, item["paths"], "branches[].paths", allow_null=True)
            if "paths" in item
            else None
        )
        excludes = (
            _parse_bases(path, item["excludes"], "branches[].excludes")
            if "excludes" in item
            else None
        )
        return BranchSpec(name=name, bases=bases, values=values, paths=paths, excludes=excludes)
    raise ConfigError(
        f"{path}: a 'branches' element must be either a branch name string or a "
        "mapping with name / bases."
    )


def _parse_branches(path: Path, data: dict[str, object]) -> tuple[BranchSpec, ...]:
    """Validate and return the 'branches' key. Defaults to an empty tuple if unspecified."""
    raw = data.get("branches", [])
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: 'branches' must be a list.")
    specs = tuple(_parse_branch_spec(path, item) for item in raw)

    names = [spec.name for spec in specs]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise ConfigError(f"{path}: 'branches' has duplicate branch names: {', '.join(duplicated)}")
    return specs


def _parse_values_mapping(path: Path, raw: object, label: str) -> dict[str, object]:
    """Validate a 'values'-like mapping and return it.

    Returns an empty dict if None. Values may be nested and are unrestricted in type.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: '{label}' must be a mapping (key: value).")
    non_str_keys = sorted(str(key) for key in raw if not isinstance(key, str))
    if non_str_keys:
        raise ConfigError(f"{path}: keys of '{label}' must be strings: {', '.join(non_str_keys)}")
    return dict(raw)


def _parse_values(path: Path, data: dict[str, object]) -> dict[str, object]:
    """Validate and return the top-level 'values' key, defaulting to an empty dict."""
    return _parse_values_mapping(path, data.get("values"), "values")


def is_valid_remap_path(value: str) -> bool:
    """Whether value is a valid 'paths' source or destination (a relative POSIX path).

    A single trailing slash (marking a directory entry) is allowed; apart from
    that the path must have no empty, '.', or '..' segments, and no
    backslashes. This also rejects a bare "/" (its core is a single empty
    segment), which would otherwise match every file. The builder reuses this
    to validate destinations after Jinja rendering.
    """
    core = value[:-1] if value.endswith("/") else value
    return "\\" not in value and all(segment not in ("", ".", "..") for segment in core.split("/"))


def _validate_remap_path(path: Path, subject: str, value: str) -> None:
    """Validate a 'paths'-like source or destination, raising ConfigError when invalid."""
    if not is_valid_remap_path(value):
        raise ConfigError(
            f"{path}: {subject} must be a relative POSIX path"
            f" without backslashes, '.', '..', or empty segments: {value!r}"
        )


def _contains_jinja(value: str) -> bool:
    """Whether value contains Jinja syntax (its path validation happens after rendering)."""
    return "{{" in value or "{%" in value or "{#" in value


def _parse_paths_mapping(
    path: Path, raw: object, label: str, *, allow_null: bool
) -> dict[str, str | None]:
    """Validate a 'paths'-like mapping (source path -> destination path) and return it.

    A source and destination that both end in "/" form a directory entry;
    mixing a directory on one side with a file on the other is rejected.
    Destinations are rendered as Jinja templates at build time, so a
    destination containing Jinja syntax skips the static path validation here
    (the rendered result is validated instead). A null destination is allowed
    only when allow_null is True — a branch override uses it to remove an
    inherited remap.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: '{label}' must be a mapping (key: value).")
    result: dict[str, str | None] = {}
    for source, dest in raw.items():
        if not isinstance(source, str) or not source:
            raise ConfigError(f"{path}: keys of '{label}' must be non-empty strings: {source!r}")
        _validate_remap_path(path, f"'{label}' source '{source}'", source)
        if dest is None:
            if not allow_null:
                raise ConfigError(
                    f"{path}: '{label}' has a null destination for '{source}' (null is only"
                    " allowed in a branch override, to remove an inherited remap)."
                )
        elif not isinstance(dest, str):
            raise ConfigError(f"{path}: '{label}' destination for '{source}' must be a string.")
        else:
            if not _contains_jinja(dest):
                _validate_remap_path(path, f"'{label}' destination for '{source}'", dest)
            if source.endswith("/") != dest.endswith("/"):
                raise ConfigError(
                    f"{path}: '{label}' entry for '{source}' must map a directory to a"
                    " directory (both ending in '/') or a file to a file, not mix the"
                    f" two: {dest!r}"
                )
        result[source] = dest
    return result


def _parse_paths(path: Path, data: dict[str, object]) -> dict[str, str]:
    """Validate and return the top-level 'paths' key, defaulting to an empty dict."""
    raw = data.get("paths")
    if raw is None:
        return {}
    parsed = _parse_paths_mapping(path, raw, "paths", allow_null=False)
    # allow_null=False rejected null destinations, so this only narrows the value type
    return {source: dest for source, dest in parsed.items() if dest is not None}


def load_manifest(config_root: Path, overlay: str) -> Manifest:
    """Load overlays/<overlay>/manifest.yaml.

    Returns:
        The parsed manifest (every key is optional).

    Raises:
        ConfigError: If the file is missing or a key fails validation.
    """
    path = config_root / OVERLAYS_DIR_NAME / overlay / MANIFEST_FILENAME
    data = _load_yaml_mapping(path)

    return Manifest(
        bases=_parse_bases(path, data.get("bases", []), "bases"),
        branches=_parse_branches(path, data),
        deploy_mode=_parse_deploy_mode(path, data),
        values=_parse_values(path, data),
        paths=_parse_paths(path, data),
        excludes=_parse_bases(path, data.get("excludes", []), "excludes"),
    )


def list_overlays(config_root: Path) -> list[str]:
    """List directory names under overlays/ (= repository names) that contain a manifest.yaml.

    Returns:
        Sorted overlay names.

    Raises:
        ConfigError: If the overlays/ directory does not exist.
    """
    overlays_dir = config_root / OVERLAYS_DIR_NAME
    if not overlays_dir.is_dir():
        raise ConfigError(f"{overlays_dir} not found.")
    return sorted(
        entry.name
        for entry in overlays_dir.iterdir()
        if entry.is_dir() and (entry / MANIFEST_FILENAME).is_file()
    )

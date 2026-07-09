"""Tests for the config module (config discovery and ghfanout.yaml / manifest.yaml parsing)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ghfanout.config import (
    AppAuthConfig,
    BranchSpec,
    Manifest,
    find_config_root,
    list_overlays,
    load_manifest,
    load_root_config,
)
from ghfanout.errors import ConfigError


class TestFindConfigRoot:
    def test_returns_as_is_when_in_current_directory(self, config_repo: Path) -> None:
        assert find_config_root(config_repo) == config_repo.resolve()

    def test_searches_upward_from_subdirectory(self, config_repo: Path) -> None:
        subdir = config_repo / "overlays" / "user-service"
        assert find_config_root(subdir) == config_repo.resolve()

    def test_raises_config_error_when_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=re.escape("ghfanout.yaml not found")):
            find_config_root(tmp_path)


class TestLoadRootConfig:
    def test_defaults_host_to_github_com_when_omitted(self, config_repo: Path) -> None:
        config = load_root_config(config_repo)
        assert config.org == "myorg"
        assert config.host == "github.com"
        assert config.is_enterprise is False

    def test_can_specify_enterprise_host(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text(
            "host: github.example.com\norg: myorg\n", encoding="utf-8"
        )
        config = load_root_config(tmp_path)
        assert config.host == "github.example.com"
        assert config.is_enterprise is True

    def test_defaults_deploy_mode_to_pr_when_omitted(self, config_repo: Path) -> None:
        config = load_root_config(config_repo)
        assert config.deploy_mode == "pr"

    def test_can_specify_push_for_deploy_mode(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\ndeploy_mode: push\n", encoding="utf-8")
        config = load_root_config(tmp_path)
        assert config.deploy_mode == "push"

    def test_raises_config_error_for_invalid_deploy_mode(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text(
            "org: myorg\ndeploy_mode: direct\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="deploy_mode"):
            load_root_config(tmp_path)

    def test_raises_config_error_for_non_string_deploy_mode(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\ndeploy_mode: [pr]\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="deploy_mode"):
            load_root_config(tmp_path)

    def test_raises_config_error_when_org_missing(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("host: github.com\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="org"):
            load_root_config(tmp_path)

    def test_raises_config_error_for_empty_host(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text('org: myorg\nhost: ""\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="host"):
            load_root_config(tmp_path)

    def test_defaults_output_dir_to_dist_when_omitted(self, config_repo: Path) -> None:
        assert load_root_config(config_repo).output_dir == "dist"

    def test_can_specify_output_dir(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\noutput_dir: out\n", encoding="utf-8")
        assert load_root_config(tmp_path).output_dir == "out"

    def test_raises_config_error_for_empty_output_dir(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text('org: myorg\noutput_dir: ""\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="output_dir"):
            load_root_config(tmp_path)

    def test_auth_fields_are_none_when_omitted(self, config_repo: Path) -> None:
        assert load_root_config(config_repo).auth == AppAuthConfig()

    def test_loads_auth(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text(
            "org: myorg\n"
            "auth:\n"
            "  app_id: 123456\n"
            "  installation_id: 789\n"
            "  private_key_file: keys/app.pem\n",
            encoding="utf-8",
        )
        config = load_root_config(tmp_path)
        # app_id accepts both numbers and strings, normalized to a string to match env vars
        assert config.auth.app_id == "123456"
        assert config.auth.installation_id == 789
        # Relative paths are resolved against the config repository root
        assert config.auth.private_key_file == tmp_path / "keys" / "app.pem"

    def test_auth_private_key_file_uses_absolute_path_as_is(self, tmp_path: Path) -> None:
        key_path = tmp_path / "elsewhere" / "app.pem"
        (tmp_path / "ghfanout.yaml").write_text(
            f"org: myorg\nauth:\n  private_key_file: {key_path.as_posix()}\n", encoding="utf-8"
        )
        assert load_root_config(tmp_path).auth.private_key_file == key_path

    def test_auth_private_key_file_is_none_when_omitted(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text(
            "org: myorg\nauth:\n  app_id: 123456\n", encoding="utf-8"
        )
        assert load_root_config(tmp_path).auth.private_key_file is None

    def test_raises_config_error_for_non_string_private_key_file(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text(
            "org: myorg\nauth:\n  private_key_file: 123\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="private_key_file"):
            load_root_config(tmp_path)

    def test_raises_config_error_when_auth_is_not_a_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\nauth: [a]\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="auth"):
            load_root_config(tmp_path)

    def test_raises_config_error_for_unknown_auth_key(self, tmp_path: Path) -> None:
        # Unknown keys are explicitly rejected so typos (e.g. appid) are caught
        (tmp_path / "ghfanout.yaml").write_text(
            "org: myorg\nauth:\n  appid: 123\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="appid"):
            load_root_config(tmp_path)

    def test_raises_config_error_for_bool_app_id(self, tmp_path: Path) -> None:
        # YAML's true is parsed as a bool (a subclass of int), so it is explicitly rejected
        (tmp_path / "ghfanout.yaml").write_text(
            "org: myorg\nauth:\n  app_id: true\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="app_id"):
            load_root_config(tmp_path)

    def test_raises_config_error_for_string_installation_id(self, tmp_path: Path) -> None:
        # Unlike environment variables, YAML has a numeric type, so only positive
        # integers are accepted
        (tmp_path / "ghfanout.yaml").write_text(
            'org: myorg\nauth:\n  installation_id: "789"\n', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="installation_id"):
            load_root_config(tmp_path)

    def test_raises_config_error_when_top_level_is_not_a_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_root_config(tmp_path)

    def test_raises_config_error_when_file_missing(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_root_config(tmp_path)

    def test_raises_config_error_for_broken_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="Failed to parse YAML"):
            load_root_config(tmp_path)


class TestLoadManifest:
    def test_loads_bases_and_branches(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - main\n  - release-1.x\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.bases == ("java-service",)
        assert manifest.branches == (BranchSpec(name="main"), BranchSpec(name="release-1.x"))

    def test_defaults_branches_to_empty_tuple_when_omitted(self, config_repo: Path) -> None:
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.branches == ()

    def test_allows_empty_manifest(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "empty-service"
        overlay_dir.mkdir()
        (overlay_dir / "manifest.yaml").write_text("", encoding="utf-8")
        manifest = load_manifest(config_repo, "empty-service")
        assert manifest.bases == ()
        assert manifest.branches == ()

    def test_defaults_deploy_mode_to_none_when_omitted(self, config_repo: Path) -> None:
        # None means "inherit deploy_mode from ghfanout.yaml"
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.deploy_mode is None

    def test_can_specify_push_for_deploy_mode(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\ndeploy_mode: push\n", encoding="utf-8"
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.deploy_mode == "push"

    def test_can_specify_pr_for_deploy_mode(self, config_repo: Path) -> None:
        # Unlike None (inherit from ghfanout.yaml), an explicit pr fixes the mode
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\ndeploy_mode: pr\n", encoding="utf-8"
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.deploy_mode == "pr"

    def test_raises_config_error_for_invalid_deploy_mode(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\ndeploy_mode: direct\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="deploy_mode"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_when_manifest_missing(self, config_repo: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_manifest(config_repo, "no-such-service")

    def test_raises_config_error_when_bases_is_not_a_string_list(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text("bases: java-service\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="bases"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_when_branches_is_not_a_list(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches: main\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="branches"):
            load_manifest(config_repo, "user-service")

    def test_can_override_bases_per_branch_via_object_element(self, config_repo: Path) -> None:
        # A string (inheriting the top-level bases) and an object (overriding bases)
        # can be mixed
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "branches:\n  - main\n  - name: release-1.x\n    bases: [java-legacy]\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.branches == (
            BranchSpec(name="main"),
            BranchSpec(name="release-1.x", bases=("java-legacy",)),
        )

    def test_bases_for_prefers_branch_override_and_inherits_when_none(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", bases=("java-legacy",)),
                BranchSpec(name="minimal", bases=()),
            ),
        )
        assert manifest.bases_for(manifest.branches[0]) == ("java-service",)
        assert manifest.bases_for(manifest.branches[1]) == ("java-legacy",)
        # Explicitly setting an empty list means "common/ only" (no inheritance)
        assert manifest.bases_for(manifest.branches[2]) == ()

    def test_has_branch_specific_build_reflects_bases_or_values_override(self) -> None:
        inherit_only = Manifest(bases=("java-service",), branches=(BranchSpec(name="main"),))
        assert inherit_only.has_branch_specific_build is False

        with_bases = Manifest(
            bases=("java-service",),
            branches=(BranchSpec(name="release-1.x", bases=("java-legacy",)),),
        )
        assert with_bases.has_branch_specific_build is True

        with_values = Manifest(
            bases=("java-service",),
            branches=(BranchSpec(name="release-1.x", values={"version": "0.9"}),),
        )
        assert with_values.has_branch_specific_build is True

        with_both = Manifest(
            bases=("java-service",),
            branches=(
                BranchSpec(name="release-1.x", bases=("java-legacy",), values={"version": "0.9"}),
            ),
        )
        assert with_both.has_branch_specific_build is True

    def test_can_explicitly_set_empty_bases_list_via_branch_object(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - name: docs\n    bases: []\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.branches == (BranchSpec(name="docs", bases=()),)

    def test_raises_config_error_when_branch_object_missing_name(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - bases: [java-legacy]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="name"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_invalid_bases_type_in_branch_object(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - name: main\n    bases: java-legacy\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="bases"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_unknown_key_in_branch_object(self, config_repo: Path) -> None:
        # Unknown keys are explicitly rejected so typos (e.g. basis) are caught
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - name: main\n    basis: [java-legacy]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="basis"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_duplicate_branch_names(self, config_repo: Path) -> None:
        # Duplicates are rejected to avoid ambiguity from differing bases under the
        # same branch name
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "branches:\n  - main\n  - name: main\n    bases: [java-legacy]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="duplicate"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_when_branch_element_is_neither_string_nor_mapping(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - 123\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="branches"):
            load_manifest(config_repo, "user-service")

    def test_loads_values(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            'bases:\n  - java-service\nvalues:\n  artifactId: user-service\n  javaVersion: "21"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.values == {"artifactId": "user-service", "javaVersion": "21"}

    def test_preserves_nested_values_structure(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nvalues:\n  maven:\n    groupId: com.example\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.values == {"maven": {"groupId": "com.example"}}

    def test_defaults_values_to_empty_dict_when_omitted(self, config_repo: Path) -> None:
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.values == {}

    def test_raises_config_error_when_values_is_not_a_mapping(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nvalues:\n  - a\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="values"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_non_string_values_key(self, config_repo: Path) -> None:
        # In YAML, keys like 1: are parsed as int, so they are explicitly rejected
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nvalues:\n  1: one\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="values"):
            load_manifest(config_repo, "user-service")

    def test_can_override_values_per_branch_via_object_element(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            'values:\n  version: "1.0"\n'
            'branches:\n  - main\n  - name: release-1.x\n    values:\n      version: "0.9"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.values == {"version": "1.0"}
        assert manifest.branches == (
            BranchSpec(name="main"),
            BranchSpec(name="release-1.x", values={"version": "0.9"}),
        )

    def test_raises_config_error_for_invalid_values_type_in_branch_object(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - name: main\n    values: [a]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="values"):
            load_manifest(config_repo, "user-service")

    def test_values_for_deep_merges_branch_override_into_top_level(self) -> None:
        manifest = Manifest(
            values={"version": "1.0", "maven": {"groupId": "com.example", "javaVersion": "21"}},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(
                    name="release-1.x",
                    values={"version": "0.9", "maven": {"javaVersion": "8"}},
                ),
            ),
        )
        # No values key (None) inherits the top level as-is
        assert manifest.values_for(manifest.branches[0]) == manifest.values
        # Dicts are recursively merged key by key, inheriting shared values while
        # overriding only the differences
        assert manifest.values_for(manifest.branches[1]) == {
            "version": "0.9",
            "maven": {"groupId": "com.example", "javaVersion": "8"},
        }

    def test_values_for_replaces_lists_and_mismatched_types_wholesale(self) -> None:
        manifest = Manifest(
            values={"plugins": ["a", "b"], "maven": {"groupId": "com.example"}},
            branches=(
                BranchSpec(name="release-1.x", values={"plugins": ["c"], "maven": "disabled"}),
            ),
        )
        # Lists are replaced rather than concatenated (same as Helm); dict/non-dict
        # type mismatches are also replaced
        assert manifest.values_for(manifest.branches[0]) == {
            "plugins": ["c"],
            "maven": "disabled",
        }

    def test_loads_paths(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: services/user/pom.xml\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.paths == {"pom.xml": "services/user/pom.xml"}

    def test_defaults_paths_to_empty_dict_when_omitted(self, config_repo: Path) -> None:
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.paths == {}

    def test_can_override_paths_per_branch_via_object_element(self, config_repo: Path) -> None:
        # A null destination is allowed only in a branch override (removes the remap)
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "paths:\n  pom.xml: services/user/pom.xml\n"
            "branches:\n  - main\n  - name: release-1.x\n    paths:\n"
            "      pom.xml: legacy/pom.xml\n"
            "      extra.txt: null\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.paths == {"pom.xml": "services/user/pom.xml"}
        assert manifest.branches == (
            BranchSpec(name="main"),
            BranchSpec(name="release-1.x", paths={"pom.xml": "legacy/pom.xml", "extra.txt": None}),
        )

    def test_paths_for_shallow_merges_branch_override_into_top_level(self) -> None:
        manifest = Manifest(
            paths={"pom.xml": "services/user/pom.xml", "ci.yml": ".github/workflows/ci.yml"},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(
                    name="release-1.x",
                    paths={"pom.xml": "legacy/pom.xml", "extra.txt": "docs/extra.txt"},
                ),
            ),
        )
        # No paths key (None) inherits the top level as-is
        assert manifest.paths_for(manifest.branches[0]) == manifest.paths
        # The merge is per source: inherited entries are kept unless overridden
        assert manifest.paths_for(manifest.branches[1]) == {
            "pom.xml": "legacy/pom.xml",
            "ci.yml": ".github/workflows/ci.yml",
            "extra.txt": "docs/extra.txt",
        }

    def test_paths_for_removes_remap_when_branch_destination_is_null(self) -> None:
        manifest = Manifest(
            paths={"pom.xml": "services/user/pom.xml", "ci.yml": ".github/workflows/ci.yml"},
            branches=(BranchSpec(name="release-1.x", paths={"ci.yml": None, "unknown.txt": None}),),
        )
        # null removes the inherited remap; null for a source without a remap is harmless
        assert manifest.paths_for(manifest.branches[0]) == {"pom.xml": "services/user/pom.xml"}

    def test_has_branch_specific_build_reflects_paths_override(self) -> None:
        with_paths = Manifest(
            bases=("java-service",),
            branches=(BranchSpec(name="release-1.x", paths={"pom.xml": "legacy/pom.xml"}),),
        )
        assert with_paths.has_branch_specific_build is True

    def test_raises_config_error_when_paths_is_not_a_mapping(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  - a\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="paths"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_non_string_paths_key(self, config_repo: Path) -> None:
        # In YAML, keys like 1: are parsed as int, so they are explicitly rejected
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  1: one.txt\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="non-empty strings"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_empty_paths_key(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            'bases:\n  - java-service\npaths:\n  "": dest.txt\n', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="non-empty strings"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_non_string_paths_destination(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: [a]\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="must be a string"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_null_destination_at_top_level(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: null\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="null is only"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_when_branch_paths_is_not_a_mapping(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - name: main\n    paths: [a]\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match=re.escape("branches[].paths")):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_non_string_branch_paths_destination(
        self, config_repo: Path
    ) -> None:
        # Only strings and null (removal) are allowed as branch override destinations
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - name: main\n    paths:\n      pom.xml: 123\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="must be a string"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_absolute_paths_destination(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: /etc/pom.xml\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_parent_segment_in_paths_destination(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: ../pom.xml\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_dot_segment_in_paths_destination(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: ./pom.xml\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_file_source_with_directory_destination(
        self, config_repo: Path
    ) -> None:
        # "pom.xml: dir/" mixes a file source with a directory destination
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: dir/\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="file to a file"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_backslash_in_paths_destination(
        self, config_repo: Path
    ) -> None:
        # Distribution paths are POSIX; backslashes would break Windows checkouts anyway
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  pom.xml: 'dir\\pom.xml'\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_empty_paths_destination(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            'bases:\n  - java-service\npaths:\n  pom.xml: ""\n', encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_loads_directory_paths_entry(self, config_repo: Path) -> None:
        # Source and destination both ending in "/" form a directory entry
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  workflows/: .github/workflows/\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.paths == {"workflows/": ".github/workflows/"}

    def test_branch_paths_can_remove_directory_entry(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "paths:\n  workflows/: .github/workflows/\n"
            "branches:\n  - main\n  - name: release-1.x\n    paths:\n      workflows/: null\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.branches[1] == BranchSpec(name="release-1.x", paths={"workflows/": None})
        assert manifest.paths_for(manifest.branches[1]) == {}

    def test_raises_config_error_for_directory_source_with_file_destination(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  workflows/: ci.yml\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="directory to a"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_bare_slash_paths_source(self, config_repo: Path) -> None:
        # "/" would match every file; its core is a single empty segment
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  /: dest/\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_parent_segment_in_directory_destination(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  workflows/: ../elsewhere/\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_invalid_paths_source(self, config_repo: Path) -> None:
        # Sources are validated with the same path rules as destinations
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\npaths:\n  ../escape.txt: dest.txt\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="relative POSIX path"):
            load_manifest(config_repo, "user-service")

    def test_templated_paths_destination_skips_static_validation(self, config_repo: Path) -> None:
        # A destination containing Jinja syntax is validated after rendering
        # (at build time), so even a suspicious-looking template passes parsing
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "paths:\n"
            "  deploy.yml: deploy-{{ values.env }}.yml\n"
            '  b.yml: "{{ values.dir }}/../b.yml"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.paths == {
            "deploy.yml": "deploy-{{ values.env }}.yml",
            "b.yml": "{{ values.dir }}/../b.yml",
        }

    def test_symmetry_check_applies_to_templated_destination(self, config_repo: Path) -> None:
        # The trailing-slash symmetry is judged on the template text itself
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            'bases:\n  - java-service\npaths:\n  workflows/: "{{ values.dir }}"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="directory to a"):
            load_manifest(config_repo, "user-service")

    def test_loads_excludes(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nexcludes:\n  - internal-notes.md\n  - drafts/\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.excludes == ("internal-notes.md", "drafts/")

    def test_defaults_excludes_to_empty_tuple_when_omitted(self, config_repo: Path) -> None:
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.excludes == ()

    def test_can_override_excludes_per_branch_via_object_element(self, config_repo: Path) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "excludes:\n  - drafts/\n"
            "branches:\n  - main\n  - name: release-1.x\n    excludes:\n      - legacy/pom.xml\n",
            encoding="utf-8",
        )
        manifest = load_manifest(config_repo, "user-service")
        assert manifest.excludes == ("drafts/",)
        assert manifest.branches == (
            BranchSpec(name="main"),
            BranchSpec(name="release-1.x", excludes=("legacy/pom.xml",)),
        )

    def test_excludes_for_appends_branch_override_after_top_level(self) -> None:
        manifest = Manifest(
            excludes=("drafts/", "*.tmp"),
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", excludes=("legacy/pom.xml",)),
                BranchSpec(name="minimal", excludes=()),
            ),
        )
        # No excludes key (None) inherits the top level as-is
        assert manifest.excludes_for(manifest.branches[0]) == ("drafts/", "*.tmp")
        # Branch patterns are appended, order preserved (gitignore is order-sensitive)
        assert manifest.excludes_for(manifest.branches[1]) == ("drafts/", "*.tmp", "legacy/pom.xml")
        # An empty branch override changes nothing (it's a union, not a replacement)
        assert manifest.excludes_for(manifest.branches[2]) == ("drafts/", "*.tmp")

    def test_has_branch_specific_build_reflects_excludes_override(self) -> None:
        with_excludes = Manifest(
            bases=("java-service",),
            branches=(BranchSpec(name="release-1.x", excludes=("legacy/pom.xml",)),),
        )
        assert with_excludes.has_branch_specific_build is True

    def test_raises_config_error_when_excludes_is_not_a_string_list(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nexcludes:\n  key: value\n", encoding="utf-8"
        )
        with pytest.raises(ConfigError, match="excludes"):
            load_manifest(config_repo, "user-service")

    def test_raises_config_error_for_invalid_excludes_type_in_branch_object(
        self, config_repo: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "branches:\n  - name: release-1.x\n    excludes: not-a-list\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match=re.escape("branches[].excludes")):
            load_manifest(config_repo, "user-service")


class TestListOverlays:
    def test_returns_only_directories_with_manifest_sorted(self, config_repo: Path) -> None:
        api_dir = config_repo / "overlays" / "api-gateway"
        api_dir.mkdir()
        (api_dir / "manifest.yaml").write_text("bases: []\n", encoding="utf-8")
        (config_repo / "overlays" / "not-an-overlay").mkdir()

        assert list_overlays(config_repo) == ["api-gateway", "user-service"]

    def test_raises_config_error_when_overlays_directory_missing(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: myorg\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="overlays"):
            list_overlays(tmp_path)

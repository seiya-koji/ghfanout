"""Tests for the builder module (profile composition and local output)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from ghfanout.builder import (
    build_overlay_files,
    build_per_variant,
    variant_key,
    write_build_output,
)
from ghfanout.config import BranchSpec, Manifest
from ghfanout.errors import BuildError


class TestBuildOverlayFiles:
    def test_composes_common_and_selected_profile(self, config_repo: Path) -> None:
        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert result.files == {
            ".gitignore": b"target/\n",  # java-service overrides common (last wins)
            ".github/CODEOWNERS": b"* @myorg/platform\n",
            "pom.xml": b"<project/>\n",
        }

    def test_distributes_common_only_when_bases_empty(self, config_repo: Path) -> None:
        result = build_overlay_files(config_repo, Manifest(), repo="user-service", org="myorg")

        assert result.files == {
            ".gitignore": b"*.log\n",
            ".github/CODEOWNERS": b"* @myorg/platform\n",
        }

    def test_later_profile_in_bases_takes_precedence(self, config_repo: Path) -> None:
        legacy_dir = config_repo / "base" / "java-legacy"
        legacy_dir.mkdir()
        (legacy_dir / "pom.xml").write_bytes(b"<project>legacy</project>\n")

        result = build_overlay_files(
            config_repo,
            Manifest(bases=("java-service", "java-legacy")),
            repo="user-service",
            org="myorg",
        )

        assert result.files["pom.xml"] == b"<project>legacy</project>\n"

    def test_logs_info_when_override_occurs(
        self, config_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="ghfanout.builder"):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

        override_logs = [r for r in caplog.records if "overriding" in r.getMessage()]
        assert len(override_logs) == 1
        assert ".gitignore" in override_logs[0].getMessage()
        assert override_logs[0].levelno == logging.INFO

    def test_raises_build_error_for_missing_profile(self, config_repo: Path) -> None:
        with pytest.raises(BuildError, match="base/no-such-profile"):
            build_overlay_files(
                config_repo, Manifest(bases=("no-such-profile",)), repo="user-service", org="myorg"
            )

    def test_allows_config_without_common(self, tmp_path: Path) -> None:
        java_dir = tmp_path / "base" / "java-service"
        java_dir.mkdir(parents=True)
        (java_dir / "pom.xml").write_bytes(b"<project/>\n")

        result = build_overlay_files(
            tmp_path, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert result.files == {"pom.xml": b"<project/>\n"}


class TestGhfanoutIgnore:
    def test_excludes_files_matching_root_ignore_file(self, config_repo: Path) -> None:
        (config_repo / "base" / "java-service" / "README.md").write_bytes(b"internal notes\n")
        (config_repo / ".ghfanoutignore").write_text("README.md\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert "README.md" not in result.files
        assert "pom.xml" in result.files  # unrelated files are still distributed

    def test_patterns_match_relative_to_profile_directory(self, config_repo: Path) -> None:
        # The same rule applies across common/ and every profile, because each
        # file is matched by its path relative to its own profile directory
        common_docs = config_repo / "base" / "common" / "docs"
        common_docs.mkdir()
        (common_docs / "README.md").write_bytes(b"common\n")
        java_docs = config_repo / "base" / "java-service" / "docs"
        java_docs.mkdir()
        (java_docs / "README.md").write_bytes(b"java\n")
        (config_repo / ".ghfanoutignore").write_text("docs/README.md\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert "docs/README.md" not in result.files

    def test_negation_reincludes_excluded_file(self, config_repo: Path) -> None:
        common_docs = config_repo / "base" / "common" / "docs"
        common_docs.mkdir()
        (common_docs / "internal.md").write_bytes(b"internal\n")
        (common_docs / "keep.md").write_bytes(b"keep\n")
        (config_repo / ".ghfanoutignore").write_text("docs/\n!docs/keep.md\n", encoding="utf-8")

        result = build_overlay_files(config_repo, Manifest(), repo="user-service", org="myorg")

        assert "docs/internal.md" not in result.files
        assert result.files["docs/keep.md"] == b"keep\n"

    def test_jinja_files_match_by_pre_strip_name(self, config_repo: Path) -> None:
        # Exclusion happens before rendering: a template that would fail to
        # render (undefined variable) is skipped without raising
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "notes.txt.jinja").write_text("{{ values.missing }}\n", encoding="utf-8")
        (config_repo / ".ghfanoutignore").write_text("notes.txt.jinja\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert "notes.txt" not in result.files

    def test_post_strip_name_does_not_exclude_jinja_file(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "notes.txt.jinja").write_text("{{ repo }}\n", encoding="utf-8")
        (config_repo / ".ghfanoutignore").write_text("notes.txt\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert result.files["notes.txt"] == b"user-service\n"

    def test_ignore_file_inside_profile_has_no_effect(self, config_repo: Path) -> None:
        # Only the file at the config repository root is read; one inside a
        # profile excludes nothing and is distributed like any other file
        java_dir = config_repo / "base" / "java-service"
        (java_dir / ".ghfanoutignore").write_bytes(b"pom.xml\n")

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert "pom.xml" in result.files
        assert result.files[".ghfanoutignore"] == b"pom.xml\n"


class TestTemplateRendering:
    def test_jinja_is_rendered_and_distributed_without_extension(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.jinja").write_text(
            "<artifactId>{{ repo }}</artifactId>\n"
            "<groupId>com.{{ org }}</groupId>\n"
            "<version>{{ values.version }}</version>\n",
            encoding="utf-8",
        )
        manifest = Manifest(bases=("java-service",), values={"version": "1.2.3"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert "pom.xml.jinja" not in result.files
        # Also verify that the trailing newline is preserved (keep_trailing_newline)
        assert result.files["pom.xml"] == (
            b"<artifactId>user-service</artifactId>\n"
            b"<groupId>com.myorg</groupId>\n"
            b"<version>1.2.3</version>\n"
        )

    def test_non_jinja_template_like_syntax_is_copied_as_is(self, config_repo: Path) -> None:
        # Not rendered even though it contains GitHub Actions' ${{ }} or Jinja-like {{ }} syntax
        raw = b"run: echo ${{ secrets.TOKEN }} {{ not_a_template }}\n"
        (config_repo / "base" / "common" / "ci.yml").write_bytes(raw)

        result = build_overlay_files(config_repo, Manifest(), repo="user-service", org="myorg")

        assert result.files["ci.yml"] == raw

    def test_default_filter_provides_fallback_for_undefined_values(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "version.txt.jinja").write_text(
            '{{ values.version | default("0.1.0") }}\n', encoding="utf-8"
        )

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert result.files["version.txt"] == b"0.1.0\n"

    def test_values_supports_nested_value_access(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "group.txt.jinja").write_text("{{ values.maven.groupId }}\n", encoding="utf-8")
        manifest = Manifest(bases=("java-service",), values={"maven": {"groupId": "com.example"}})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["group.txt"] == b"com.example\n"

    def test_jinja_files_are_overridden_last_wins_too(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "app.conf.jinja").write_text("java {{ repo }}\n", encoding="utf-8")
        legacy_dir = config_repo / "base" / "java-legacy"
        legacy_dir.mkdir()
        (legacy_dir / "app.conf.jinja").write_text("legacy {{ repo }}\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo,
            Manifest(bases=("java-service", "java-legacy")),
            repo="user-service",
            org="myorg",
        )

        assert result.files["app.conf"] == b"legacy user-service\n"

    def test_hidden_file_named_jinja_is_not_treated_as_template(self, config_repo: Path) -> None:
        (config_repo / "base" / "common" / ".jinja").write_bytes(b"{{ raw }}\n")

        result = build_overlay_files(config_repo, Manifest(), repo="user-service", org="myorg")

        assert result.files[".jinja"] == b"{{ raw }}\n"

    def test_raises_build_error_for_undefined_variable_reference(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "bad.txt.jinja").write_text("{{ values.missing }}\n", encoding="utf-8")

        with pytest.raises(BuildError, match=re.escape("bad.txt.jinja")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

    def test_raises_build_error_for_template_syntax_error(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "broken.txt.jinja").write_text("{% if %}\n", encoding="utf-8")

        with pytest.raises(BuildError, match=re.escape("broken.txt.jinja")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

    def test_raises_build_error_when_jinja_and_non_jinja_coexist(self, config_repo: Path) -> None:
        # Add pom.xml.jinja where java-service already has pom.xml
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml.jinja").write_text("<x/>\n", encoding="utf-8")

        with pytest.raises(BuildError, match=re.escape("pom.xml")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

    def test_raises_build_error_for_non_utf8_jinja(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "binary.dat.jinja").write_bytes(b"\xff\xfe\x00\x01")

        with pytest.raises(BuildError, match=re.escape("binary.dat.jinja")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )


class TestPathRemapping:
    def test_remaps_distribution_path(self, config_repo: Path) -> None:
        manifest = Manifest(bases=("java-service",), paths={"pom.xml": "services/user/pom.xml"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files == {
            ".gitignore": b"target/\n",
            ".github/CODEOWNERS": b"* @myorg/platform\n",
            "services/user/pom.xml": b"<project/>\n",
        }
        assert result.unmatched_path_sources == frozenset()

    def test_sources_match_by_post_strip_name(self, config_repo: Path) -> None:
        # Sources use the distribution path (after the .jinja suffix is
        # stripped), so templating a file does not break its remap
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.jinja").write_text(
            "<artifactId>{{ repo }}</artifactId>\n", encoding="utf-8"
        )
        manifest = Manifest(bases=("java-service",), paths={"pom.xml": "services/pom.xml"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["services/pom.xml"] == b"<artifactId>user-service</artifactId>\n"
        assert "pom.xml" not in result.files

    def test_destination_ending_in_jinja_is_not_rendered_again(self, config_repo: Path) -> None:
        # Remapping happens after rendering; a destination that happens to end
        # in .jinja is distributed under that name as-is
        manifest = Manifest(bases=("java-service",), paths={"pom.xml": "pom.xml.jinja"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["pom.xml.jinja"] == b"<project/>\n"

    def test_noop_remap_to_same_path_is_allowed(self, config_repo: Path) -> None:
        manifest = Manifest(bases=("java-service",), paths={"pom.xml": "pom.xml"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["pom.xml"] == b"<project/>\n"

    def test_swap_remaps_exchange_contents(self, config_repo: Path) -> None:
        # The collision check ignores files that are themselves remapped away
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "a.txt").write_bytes(b"A\n")
        (java_dir / "b.txt").write_bytes(b"B\n")
        manifest = Manifest(bases=("java-service",), paths={"a.txt": "b.txt", "b.txt": "a.txt"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["a.txt"] == b"B\n"
        assert result.files["b.txt"] == b"A\n"

    def test_chain_remaps_do_not_cascade(self, config_repo: Path) -> None:
        # a -> b and b -> c each move one file; the result of a remap is never
        # remapped again (a's content ends at b, not c)
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "a.txt").write_bytes(b"A\n")
        (java_dir / "b.txt").write_bytes(b"B\n")
        manifest = Manifest(bases=("java-service",), paths={"a.txt": "b.txt", "b.txt": "c.txt"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["b.txt"] == b"A\n"
        assert result.files["c.txt"] == b"B\n"
        assert "a.txt" not in result.files

    def test_destination_matching_ignore_pattern_is_still_distributed(
        self, config_repo: Path
    ) -> None:
        # .ghfanoutignore matches source names before remapping, so a
        # destination that happens to match a pattern is not excluded
        (config_repo / ".ghfanoutignore").write_text("docs/\n", encoding="utf-8")
        manifest = Manifest(bases=("java-service",), paths={"pom.xml": "docs/pom.xml"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["docs/pom.xml"] == b"<project/>\n"

    def test_unmatched_source_is_reported_not_raised(self, config_repo: Path) -> None:
        # build_overlay_files alone does not fail: build_per_variant judges
        # unmatched sources across all variants
        manifest = Manifest(bases=("java-service",), paths={"no-such.txt": "dest.txt"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.unmatched_path_sources == frozenset({"no-such.txt"})
        assert "dest.txt" not in result.files
        assert result.files["pom.xml"] == b"<project/>\n"

    def test_raises_build_error_when_two_sources_map_to_same_destination(
        self, config_repo: Path
    ) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "a.txt").write_bytes(b"A\n")
        manifest = Manifest(
            bases=("java-service",), paths={"a.txt": "same.txt", "pom.xml": "same.txt"}
        )

        with pytest.raises(BuildError, match=re.escape("both map to 'same.txt'")):
            build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

    def test_raises_build_error_when_destination_collides_with_unremapped_file(
        self, config_repo: Path
    ) -> None:
        manifest = Manifest(bases=("java-service",), paths={"pom.xml": ".gitignore"})

        with pytest.raises(BuildError, match=re.escape("collides with a file that is not")):
            build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")


class TestVariantKey:
    def test_different_values_yield_different_key_even_with_same_bases(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            values={"version": "1.0"},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", values={"version": "0.9"}),
            ),
        )
        assert variant_key(manifest, manifest.branches[0]) != variant_key(
            manifest, manifest.branches[1]
        )

    def test_lists_in_values_are_frozen_into_hashable_keys(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            values={"plugins": ["a", "b"]},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="develop", values={"plugins": ["a", "b"]}),
                BranchSpec(name="release-1.x", values={"plugins": ["c"]}),
            ),
        )
        # Lists nested in values are frozen to tuples, so the key stays hashable:
        # identical content shares a variant, different content does not
        assert variant_key(manifest, manifest.branches[0]) == variant_key(
            manifest, manifest.branches[1]
        )
        assert variant_key(manifest, manifest.branches[0]) != variant_key(
            manifest, manifest.branches[2]
        )

    def test_same_effective_bases_and_values_yield_same_key(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            values={"version": "1.0", "maven": {"groupId": "com.example"}},
            branches=(
                BranchSpec(name="main"),
                # Even if explicitly set to the same content as top-level, it's
                # the same variant as long as the effective values match
                BranchSpec(name="develop", bases=("java-service",), values={"version": "1.0"}),
            ),
        )
        assert variant_key(manifest, manifest.branches[0]) == variant_key(
            manifest, manifest.branches[1]
        )

    def test_different_paths_yield_different_key_even_with_same_bases_and_values(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            paths={"pom.xml": "services/pom.xml"},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", paths={"pom.xml": "legacy/pom.xml"}),
            ),
        )
        assert variant_key(manifest, manifest.branches[0]) != variant_key(
            manifest, manifest.branches[1]
        )

    def test_same_effective_paths_yield_same_key(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            paths={"pom.xml": "services/pom.xml"},
            branches=(
                BranchSpec(name="main"),
                # Explicitly overriding with the same content is the same variant
                BranchSpec(name="develop", paths={"pom.xml": "services/pom.xml"}),
            ),
        )
        assert variant_key(manifest, manifest.branches[0]) == variant_key(
            manifest, manifest.branches[1]
        )

    def test_null_removal_changes_effective_paths_and_key(self) -> None:
        manifest = Manifest(
            bases=("java-service",),
            paths={"pom.xml": "services/pom.xml"},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", paths={"pom.xml": None}),
            ),
        )
        assert variant_key(manifest, manifest.branches[0]) != variant_key(
            manifest, manifest.branches[1]
        )


class TestBuildPerVariant:
    def test_builds_only_unique_variants(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.jinja").write_text(
            "<version>{{ values.version }}</version>\n", encoding="utf-8"
        )
        manifest = Manifest(
            bases=("java-service",),
            values={"version": "1.0"},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="develop"),
                BranchSpec(name="release-1.x", values={"version": "0.9"}),
            ),
        )

        builds = build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

        # main and develop share the same variant, so there are only two distinct builds
        assert len(builds) == 2
        main_build = builds[variant_key(manifest, manifest.branches[0])]
        assert main_build is builds[variant_key(manifest, manifest.branches[1])]
        assert main_build.files["pom.xml"] == b"<version>1.0</version>\n"
        release_build = builds[variant_key(manifest, manifest.branches[2])]
        assert release_build.files["pom.xml"] == b"<version>0.9</version>\n"

    def test_builds_single_top_level_combination_when_branches_omitted(
        self, config_repo: Path
    ) -> None:
        manifest = Manifest(bases=("java-service",))

        builds = build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

        assert len(builds) == 1
        # The key for a spec without bases / values overrides matches the
        # top-level combination (verifying that deploy can look it up with
        # the default branch's BranchSpec)
        build = builds[variant_key(manifest, BranchSpec(name="main"))]
        assert build.files["pom.xml"] == b"<project/>\n"

    def test_branch_specific_paths_produce_distinct_builds(self, config_repo: Path) -> None:
        manifest = Manifest(
            bases=("java-service",),
            paths={"pom.xml": "services/pom.xml"},
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", paths={"pom.xml": "legacy/pom.xml"}),
            ),
        )

        builds = build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

        assert len(builds) == 2
        main_build = builds[variant_key(manifest, manifest.branches[0])]
        assert main_build.files["services/pom.xml"] == b"<project/>\n"
        release_build = builds[variant_key(manifest, manifest.branches[1])]
        assert release_build.files["legacy/pom.xml"] == b"<project/>\n"
        assert "services/pom.xml" not in release_build.files

    def test_raises_build_error_for_unmatched_source_without_branches(
        self, config_repo: Path
    ) -> None:
        # With branches omitted there is exactly one variant, so an unmatched
        # source has matched nowhere and is rejected right away
        manifest = Manifest(bases=("java-service",), paths={"typo.txt": "dest.txt"})

        with pytest.raises(BuildError, match=re.escape("typo.txt")):
            build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

    def test_raises_build_error_listing_all_never_matched_sources(self, config_repo: Path) -> None:
        manifest = Manifest(
            bases=("java-service",),
            paths={"typo-b.txt": "x.txt", "typo-a.txt": "y.txt"},
            branches=(BranchSpec(name="main"), BranchSpec(name="develop")),
        )

        with pytest.raises(BuildError, match=re.escape("typo-a.txt, typo-b.txt")):
            build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

    def test_source_missing_in_some_variants_logs_info_instead_of_raising(
        self, config_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # java-service has pom.xml but the docs branch (bases: []) does not:
        # the remap is skipped there, reported as info rather than an error
        manifest = Manifest(
            bases=("java-service",),
            paths={"pom.xml": "services/pom.xml"},
            branches=(BranchSpec(name="main"), BranchSpec(name="docs", bases=())),
        )

        with caplog.at_level(logging.INFO, logger="ghfanout.builder"):
            builds = build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

        main_build = builds[variant_key(manifest, manifest.branches[0])]
        assert main_build.files["services/pom.xml"] == b"<project/>\n"
        docs_build = builds[variant_key(manifest, manifest.branches[1])]
        assert "services/pom.xml" not in docs_build.files
        skip_logs = [r for r in caplog.records if "remap skipped" in r.getMessage()]
        assert len(skip_logs) == 1
        assert "pom.xml" in skip_logs[0].getMessage()

    def test_source_only_in_branch_override_matches_within_its_variant(
        self, config_repo: Path
    ) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "ci.txt").write_bytes(b"ci\n")
        manifest = Manifest(
            bases=("java-service",),
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", paths={"ci.txt": "workflows/ci.txt"}),
            ),
        )

        builds = build_per_variant(config_repo, manifest, repo="user-service", org="myorg")

        main_build = builds[variant_key(manifest, manifest.branches[0])]
        assert main_build.files["ci.txt"] == b"ci\n"
        release_build = builds[variant_key(manifest, manifest.branches[1])]
        assert release_build.files["workflows/ci.txt"] == b"ci\n"

    def test_raises_build_error_for_unmatched_source_only_in_branch_override(
        self, config_repo: Path
    ) -> None:
        # A source appearing only in one branch's override is judged within
        # the variants it applies to — matching nowhere is still an error
        manifest = Manifest(
            bases=("java-service",),
            branches=(
                BranchSpec(name="main"),
                BranchSpec(name="release-1.x", paths={"typo.txt": "x.txt"}),
            ),
        )

        with pytest.raises(BuildError, match=re.escape("typo.txt")):
            build_per_variant(config_repo, manifest, repo="user-service", org="myorg")


class TestWriteBuildOutput:
    def test_writes_output_preserving_nested_structure(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )
        output_dir = tmp_path / "out"

        written = write_build_output(result, output_dir)

        assert sorted(written) == sorted(
            [
                output_dir / ".gitignore",
                output_dir / ".github" / "CODEOWNERS",
                output_dir / "pom.xml",
            ]
        )
        assert (output_dir / ".github" / "CODEOWNERS").read_bytes() == b"* @myorg/platform\n"
        assert (output_dir / ".gitignore").read_bytes() == b"target/\n"

    def test_overwrites_existing_files_without_deleting_unrelated_files(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        (output_dir / ".gitignore").write_bytes(b"stale\n")
        (output_dir / "unrelated.txt").write_bytes(b"keep me\n")

        write_build_output(result, output_dir)

        assert (output_dir / ".gitignore").read_bytes() == b"target/\n"
        assert (output_dir / "unrelated.txt").read_bytes() == b"keep me\n"

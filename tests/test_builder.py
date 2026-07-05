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

    def test_tmpl_files_match_by_pre_strip_name(self, config_repo: Path) -> None:
        # Exclusion happens before rendering: a template that would fail to
        # render (undefined variable) is skipped without raising
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "notes.txt.tmpl").write_text("{{ values.missing }}\n", encoding="utf-8")
        (config_repo / ".ghfanoutignore").write_text("notes.txt.tmpl\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert "notes.txt" not in result.files

    def test_post_strip_name_does_not_exclude_tmpl_file(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "notes.txt.tmpl").write_text("{{ repo }}\n", encoding="utf-8")
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
    def test_tmpl_is_rendered_and_distributed_without_extension(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.tmpl").write_text(
            "<artifactId>{{ repo }}</artifactId>\n"
            "<groupId>com.{{ org }}</groupId>\n"
            "<version>{{ values.version }}</version>\n",
            encoding="utf-8",
        )
        manifest = Manifest(bases=("java-service",), values={"version": "1.2.3"})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert "pom.xml.tmpl" not in result.files
        # Also verify that the trailing newline is preserved (keep_trailing_newline)
        assert result.files["pom.xml"] == (
            b"<artifactId>user-service</artifactId>\n"
            b"<groupId>com.myorg</groupId>\n"
            b"<version>1.2.3</version>\n"
        )

    def test_non_tmpl_template_like_syntax_is_copied_as_is(self, config_repo: Path) -> None:
        # Not rendered even though it contains GitHub Actions' ${{ }} or Jinja-like {{ }} syntax
        raw = b"run: echo ${{ secrets.TOKEN }} {{ not_a_template }}\n"
        (config_repo / "base" / "common" / "ci.yml").write_bytes(raw)

        result = build_overlay_files(config_repo, Manifest(), repo="user-service", org="myorg")

        assert result.files["ci.yml"] == raw

    def test_default_filter_provides_fallback_for_undefined_values(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "version.txt.tmpl").write_text(
            '{{ values.version | default("0.1.0") }}\n', encoding="utf-8"
        )

        result = build_overlay_files(
            config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
        )

        assert result.files["version.txt"] == b"0.1.0\n"

    def test_values_supports_nested_value_access(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "group.txt.tmpl").write_text("{{ values.maven.groupId }}\n", encoding="utf-8")
        manifest = Manifest(bases=("java-service",), values={"maven": {"groupId": "com.example"}})

        result = build_overlay_files(config_repo, manifest, repo="user-service", org="myorg")

        assert result.files["group.txt"] == b"com.example\n"

    def test_tmpl_files_are_overridden_last_wins_too(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "app.conf.tmpl").write_text("java {{ repo }}\n", encoding="utf-8")
        legacy_dir = config_repo / "base" / "java-legacy"
        legacy_dir.mkdir()
        (legacy_dir / "app.conf.tmpl").write_text("legacy {{ repo }}\n", encoding="utf-8")

        result = build_overlay_files(
            config_repo,
            Manifest(bases=("java-service", "java-legacy")),
            repo="user-service",
            org="myorg",
        )

        assert result.files["app.conf"] == b"legacy user-service\n"

    def test_hidden_file_named_tmpl_is_not_treated_as_template(self, config_repo: Path) -> None:
        (config_repo / "base" / "common" / ".tmpl").write_bytes(b"{{ raw }}\n")

        result = build_overlay_files(config_repo, Manifest(), repo="user-service", org="myorg")

        assert result.files[".tmpl"] == b"{{ raw }}\n"

    def test_raises_build_error_for_undefined_variable_reference(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "bad.txt.tmpl").write_text("{{ values.missing }}\n", encoding="utf-8")

        with pytest.raises(BuildError, match=re.escape("bad.txt.tmpl")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

    def test_raises_build_error_for_template_syntax_error(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "broken.txt.tmpl").write_text("{% if %}\n", encoding="utf-8")

        with pytest.raises(BuildError, match=re.escape("broken.txt.tmpl")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

    def test_raises_build_error_when_tmpl_and_non_tmpl_coexist(self, config_repo: Path) -> None:
        # Add pom.xml.tmpl where java-service already has pom.xml
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml.tmpl").write_text("<x/>\n", encoding="utf-8")

        with pytest.raises(BuildError, match=re.escape("pom.xml")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )

    def test_raises_build_error_for_non_utf8_tmpl(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "binary.dat.tmpl").write_bytes(b"\xff\xfe\x00\x01")

        with pytest.raises(BuildError, match=re.escape("binary.dat.tmpl")):
            build_overlay_files(
                config_repo, Manifest(bases=("java-service",)), repo="user-service", org="myorg"
            )


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


class TestBuildPerVariant:
    def test_builds_only_unique_variants(self, config_repo: Path) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.tmpl").write_text(
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

    def test_overwrites_existing_files_and_keeps_unrelated_files(
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

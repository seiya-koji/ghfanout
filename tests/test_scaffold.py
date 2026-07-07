"""Tests for scaffold (scaffold generation for init)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghfanout.errors import ConfigError
from ghfanout.scaffold import PLACEHOLDER_ORG, init_config_repo


class TestInitConfigRepo:
    def test_generates_full_scaffold_and_returns_paths(self, tmp_path: Path) -> None:
        created = init_config_repo(tmp_path)

        rels = sorted(p.relative_to(tmp_path).as_posix() for p in created)
        assert rels == [
            ".ghfanoutignore",
            "base/common/.gitignore",
            "base/java-service/pom.xml.jinja",
            "ghfanout.yaml",
            "overlays/example-service/manifest.yaml",
        ]

    def test_creates_scaffold_in_nonexistent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "new" / "nested"

        created = init_config_repo(target)

        assert (target / "ghfanout.yaml").is_file()
        assert len(created) == 5

    def test_fills_placeholder_when_org_omitted(self, tmp_path: Path) -> None:
        init_config_repo(tmp_path)

        content = (tmp_path / "ghfanout.yaml").read_text(encoding="utf-8")
        assert f"org: {PLACEHOLDER_ORG}" in content

    def test_fills_specified_value_when_org_given(self, tmp_path: Path) -> None:
        init_config_repo(tmp_path, org="myorg")

        content = (tmp_path / "ghfanout.yaml").read_text(encoding="utf-8")
        assert "org: myorg" in content

    def test_raises_error_when_ghfanout_yaml_already_exists(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: existing\n", encoding="utf-8")

        with pytest.raises(ConfigError):
            init_config_repo(tmp_path)

    def test_skips_existing_individual_file_without_overwriting(self, tmp_path: Path) -> None:
        # ghfanout.yaml does not exist yet, but pre-create pom.xml.jinja only
        pom = tmp_path / "base" / "java-service" / "pom.xml.jinja"
        pom.parent.mkdir(parents=True)
        pom.write_bytes(b"<existing/>\n")

        created = init_config_repo(tmp_path)

        # The existing pom.xml.jinja is excluded from the generated list, and its content is kept
        assert pom not in created
        assert pom.read_bytes() == b"<existing/>\n"
        # Other files are generated as usual
        assert (tmp_path / "ghfanout.yaml") in created

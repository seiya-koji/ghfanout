"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def config_repo(tmp_path: Path) -> Path:
    """Build and return a directory tree for a standard config repository.

    - base/common: .gitignore, .github/CODEOWNERS (for verifying nested structures)
    - base/java-service: pom.xml, .gitignore (for verifying conflicts with common)
    - overlays/user-service: bases: [java-service]
    """
    (tmp_path / "ghfanout.yaml").write_text("org: myorg\n", encoding="utf-8")

    common_dir = tmp_path / "base" / "common"
    (common_dir / ".github").mkdir(parents=True)
    (common_dir / ".gitignore").write_bytes(b"*.log\n")
    (common_dir / ".github" / "CODEOWNERS").write_bytes(b"* @myorg/platform\n")

    java_dir = tmp_path / "base" / "java-service"
    java_dir.mkdir(parents=True)
    (java_dir / "pom.xml").write_bytes(b"<project/>\n")
    (java_dir / ".gitignore").write_bytes(b"target/\n")

    overlay_dir = tmp_path / "overlays" / "user-service"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "manifest.yaml").write_text("bases:\n  - java-service\n", encoding="utf-8")

    return tmp_path

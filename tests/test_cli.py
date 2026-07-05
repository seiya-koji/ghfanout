"""Tests for the CLI (Typer app)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from github import GithubException, UnknownObjectException
from typer.testing import CliRunner

from ghfanout.cli import app

runner = CliRunner()


class TestInitCommand:
    def test_generated_scaffold_can_be_built(self, tmp_path: Path) -> None:
        init_result = runner.invoke(app, ["init", str(tmp_path)])

        assert init_result.exit_code == 0, init_result.output
        assert (tmp_path / "ghfanout.yaml").is_file()
        assert (tmp_path / "overlays" / "example-service" / "manifest.yaml").is_file()

        # Verify that the generated scaffold can be built as-is (E2E)
        out_dir = tmp_path / "out"
        build_result = runner.invoke(
            app, ["-C", str(tmp_path), "build", "example-service", "-o", str(out_dir)]
        )

        assert build_result.exit_code == 0, build_result.output
        assert (out_dir / ".gitignore").is_file()
        # pom.xml.tmpl is rendered, and the built-in repo variable expands to the overlay name
        assert not (out_dir / "pom.xml.tmpl").exists()
        pom = (out_dir / "pom.xml").read_text(encoding="utf-8")
        assert "<artifactId>example-service</artifactId>" in pom
        assert "<version>0.1.0</version>" in pom

    def test_org_option_is_reflected_in_ghfanout_yaml(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["init", str(tmp_path), "--org", "myorg"])

        assert result.exit_code == 0, result.output
        assert "org: myorg" in (tmp_path / "ghfanout.yaml").read_text(encoding="utf-8")

    def test_generates_in_current_dir_when_path_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["init"])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "ghfanout.yaml").is_file()

    def test_exits_with_error_when_ghfanout_yaml_already_exists(self, tmp_path: Path) -> None:
        (tmp_path / "ghfanout.yaml").write_text("org: existing\n", encoding="utf-8")

        result = runner.invoke(app, ["init", str(tmp_path)])

        assert result.exit_code == 1
        assert "Error" in result.stderr


class TestBuildCommand:
    def test_writes_build_output_to_specified_directory(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "out"

        result = runner.invoke(
            app, ["-C", str(config_repo), "build", "user-service", "-o", str(output_dir)]
        )

        assert result.exit_code == 0, result.output
        assert "wrote 3 file(s)" in result.stdout
        assert (output_dir / "pom.xml").read_bytes() == b"<project/>\n"
        assert (output_dir / ".gitignore").read_bytes() == b"target/\n"
        assert (output_dir / ".github" / "CODEOWNERS").is_file()

    def test_writes_to_dist_under_config_root_when_output_omitted(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even when run from a subdirectory, the output path is based on the config root, not cwd
        monkeypatch.chdir(config_repo / "overlays" / "user-service")

        result = runner.invoke(app, ["build", "user-service"])

        assert result.exit_code == 0, result.output
        assert (config_repo / "dist" / "user-service" / "pom.xml").is_file()

    def test_output_dir_from_ghfanout_yaml_is_used(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (config_repo / "ghfanout.yaml").write_text(
            "org: myorg\noutput_dir: build-out\n", encoding="utf-8"
        )
        monkeypatch.chdir(config_repo)

        result = runner.invoke(app, ["build", "user-service"])

        assert result.exit_code == 0, result.output
        assert (config_repo / "build-out" / "user-service" / "pom.xml").is_file()

    def test_finds_config_root_when_run_from_subdirectory(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(config_repo / "overlays" / "user-service")

        result = runner.invoke(app, ["build", "user-service"])

        assert result.exit_code == 0, result.output

    def test_writes_per_branch_directory_when_bases_overridden_per_branch(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        legacy_dir = config_repo / "base" / "java-legacy"
        legacy_dir.mkdir()
        (legacy_dir / "pom.xml").write_bytes(b"<legacy/>\n")
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            "branches:\n  - main\n  - name: release-1.x\n    bases: [java-legacy]\n",
            encoding="utf-8",
        )
        output_dir = tmp_path / "out"

        result = runner.invoke(
            app, ["-C", str(config_repo), "build", "user-service", "-o", str(output_dir)]
        )

        assert result.exit_code == 0, result.output
        # Print a one-line summary per branch
        assert f"user-service@main: wrote 3 file(s) to {output_dir / 'main'}" in result.stdout
        assert (
            f"user-service@release-1.x: wrote 3 file(s) to {output_dir / 'release-1.x'}"
            in result.stdout
        )
        # main uses the top-level bases (java-service version), release-1.x uses java-legacy
        assert (output_dir / "main" / "pom.xml").read_bytes() == b"<project/>\n"
        assert (output_dir / "release-1.x" / "pom.xml").read_bytes() == b"<legacy/>\n"

    def test_writes_per_branch_directory_when_values_overridden_per_branch(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        java_dir = config_repo / "base" / "java-service"
        (java_dir / "pom.xml").unlink()
        (java_dir / "pom.xml.tmpl").write_text(
            "<version>{{ values.version }}</version>\n", encoding="utf-8"
        )
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\n"
            'values:\n  version: "1.0"\n'
            'branches:\n  - main\n  - name: release-1.x\n    values:\n      version: "0.9"\n',
            encoding="utf-8",
        )
        output_dir = tmp_path / "out"

        result = runner.invoke(
            app, ["-C", str(config_repo), "build", "user-service", "-o", str(output_dir)]
        )

        assert result.exit_code == 0, result.output
        # main uses the top-level values (1.0), release-1.x uses the branch-specific override (0.9)
        assert (output_dir / "main" / "pom.xml").read_bytes() == b"<version>1.0</version>\n"
        assert (output_dir / "release-1.x" / "pom.xml").read_bytes() == b"<version>0.9</version>\n"

    def test_writes_single_output_when_no_branch_specific_override_exists(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        overlay_dir = config_repo / "overlays" / "user-service"
        (overlay_dir / "manifest.yaml").write_text(
            "bases:\n  - java-service\nbranches:\n  - main\n  - release-1.x\n",
            encoding="utf-8",
        )
        output_dir = tmp_path / "out"

        result = runner.invoke(
            app, ["-C", str(config_repo), "build", "user-service", "-o", str(output_dir)]
        )

        assert result.exit_code == 0, result.output
        assert f"user-service: wrote 3 file(s) to {output_dir}" in result.stdout
        # No per-branch subdirectory is created (content is identical across all branches)
        assert (output_dir / "pom.xml").is_file()
        assert not (output_dir / "main").exists()

    def test_builds_all_overlays_when_overlay_omitted(self, config_repo: Path) -> None:
        api_dir = config_repo / "overlays" / "api-gateway"
        api_dir.mkdir()
        (api_dir / "manifest.yaml").write_text("bases: []\n", encoding="utf-8")

        result = runner.invoke(app, ["-C", str(config_repo), "build"])

        assert result.exit_code == 0, result.output
        # user-service builds/composes java-service, api-gateway is common only
        assert (config_repo / "dist" / "user-service" / "pom.xml").is_file()
        assert (config_repo / "dist" / "api-gateway" / ".gitignore").is_file()
        assert not (config_repo / "dist" / "api-gateway" / "pom.xml").exists()
        assert "user-service: wrote 3 file(s)" in result.stdout
        assert "api-gateway: wrote 2 file(s)" in result.stdout

    def test_output_option_becomes_parent_dir_for_each_repo_when_overlay_omitted(
        self, config_repo: Path, tmp_path: Path
    ) -> None:
        api_dir = config_repo / "overlays" / "api-gateway"
        api_dir.mkdir()
        (api_dir / "manifest.yaml").write_text("bases: []\n", encoding="utf-8")
        output_dir = tmp_path / "out"

        result = runner.invoke(app, ["-C", str(config_repo), "build", "-o", str(output_dir)])

        assert result.exit_code == 0, result.output
        assert (output_dir / "user-service" / "pom.xml").is_file()
        assert (output_dir / "api-gateway" / ".gitignore").is_file()

    def test_builds_all_and_exits_nonzero_when_one_overlay_fails(self, config_repo: Path) -> None:
        # Break api-gateway (processed first) and let user-service succeed
        api_dir = config_repo / "overlays" / "api-gateway"
        api_dir.mkdir()
        (api_dir / "manifest.yaml").write_text("bases: [no-such-base]\n", encoding="utf-8")

        result = runner.invoke(app, ["-C", str(config_repo), "build"])

        assert result.exit_code == 1
        # user-service is still built even after the failed api-gateway
        assert (config_repo / "dist" / "user-service" / "pom.xml").is_file()
        assert "api-gateway" in result.stderr

    def test_shows_message_when_no_build_targets_exist(self, config_repo: Path) -> None:
        (config_repo / "overlays" / "user-service" / "manifest.yaml").unlink()

        result = runner.invoke(app, ["-C", str(config_repo), "build"])

        assert result.exit_code == 0, result.output
        assert "No build targets found" in result.stdout

    def test_exits_with_error_for_nonexistent_overlay(self, config_repo: Path) -> None:
        result = runner.invoke(app, ["-C", str(config_repo), "build", "no-such-service"])

        assert result.exit_code == 1
        assert "Error" in result.stderr

    def test_exits_with_error_when_config_root_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["build", "user-service"])

        assert result.exit_code == 1
        assert "ghfanout.yaml" in result.stderr

    def test_exits_with_error_when_config_dir_lacks_ghfanout_yaml(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["-C", str(tmp_path), "build", "user-service"])

        assert result.exit_code == 1
        assert "--config-dir" in result.stderr


def make_dry_runnable_repo(full_name: str) -> MagicMock:
    """Return a Repository mock where all files are new (i.e., there is a diff)."""
    repo = MagicMock(name=f"Repository({full_name})")
    repo.full_name = full_name
    repo.default_branch = "main"
    repo.get_contents.side_effect = UnknownObjectException(404, {"message": "Not Found"}, None)
    return repo


class TestDeployCommand:
    def test_specifying_overlay_and_all_together_is_an_error(self, config_repo: Path) -> None:
        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service", "--all"])
        assert result.exit_code == 1
        assert "--all" in result.stderr

    def test_specifying_neither_overlay_nor_all_is_an_error(self, config_repo: Path) -> None:
        result = runner.invoke(app, ["-C", str(config_repo), "deploy"])
        assert result.exit_code == 1
        assert "--all" in result.stderr

    def test_dry_run_shows_diff(self, config_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = make_dry_runnable_repo("myorg/user-service")
        gh = MagicMock(name="Github")
        gh.get_repo.return_value = repo
        monkeypatch.setattr("ghfanout.cli.create_github_client", lambda _config: gh)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "[dry-run] myorg/user-service@main:" in result.stdout
        assert "+ pom.xml (new)" in result.stdout
        repo.create_pull.assert_not_called()

    def test_shows_direct_push_result_when_deploy_mode_is_push(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (config_repo / "ghfanout.yaml").write_text(
            "org: myorg\ndeploy_mode: push\n", encoding="utf-8"
        )
        repo = make_dry_runnable_repo("myorg/user-service")
        # InputGitTreeElement requires sha to be a string, so explicitly set the blob and commit sha
        repo.create_git_blob.return_value.sha = "blob-sha"
        repo.create_git_commit.return_value.sha = "new-commit-sha"
        gh = MagicMock(name="Github")
        gh.get_repo.return_value = repo
        monkeypatch.setattr("ghfanout.cli.create_github_client", lambda _config: gh)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service"])

        assert result.exit_code == 0, result.output
        assert "Pushed directly: new-commit-sha" in result.stdout
        repo.create_pull.assert_not_called()

    def test_shows_no_changes_when_branch_is_up_to_date(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every built file already exists on main with identical content
        built_files = {
            ".github/CODEOWNERS": b"* @myorg/platform\n",
            ".gitignore": b"target/\n",
            "pom.xml": b"<project/>\n",
        }
        repo = MagicMock(name="Repository(myorg/user-service)")
        repo.full_name = "myorg/user-service"
        repo.default_branch = "main"

        def get_contents(path: str, ref: str) -> MagicMock:
            content = MagicMock(name=f"ContentFile({path}@{ref})")
            content.decoded_content = built_files[path]
            return content

        repo.get_contents.side_effect = get_contents
        gh = MagicMock(name="Github")
        gh.get_repo.return_value = repo
        monkeypatch.setattr("ghfanout.cli.create_github_client", lambda _config: gh)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service"])

        assert result.exit_code == 0, result.output
        assert "myorg/user-service@main: no changes" in result.stdout
        repo.create_pull.assert_not_called()

    def test_shows_updated_file_and_created_pr_url(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # .gitignore exists with differing content (updated); everything else is new
        repo = make_dry_runnable_repo("myorg/user-service")

        def get_contents(path: str, ref: str) -> MagicMock:
            if path != ".gitignore":
                raise UnknownObjectException(404, {"message": "Not Found"}, None)
            content = MagicMock(name=f"ContentFile({path}@{ref})")
            content.decoded_content = b"*.log\n"
            return content

        repo.get_contents.side_effect = get_contents
        repo.create_git_blob.return_value.sha = "blob-sha"
        repo.create_git_commit.return_value.sha = "new-commit-sha"
        repo.get_pulls.return_value = []
        repo.create_pull.return_value.html_url = "https://github.com/myorg/user-service/pull/1"
        gh = MagicMock(name="Github")
        gh.get_repo.return_value = repo
        monkeypatch.setattr("ghfanout.cli.create_github_client", lambda _config: gh)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service"])

        assert result.exit_code == 0, result.output
        assert "~ .gitignore (updated)" in result.stdout
        assert "=> Created PR: https://github.com/myorg/user-service/pull/1" in result.stdout

    def test_shows_updated_existing_pr_when_pr_is_reused(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_dry_runnable_repo("myorg/user-service")
        repo.create_git_blob.return_value.sha = "blob-sha"
        repo.create_git_commit.return_value.sha = "new-commit-sha"
        existing_pr = MagicMock(name="PullRequest(existing)")
        existing_pr.html_url = "https://github.com/myorg/user-service/pull/7"
        repo.get_pulls.return_value = [existing_pr]
        gh = MagicMock(name="Github")
        gh.get_repo.return_value = repo
        monkeypatch.setattr("ghfanout.cli.create_github_client", lambda _config: gh)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service"])

        assert result.exit_code == 0, result.output
        assert (
            "=> Updated existing PR: https://github.com/myorg/user-service/pull/7" in result.stdout
        )
        repo.create_pull.assert_not_called()

    def test_all_runs_all_and_exits_nonzero_when_one_fails(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Break api-gateway (processed first) and let user-service succeed
        api_dir = config_repo / "overlays" / "api-gateway"
        api_dir.mkdir()
        (api_dir / "manifest.yaml").write_text("bases: []\n", encoding="utf-8")

        good_repo = make_dry_runnable_repo("myorg/user-service")

        def get_repo(full_name: str) -> MagicMock:
            if full_name == "myorg/api-gateway":
                raise GithubException(403, {"message": "Forbidden"}, None)
            return good_repo

        gh = MagicMock(name="Github")
        gh.get_repo.side_effect = get_repo
        monkeypatch.setattr("ghfanout.cli.create_github_client", lambda _config: gh)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "--all", "--dry-run"])

        assert result.exit_code == 1
        # user-service is still processed even after the failed api-gateway
        assert "[dry-run] myorg/user-service@main:" in result.stdout
        assert "api-gateway" in result.stderr

    def test_exits_with_error_when_token_not_set(
        self, config_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GHFANOUT_TOKEN", raising=False)

        result = runner.invoke(app, ["-C", str(config_repo), "deploy", "user-service"])

        assert result.exit_code == 1
        assert "GHFANOUT_TOKEN" in result.stderr

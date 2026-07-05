"""Tests for the github_client module (PAT / GitHub App authentication, Enterprise support)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from github import Auth, GithubException

from ghfanout.config import AppAuthConfig, RootConfig
from ghfanout.errors import DeployError
from ghfanout.github_client import (
    APP_ID_ENV_VAR,
    APP_INSTALLATION_ID_ENV_VAR,
    APP_PRIVATE_KEY_ENV_VAR,
    APP_PRIVATE_KEY_FILE_ENV_VAR,
    TOKEN_ENV_VAR,
    create_github_client,
)

# AppAuth doesn't parse the key when the client is created, so a dummy PEM works for testing
DUMMY_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"  # pragma: allowlist secret
    "dummy\n"
    "-----END RSA PRIVATE KEY-----\n"
)


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all auth-related environment variables so they don't affect test results."""
    for name in (
        TOKEN_ENV_VAR,
        APP_ID_ENV_VAR,
        APP_PRIVATE_KEY_ENV_VAR,
        APP_PRIVATE_KEY_FILE_ENV_VAR,
        APP_INSTALLATION_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_token_raises_deploy_error() -> None:
    with pytest.raises(DeployError, match=TOKEN_ENV_VAR):
        create_github_client(RootConfig(org="myorg"))


def test_github_com_does_not_set_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    github_class = MagicMock()
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)

    create_github_client(RootConfig(org="myorg"))

    assert github_class.call_count == 1
    kwargs = github_class.call_args.kwargs
    assert "base_url" not in kwargs
    assert kwargs["auth"].token == "test-token"


def test_enterprise_host_builds_api_v3_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "enterprise-token")
    github_class = MagicMock()
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)

    create_github_client(RootConfig(org="myorg", host="github.example.com"))

    kwargs = github_class.call_args.kwargs
    assert kwargs["base_url"] == "https://github.example.com/api/v3"
    assert kwargs["auth"].token == "enterprise-token"


def _set_app_env(monkeypatch: pytest.MonkeyPatch, installation_id: str | None = "789") -> None:
    """Set the standard set of environment variables for GitHub App authentication."""
    monkeypatch.setenv(APP_ID_ENV_VAR, "123456")
    monkeypatch.setenv(APP_PRIVATE_KEY_ENV_VAR, DUMMY_PEM)
    if installation_id is not None:
        monkeypatch.setenv(APP_INSTALLATION_ID_ENV_VAR, installation_id)


def test_app_env_vars_authenticate_with_app_installation_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_app_env(monkeypatch)
    github_class = MagicMock()
    integration_class = MagicMock()
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)
    monkeypatch.setattr("ghfanout.github_client.GithubIntegration", integration_class)

    create_github_client(RootConfig(org="myorg"))

    auth = github_class.call_args.kwargs["auth"]
    assert isinstance(auth, Auth.AppInstallationAuth)
    assert auth.app_id == "123456"
    assert auth.installation_id == 789
    # When installation ID is explicitly specified, auto-resolution via the API is skipped
    integration_class.assert_not_called()


def test_omitted_installation_id_is_resolved_from_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_app_env(monkeypatch, installation_id=None)
    github_class = MagicMock()
    integration_class = MagicMock()
    integration_class.return_value.get_org_installation.return_value.id = 42
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)
    monkeypatch.setattr("ghfanout.github_client.GithubIntegration", integration_class)

    create_github_client(RootConfig(org="myorg"))

    integration_class.return_value.get_org_installation.assert_called_once_with("myorg")
    assert "base_url" not in integration_class.call_args.kwargs
    auth = github_class.call_args.kwargs["auth"]
    assert isinstance(auth, Auth.AppInstallationAuth)
    assert auth.installation_id == 42


def test_loads_pem_from_private_key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(DUMMY_PEM, encoding="utf-8")
    monkeypatch.setenv(APP_ID_ENV_VAR, "123456")
    monkeypatch.setenv(APP_PRIVATE_KEY_FILE_ENV_VAR, str(key_path))
    monkeypatch.setenv(APP_INSTALLATION_ID_ENV_VAR, "789")
    github_class = MagicMock()
    # AppAuth doesn't retain the private key, so use a spy (wraps) to verify the value passed in.
    # Overwriting github.Auth directly would break PyGithub's internal isinstance checks,
    # so only replace the Auth namespace as seen from github_client
    auth_module = MagicMock()
    auth_module.AppAuth = MagicMock(wraps=Auth.AppAuth)
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)
    monkeypatch.setattr("ghfanout.github_client.Auth", auth_module)

    create_github_client(RootConfig(org="myorg"))

    auth_module.AppAuth.assert_called_once_with("123456", DUMMY_PEM)
    auth = github_class.call_args.kwargs["auth"]
    assert isinstance(auth, Auth.AppInstallationAuth)


def test_app_auth_takes_priority_over_pat_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "pat-token")
    _set_app_env(monkeypatch)
    github_class = MagicMock()
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)

    create_github_client(RootConfig(org="myorg"))

    auth = github_class.call_args.kwargs["auth"]
    assert isinstance(auth, Auth.AppInstallationAuth)


def test_enterprise_host_uses_api_v3_base_url_for_app_auth_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_app_env(monkeypatch, installation_id=None)
    github_class = MagicMock()
    integration_class = MagicMock()
    integration_class.return_value.get_org_installation.return_value.id = 42
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)
    monkeypatch.setattr("ghfanout.github_client.GithubIntegration", integration_class)

    create_github_client(RootConfig(org="myorg", host="github.example.com"))

    expected_base_url = "https://github.example.com/api/v3"
    assert integration_class.call_args.kwargs["base_url"] == expected_base_url
    assert github_class.call_args.kwargs["base_url"] == expected_base_url


def test_app_id_without_private_key_raises_deploy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(APP_ID_ENV_VAR, "123456")

    with pytest.raises(DeployError, match=APP_PRIVATE_KEY_FILE_ENV_VAR):
        create_github_client(RootConfig(org="myorg"))


def test_both_private_key_env_vars_set_raises_deploy_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(DUMMY_PEM, encoding="utf-8")
    monkeypatch.setenv(APP_ID_ENV_VAR, "123456")
    monkeypatch.setenv(APP_PRIVATE_KEY_ENV_VAR, DUMMY_PEM)
    monkeypatch.setenv(APP_PRIVATE_KEY_FILE_ENV_VAR, str(key_path))

    with pytest.raises(DeployError, match="only one"):
        create_github_client(RootConfig(org="myorg"))


def test_missing_private_key_file_raises_deploy_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(APP_ID_ENV_VAR, "123456")
    monkeypatch.setenv(APP_PRIVATE_KEY_FILE_ENV_VAR, str(tmp_path / "missing.pem"))

    with pytest.raises(DeployError, match="not found"):
        create_github_client(RootConfig(org="myorg"))


def test_non_numeric_installation_id_raises_deploy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_app_env(monkeypatch, installation_id="not-a-number")

    with pytest.raises(DeployError, match=APP_INSTALLATION_ID_ENV_VAR):
        create_github_client(RootConfig(org="myorg"))


def test_app_auth_succeeds_with_yaml_auth_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # GitHub App authentication succeeds using only the YAML auth settings, without env vars
    key_path = tmp_path / "app.pem"
    key_path.write_text(DUMMY_PEM, encoding="utf-8")
    github_class = MagicMock()
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)

    config = RootConfig(
        org="myorg",
        auth=AppAuthConfig(app_id="123456", installation_id=789, private_key_file=key_path),
    )
    create_github_client(config)

    auth = github_class.call_args.kwargs["auth"]
    assert isinstance(auth, Auth.AppInstallationAuth)
    assert auth.app_id == "123456"
    assert auth.installation_id == 789


def test_env_vars_take_priority_over_yaml_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_app_env(monkeypatch)
    github_class = MagicMock()
    monkeypatch.setattr("ghfanout.github_client.Github", github_class)

    # The YAML-side private key points to a nonexistent path, but it's not read because
    # env vars take priority
    config = RootConfig(
        org="myorg",
        auth=AppAuthConfig(
            app_id="999999", installation_id=111, private_key_file=tmp_path / "unused.pem"
        ),
    )
    create_github_client(config)

    auth = github_class.call_args.kwargs["auth"]
    assert auth.app_id == "123456"
    assert auth.installation_id == 789


def test_yaml_private_key_file_missing_raises_deploy_error(tmp_path: Path) -> None:
    config = RootConfig(
        org="myorg",
        auth=AppAuthConfig(app_id="123456", private_key_file=tmp_path / "missing.pem"),
    )
    with pytest.raises(DeployError, match="not found"):
        create_github_client(config)


def test_yaml_auth_without_private_key_raises_deploy_error() -> None:
    config = RootConfig(org="myorg", auth=AppAuthConfig(app_id="123456"))
    with pytest.raises(DeployError, match="private_key_file"):
        create_github_client(config)


def test_app_not_installed_on_org_raises_deploy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_app_env(monkeypatch, installation_id=None)
    integration_class = MagicMock()
    integration_class.return_value.get_org_installation.side_effect = GithubException(
        404, {"message": "Not Found"}, None
    )
    monkeypatch.setattr("ghfanout.github_client.GithubIntegration", integration_class)

    with pytest.raises(DeployError, match="installation"):
        create_github_client(RootConfig(org="myorg"))

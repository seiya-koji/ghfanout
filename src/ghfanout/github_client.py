"""Create a PyGithub client (supports PAT / GitHub App authentication and GitHub Enterprise)."""

from __future__ import annotations

import os
from pathlib import Path

from github import Auth, Github, GithubException, GithubIntegration

from ghfanout.config import RootConfig
from ghfanout.errors import DeployError

TOKEN_ENV_VAR = "GHFANOUT_TOKEN"  # noqa: S105 -- this is the *name* of an env var, not a secret value
APP_ID_ENV_VAR = "GHFANOUT_APP_ID"
APP_PRIVATE_KEY_ENV_VAR = "GHFANOUT_APP_PRIVATE_KEY"  # pragma: allowlist secret
APP_PRIVATE_KEY_FILE_ENV_VAR = "GHFANOUT_APP_PRIVATE_KEY_FILE"  # pragma: allowlist secret
APP_INSTALLATION_ID_ENV_VAR = "GHFANOUT_APP_INSTALLATION_ID"


def _base_url(root_config: RootConfig) -> str | None:
    """Return the API base_url for GitHub Enterprise Server (None for github.com)."""
    if root_config.is_enterprise:
        return f"https://{root_config.host}/api/v3"
    return None


def _load_app_private_key(root_config: RootConfig) -> str:
    """Load the GitHub App private key (PEM) from an env var or the ghfanout.yaml auth settings."""
    key = os.environ.get(APP_PRIVATE_KEY_ENV_VAR)
    key_file = os.environ.get(APP_PRIVATE_KEY_FILE_ENV_VAR)
    if key and key_file:
        raise DeployError(
            f"Environment variables {APP_PRIVATE_KEY_ENV_VAR} and {APP_PRIVATE_KEY_FILE_ENV_VAR} "
            "cannot both be set. Please set only one of them."
        )
    if key:
        return key
    path = Path(key_file).expanduser() if key_file else root_config.auth.private_key_file
    if path is None:
        raise DeployError(
            f"GitHub App authentication requires setting the environment variable "
            f"{APP_PRIVATE_KEY_FILE_ENV_VAR} (path to the private key PEM file) or "
            f"{APP_PRIVATE_KEY_ENV_VAR} (PEM string), or auth.private_key_file in ghfanout.yaml."
        )
    if not path.is_file():
        raise DeployError(f"Private key file {path} was not found.")
    return path.read_text(encoding="utf-8")


def _resolve_installation_id(app_auth: Auth.AppAuth, root_config: RootConfig) -> int:
    """Get the installation ID from an env var or ghfanout.yaml; auto-resolve from org if unset."""
    raw = os.environ.get(APP_INSTALLATION_ID_ENV_VAR)
    if raw is not None:
        if not raw.isdigit():
            raise DeployError(
                f"Environment variable {APP_INSTALLATION_ID_ENV_VAR} must be numeric: {raw!r}"
            )
        return int(raw)
    if root_config.auth.installation_id is not None:
        return root_config.auth.installation_id

    base_url = _base_url(root_config)
    if base_url is not None:
        integration = GithubIntegration(base_url=base_url, auth=app_auth)
    else:
        integration = GithubIntegration(auth=app_auth)
    try:
        installation_id: int = integration.get_org_installation(root_config.org).id
    except GithubException as exc:
        raise DeployError(
            f"Failed to get the GitHub App installation for org '{root_config.org}'. "
            f"Check that the App is installed on the org: {exc}"
        ) from exc
    return installation_id


def _resolve_auth(root_config: RootConfig) -> Auth.Auth:
    """Determine the auth method from env vars and ghfanout.yaml (App auth wins over PAT)."""
    app_id = os.environ.get(APP_ID_ENV_VAR) or root_config.auth.app_id
    if app_id:
        app_auth = Auth.AppAuth(app_id, _load_app_private_key(root_config))
        return app_auth.get_installation_auth(_resolve_installation_id(app_auth, root_config))

    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise DeployError(
            f"Environment variable {TOKEN_ENV_VAR} is not set. "
            "Please set a Personal Access Token for the target host "
            f"(to use GitHub App authentication instead, set environment variables such as "
            f"{APP_ID_ENV_VAR}, or configure auth in ghfanout.yaml)."
        )
    return Auth.Token(token)


def create_github_client(root_config: RootConfig) -> Github:
    """Create a PyGithub client authenticated for the configured host.

    The auth method is resolved from environment variables and ghfanout.yaml's
    auth: section (GitHub App auth wins over PAT). For a host other than
    github.com (GitHub Enterprise Server), the client connects to
    https://<host>/api/v3.

    Returns:
        An authenticated client.

    Raises:
        DeployError: If no usable credentials are configured.
    """
    auth = _resolve_auth(root_config)
    base_url = _base_url(root_config)
    if base_url is not None:
        return Github(base_url=base_url, auth=auth)
    return Github(auth=auth)

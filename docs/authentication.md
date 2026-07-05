# Authentication

ghfanout supports two authentication methods: **PAT** (Personal Access Token) and **GitHub App**.

## Which method is used?

If an App ID is set — via the environment variable `GHFANOUT_APP_ID` or `auth.app_id` in `ghfanout.yaml` — GitHub App authentication is used; otherwise PAT authentication is used. If both are configured, GitHub App authentication takes priority.

## Secrets are environment-variables-only

**Secrets** such as tokens or the contents of a private key **can only be passed via environment variables**. Non-secret GitHub App settings (App ID, private key file path, installation ID) can also be written under `auth:` in `ghfanout.yaml`, so everything except secrets can be self-contained within the config repository. When the same item is set in both an environment variable and YAML, the environment variable takes priority.

## PAT authentication

Set your Personal Access Token in the environment variable `GHFANOUT_TOKEN`:

```bash
export GHFANOUT_TOKEN=ghp_xxxx
```

- Requires permissions equivalent to `contents: write` / `pull_requests: write` on the target repositories
- If `host` is a GitHub Enterprise instance, you need a PAT issued on that host
- GitHub Actions' `GITHUB_TOKEN` cannot be used, since it cannot access other repositories

## GitHub App authentication

This method operates as a GitHub App installed on the org. Compared to a long-lived PAT it is safer: access can be scoped to just the target repositories and the required permissions, and tokens automatically expire after a short time (renewal is handled automatically within the tool). Since it is not tied to a personal account, it is also well-suited to shared environments such as CI.

### Setup

1. Create a GitHub App and grant it **Contents: Read and write** and **Pull requests: Read and write** permissions
2. Install the App on the target repositories in your `org`
3. Configure the App ID and private key, via environment variables or YAML

When configuring via environment variables:

```bash
export GHFANOUT_APP_ID=123456
export GHFANOUT_APP_PRIVATE_KEY_FILE=/path/to/app-private-key.pem
```

When configuring via `auth:` in `ghfanout.yaml` (the private key file path is not a secret, so it can be written in YAML):

```yaml
auth:
  app_id: 123456
  private_key_file: ~/.config/ghfanout/app.pem   # Recommended to be outside the config repository to prevent accidental commits
```

### Settings reference

| Environment variable | `auth:` in `ghfanout.yaml` | Description |
| --- | --- | --- |
| `GHFANOUT_APP_ID` | `app_id` | **Required**. The GitHub App's App ID (or Client ID) |
| `GHFANOUT_APP_PRIVATE_KEY_FILE` | `private_key_file` | Path to the private key PEM file (for local runs). `~` is supported in both. A relative path in the YAML key resolves against the config repository root; a relative path in the environment variable resolves against the current directory |
| `GHFANOUT_APP_PRIVATE_KEY` | — (environment variable only) | The private key PEM string itself (for CI secrets) |
| `GHFANOUT_APP_INSTALLATION_ID` | `installation_id` | Optional. The installation ID. Resolved automatically from `org` if omitted |

### Notes

- The private key must come from exactly one source: `GHFANOUT_APP_PRIVATE_KEY_FILE`, `GHFANOUT_APP_PRIVATE_KEY`, or `auth.private_key_file` in YAML. Setting both environment variables is an error, and so is setting none of the three
- The private key contents (PEM string) cannot be written in YAML. The `private_key_file` path itself is not a secret, but it is recommended to keep the key file outside the config repository
- Automatic installation ID resolution requires the App to be installed on the `org`; if it is not, deploy stops with an error. When set explicitly, `GHFANOUT_APP_INSTALLATION_ID` must be numeric

# CLI Reference

```text
ghfanout [GLOBAL OPTIONS] COMMAND [ARGS]
```

## Global options

| Option | Description |
| --- | --- |
| `-C, --config-dir <dir>` | Explicitly specify the config repository root (defaults to searching upward from the current directory for `ghfanout.yaml`) |
| `-v, --verbose` | Output verbose (DEBUG) logs |
| `-V, --version` | Show the version and exit |

## init

Generate a config repository scaffold, with a working sample:

```bash
ghfanout init                          # Generate into the current directory
ghfanout init ./config --org myorg     # Specify the target directory and org
```

- Generates `ghfanout.yaml`, `.ghfanoutignore`, `base/common/.gitignore`, `base/java-service/pom.xml.jinja`, and `overlays/example-service/manifest.yaml` — you can try a build right after generating
- Fails if `ghfanout.yaml` already exists in the target directory; individual scaffold files that already exist are skipped without being overwritten

## build

Build (compose) base and overlay locally — no git operations, no network access. Use it to check exactly what would be distributed:

```bash
ghfanout build                         # Build all repositories under overlays/ (output: <config root>/dist/<overlay>/)
ghfanout build user-service            # Output: <config root>/dist/user-service/ (can be changed via output_dir)
ghfanout build user-service -o /tmp/x  # Specify the output directory (takes precedence over ghfanout.yaml's output_dir)
ghfanout build -o /tmp/x               # Output all repositories to /tmp/x/<overlay>/
ghfanout build user-service --detail   # List every distributed file with its origin profile and transformations
ghfanout build --json                  # Print a machine-readable JSON report instead (for CI or chat integrations)
```

Behavior:

- The output directory is wiped before each build, so files removed from your base profiles are not left behind
- Files matched by the config repository's `.ghfanoutignore` are excluded from the output — see [Configuration](configuration.md#ghfanoutignore)
- Running `build` with the overlay omitted builds all repositories under `overlays/`. Like `deploy --all`, it does not stop on a single failure — it tries all of them before exiting non-zero
- Building an overlay that has a per-branch `bases`, `values`, or `paths` override writes output per branch to `<output>/<branch name>/` (without an override, a single set is written directly under `<output>/`)
- Template (`*.jinja`) rendering errors, such as referencing an undefined variable, cause the build to fail ([Templates](templates.md))

### Output

Each overlay prints one line showing where it was written and how many files it contains, followed by a trailing `Summary` across every targeted overlay:

```text
user-service -> dist/user-service (3 files: 1 rendered, 1 remapped, 1 override)
api-gateway -> dist/api-gateway (2 files)

Summary
  2 built  user-service, api-gateway
```

- The counts in parentheses break down the transformations: `rendered` (from a `*.jinja` template), `remapped` (moved by a `paths:` entry), and `override` (replaced a lower-precedence profile's file at the same path). Zero counts are omitted
- With a per-branch build, the heading becomes `user-service@main -> <output>/main (...)`
- When some overlays fail, the summary adds a `failed` line with their names, and the command exits non-zero

With `--detail`, every distributed file is listed with its origin profile and transformations:

```text
user-service -> dist/user-service (3 files)
  .github/CODEOWNERS  from common
  .gitignore          from java-service (overrides common)
  services/pom.xml    from java-service (rendered, remapped)
```

With `--json`, stdout carries a single JSON report instead of the human-readable output (logs still go to stderr). The report always includes the per-file detail, so `--detail` cannot be combined with `--json`:

```json
{
  "command": "build",
  "overlays": [
    {
      "name": "user-service",
      "variants": [
        {
          "branch": null,
          "output_dir": "dist/user-service",
          "files": [
            {
              "path": "services/pom.xml",
              "from": "java-service",
              "overrides": null,
              "rendered": true,
              "remapped_from": "pom.xml"
            }
          ]
        }
      ]
    }
  ],
  "summary": {"built": ["user-service"], "failed": []}
}
```

## deploy

Compare the build result against the target repository's current state, and open a PR (or push directly) only if there is a diff:

```bash
ghfanout deploy user-service --dry-run   # Show only the diff, change nothing (safety check)
ghfanout deploy user-service             # Create branch ghfanout/update-<branch> and open a PR
ghfanout deploy --all                    # Deploy to all repositories under overlays/
ghfanout deploy --all --json             # Print a machine-readable JSON report instead (for CI or chat integrations)
```

Requires authentication — see [Authentication](authentication.md). Specify either an overlay name or `--all` (not both).

Behavior:

- The diff is computed for each target branch, and a PR is created only for branches that have a diff (branches without a diff report `no changes`)
- If the working branch `ghfanout/update-<branch>` already exists, it is recreated with the latest commit; if there is an existing open PR, it is reused instead of creating a new one
- If composition yields zero files to distribute (for example, everything is excluded by `.ghfanoutignore`), the repository — or the affected branch — is skipped with a warning instead of failing
- With `--all`, a single repository's failure does not stop the run; all repositories are tried before exiting non-zero
- Template rendering is performed on deploy as well, and trailing newlines are preserved, so no unnecessary diff appears at the destination

### Summary

After the per-repository lines, a summary across every targeted repository is printed. Each repository is counted as `created` (a new PR), `pushed` (a direct push), `reused` (an existing open PR was updated), `no-change`, or `failed`; zero-count categories are omitted:

```text
myorg/user-service@main:
  + pom.xml (new)
  => Created PR: https://github.com/myorg/user-service/pull/1
myorg/api-gateway@main: no changes

Summary
  2 repositories
  1 created    user-service
  1 no-change  api-gateway
```

With `--dry-run` the heading becomes `Summary (dry-run)` and repositories with a diff are counted as `would-change`.

With `--json`, stdout carries a single JSON report instead of the human-readable output (logs still go to stderr). Each branch's `status` is one of `created` / `pushed` / `reused` / `no_change` / `would_change`:

```json
{
  "command": "deploy",
  "dry_run": false,
  "repositories": [
    {
      "name": "myorg/user-service",
      "branches": [
        {
          "branch": "main",
          "status": "created",
          "added": ["pom.xml"],
          "updated": [],
          "pr_url": "https://github.com/myorg/user-service/pull/1",
          "commit_sha": null
        }
      ]
    },
    {
      "name": "myorg/api-gateway",
      "branches": [
        {
          "branch": "main",
          "status": "no_change",
          "added": [],
          "updated": [],
          "pr_url": null,
          "commit_sha": null
        }
      ]
    }
  ],
  "summary": {
    "created": ["user-service"],
    "pushed": [],
    "reused": [],
    "would_change": [],
    "no_change": ["api-gateway"],
    "failed": []
  }
}
```

### push mode

With `deploy_mode: push` (set in `ghfanout.yaml`, or per repository in `manifest.yaml`), no working branch or PR is created; commits are pushed directly onto the target branch.

- Force push is never used: if another push lands after the diff is computed, the deploy fails with an error (just retry)
- For repositories with branch protection, use `pr` (the default)

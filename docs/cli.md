# CLI Reference

```text
ghfanout [GLOBAL OPTIONS] COMMAND [ARGS]
```

## Global options

| Option | Description |
| --- | --- |
| `-C, --config-dir <dir>` | Explicitly specify the config repository root (defaults to searching upward from the current directory for `ghfanout.yaml`) |
| `-v, --verbose` | Output verbose (DEBUG) logs |

## init

Generate a config repository scaffold, with a working sample:

```bash
ghfanout init                          # Generate into the current directory
ghfanout init ./config --org myorg     # Specify the target directory and org
```

- Generates `ghfanout.yaml`, `.ghfanoutignore`, `base/common/.gitignore`, `base/java-service/pom.xml.tmpl`, and `overlays/example-service/manifest.yaml` — you can try a build right after generating
- Fails if `ghfanout.yaml` already exists in the target directory; individual scaffold files that already exist are skipped without being overwritten

## build

Build (compose) base and overlay locally — no git operations, no network access. Use it to check exactly what would be distributed:

```bash
ghfanout build                         # Build all repositories under overlays/ (output: <config root>/dist/<overlay>/)
ghfanout build user-service            # Output: <config root>/dist/user-service/ (can be changed via output_dir)
ghfanout build user-service -o /tmp/x  # Specify the output directory (takes precedence over ghfanout.yaml's output_dir)
ghfanout build -o /tmp/x               # Output all repositories to /tmp/x/<overlay>/
```

Behavior:

- The output only overwrites files; it does not delete unrelated files remaining in the output destination
- Files matched by the config repository's `.ghfanoutignore` are excluded from the output — see [Configuration](configuration.md#ghfanoutignore)
- Running `build` with the overlay omitted builds all repositories under `overlays/`. Like `deploy --all`, it does not stop on a single failure — it tries all of them before exiting non-zero
- Building an overlay that has a per-branch `bases` or `values` override writes output per branch to `<output>/<branch name>/` (without an override, a single set is written directly under `<output>/`)
- Template (`*.tmpl`) rendering errors, such as referencing an undefined variable, cause the build to fail ([Templates](templates.md))

## deploy

Compare the build result against the target repository's current state, and open a PR (or push directly) only if there is a diff:

```bash
ghfanout deploy user-service --dry-run   # Show only the diff, change nothing (safety check)
ghfanout deploy user-service             # Create branch ghfanout/update-<branch> and open a PR
ghfanout deploy --all                    # Deploy to all repositories under overlays/
```

Requires authentication — see [Authentication](authentication.md). Specify either an overlay name or `--all` (not both).

Behavior:

- The diff is computed for each target branch, and a PR is created only for branches that have a diff (branches without a diff report `no changes`)
- If the working branch `ghfanout/update-<branch>` already exists, it is recreated with the latest commit; if there is an existing open PR, it is reused instead of creating a new one
- If composition yields zero files to distribute (for example, everything is excluded by `.ghfanoutignore`), the repository — or the affected branch — is skipped with a warning instead of failing
- With `--all`, a single repository's failure does not stop the run; all repositories are tried before exiting non-zero
- Template rendering is performed on deploy as well, and trailing newlines are preserved, so no unnecessary diff appears at the destination

### push mode

With `deploy_mode: push` (set in `ghfanout.yaml`, or per repository in `manifest.yaml`), no working branch or PR is created; commits are pushed directly onto the target branch.

- Force push is never used: if another push lands after the diff is computed, the deploy fails with an error (just retry)
- For repositories with branch protection, use `pr` (the default)

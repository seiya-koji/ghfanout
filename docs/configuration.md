# Configuration

A *config repository* is the input to ghfanout: a directory (usually its own Git repository) containing everything the tool needs — which files to distribute, to which repositories, on which branches.

## Concepts

ghfanout borrows Kustomize's base + overlay model:

- **base** — the content. Directories under `base/` hold the actual files to distribute:
    - `base/common/` is distributed to **every** repository, unconditionally
    - Every other directory under `base/` is a **profile**: a named file set (e.g. `java-service`) that a repository receives only when its overlay opts in
- **overlay** — the selection. Each directory under `overlays/` corresponds to one target repository (the directory name *is* the destination repository name), and its `manifest.yaml` chooses profiles, branches, and template values
- **ghfanout.yaml** — the root config at the top of the config repository: the destination host / org, and tool-wide defaults

Typical tasks map onto this directly:

- Distribute a file to every repository → put it under `base/common/`
- Share a `pom.xml` among only the Java services → create a `java-service` profile and list it in the `bases:` of each Java repository's manifest
- Give one repository a slightly different value → set it in that overlay's `values:` and reference it from a template ([Templates](templates.md))

## Directory layout

```text
ghfanout.yaml                # Declares the destination host / org (one per config repository)

base/
  common/                    # Common to all repositories, distributed unconditionally
    .gitignore
  java-service/              # A profile (distributed only when selected by an overlay)
    pom.xml.tmpl             # .tmpl is a template. Variables are expanded and distributed as pom.xml
  python-service/
    pyproject.toml
  node-service/
    package.json

overlays/
  user-service/              # Directory name = destination repository name
    manifest.yaml
  api-gateway/
    manifest.yaml
```

## ghfanout.yaml

The root configuration file:

```yaml
host: github.example.com   # Defaults to github.com (specify only for GitHub Enterprise Server)
org: myorg             # Required. overlays/<name> corresponds to the myorg/<name> repository
deploy_mode: pr        # Deployment method. pr: open a PR (default) / push: push directly to the target branch
output_dir: dist       # Output directory for build (defaults to dist)
auth:                  # Non-secret GitHub App authentication settings (optional)
  app_id: 123456
  # installation_id: 789   # resolved automatically from org if omitted
  private_key_file: ~/.config/ghfanout/app.pem
```

| Key | Required | Description |
| --- | --- | --- |
| `org` | ✓ | Destination GitHub org. `overlays/<name>` corresponds to the `<org>/<name>` repository |
| `host` | | GitHub host. Defaults to `github.com`; specify only for GitHub Enterprise Server |
| `deploy_mode` | | `pr` (default): open a pull request / `push`: push directly to the target branch. See [CLI Reference](cli.md#deploy) for the trade-offs |
| `output_dir` | | Output directory for `build` (defaults to `dist`). Relative paths resolve against the directory containing `ghfanout.yaml` |
| `auth` | | Non-secret GitHub App authentication settings. See [Authentication](authentication.md) |

## .ghfanoutignore

An optional file at the config repository root that excludes files under `base/` from distribution. This lets a profile keep its own `README.md` or helper files alongside the content it ships, without those files being sent to the target repositories.

```text
# Same syntax as .gitignore ('#' starts a comment only at the start of a line)

# A README.md at any depth in any profile
README.md

# All markdown files
*.md

# Everything under a docs/ directory — but keep docs/keep.md (negation)
docs/
!docs/keep.md
```

- Syntax is `.gitignore` compatible (including `!` negation, `**`, and trailing-slash directory matches). One difference from git itself: `!` can re-include a file whose parent directory is excluded, so the `docs/` + `!docs/keep.md` example above works
- Each pattern is matched against a file's path **relative to its profile directory** — e.g. `base/java-service/docs/README.md` is matched as `docs/README.md`, so one rule applies across `common/` and every profile
- Matching happens on the **source name, before the `.tmpl` suffix is stripped**. To exclude `pom.xml.tmpl`, write `pom.xml.tmpl` (or `*.tmpl`), not `pom.xml`
- Only the single file at the config repository root is read; a `.ghfanoutignore` placed inside a profile has no effect (it is treated like any other file and distributed)

## manifest.yaml

One per overlay, at `overlays/<repo>/manifest.yaml`:

```yaml
bases:                 # Profiles to distribute. common/ is always included
  - java-service
branches:              # Destination branches. Defaults to the target repository's default branch only
  - main
  - release-1.x
deploy_mode: push      # Override the deployment method for only this repository (defaults to ghfanout.yaml's value)
values:                # Values referenced from templates (*.tmpl) (optional, can be nested)
  version: "1.2.3"
```

| Key | Required | Description |
| --- | --- | --- |
| `bases` | | Profiles (directory names under `base/`) to distribute. `common/` is always included |
| `branches` | | Destination branches. Defaults to the target repository's default branch only. Elements can be strings or objects (see [Per-branch overrides](#per-branch-overrides)) |
| `deploy_mode` | | Overrides `ghfanout.yaml`'s `deploy_mode` for this repository only |
| `values` | | Values referenced from templates; can be nested. See [Templates](templates.md) |

Typos are caught early: unknown keys and duplicate branch names are rejected when the manifest is loaded, and a profile listed in `bases` that has no directory under `base/` fails the build.

### File precedence

When files with the same relative path exist in multiple profiles, `common/` has the lowest priority, and profiles listed later in `bases:` take precedence. The later one wins and fully overwrites the file — this is reported via an info log when it occurs.

## Per-branch overrides

By writing an element of `branches:` in object form, you can override the profiles to distribute (`bases`) or the template values (`values`) for just that branch. Strings and objects can be mixed:

```yaml
bases:
  - java-service
values:
  version: "1.0"
branches:
  - main                    # String = use the top-level bases / values as-is
  - name: release-1.x       # Object = override bases or values for just this branch
    bases:
      - java-service-legacy
    values:
      version: "0.9"
```

The override semantics differ between `bases` and `values`:

- `bases` is a **replacement**: the top-level `bases` is not inherited (explicitly specifying `bases: []` distributes only `common/`)
- `values` is a **deep merge** (similar to Helm): it is recursively merged key by key into the top-level `values`, inheriting common values while overriding only the differences. Lists are not concatenated — they are replaced wholesale

**Note:** building an overlay that has a per-branch `bases` or `values` override writes output per branch to `<output>/<branch name>/`. See [CLI Reference](cli.md#build) for details.

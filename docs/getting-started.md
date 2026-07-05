# Getting Started

This tutorial walks you through the whole ghfanout workflow: installing the tool, generating a config repository, building it locally, and opening your first pull request.

## Prerequisites

- Python 3.11 or later (3.11–3.14 are tested)
- A GitHub org (or user account) with at least one repository you want to distribute files to
- A Personal Access Token for that org (used in step 4; [GitHub App authentication](authentication.md) is also supported)

## 1. Install ghfanout

```bash
pip install ghfanout
```

Or, if you use [uv](https://docs.astral.sh/uv/):

```bash
uv tool install ghfanout
```

## 2. Generate a config repository

ghfanout reads its input from a *config repository*: a directory (usually its own Git repository) that declares what to distribute and where. `ghfanout init` generates a working scaffold with a sample:

```bash
mkdir my-config && cd my-config
ghfanout init --org myorg      # use your actual GitHub org
```

```text
Generated 5 file(s) in /path/to/my-config
Next steps:
  1. Change the org in ghfanout.yaml to your actual GitHub org
  2. Run ghfanout build example-service to check the build output
  3. Set GHFANOUT_TOKEN and run ghfanout deploy example-service --dry-run
```

The generated layout:

```text
ghfanout.yaml                  # where to distribute: host / org, etc.
.ghfanoutignore                # optional: files under base/ to exclude (.gitignore syntax)
base/
  common/                      # files distributed to every repository
    .gitignore
  java-service/                # a "profile": distributed only to repos that opt in
    pom.xml.tmpl               # .tmpl files are Jinja2 templates
overlays/
  example-service/             # one directory per target repository
    manifest.yaml              # which profiles / branches this repo receives
```

The mental model: **`base/` holds the content, `overlays/` selects who gets it.** Every repository listed under `overlays/` receives everything in `base/common/`, plus the profiles its `manifest.yaml` opts into. See [Configuration](configuration.md) for the full picture.

The sample overlay targets a repository called `example-service` in your org. Rename the directory under `overlays/` to match one of your real repositories — the directory name *is* the target repository name. (The rest of this tutorial keeps the name `example-service`.)

## 3. Build locally

`build` composes base + overlay into plain files on disk — no git operations, no network access — so you can inspect exactly what would be distributed:

```bash
ghfanout build example-service
```

```text
example-service: wrote 2 file(s) to /path/to/my-config/dist/example-service
```

Look inside `dist/example-service/`:

- `.gitignore` — copied as-is from `base/common/`
- `pom.xml` — rendered from `base/java-service/pom.xml.tmpl`, with `{{ repo }}` replaced by the repository name (see [Templates](templates.md))

## 4. Set up authentication

For a first run, a Personal Access Token is the quickest path. Create a token with permissions equivalent to `contents: write` and `pull_requests: write` on the target repositories, and export it:

```bash
export GHFANOUT_TOKEN=ghp_xxxx
```

For CI and shared environments, [GitHub App authentication](authentication.md) is safer and recommended.

## 5. Preview the diff

`deploy --dry-run` compares your build output against the current state of the target repository and prints the diff, without changing anything:

```bash
ghfanout deploy example-service --dry-run
```

```text
[dry-run] myorg/example-service@main:
  + .gitignore (new)
  + pom.xml (new)
```

## 6. Deploy

Run the same command without `--dry-run`, and ghfanout creates a working branch (`ghfanout/update-<branch>`) and opens a pull request:

```bash
ghfanout deploy example-service
```

```text
myorg/example-service@main:
  + .gitignore (new)
  + pom.xml (new)
  => Created PR: https://github.com/myorg/example-service/pull/1
```

Review and merge the PR on GitHub as usual. When you later change a file under `base/` and deploy again, only repositories whose content actually differs get a PR — repositories that are already up to date report `no changes`.

## Next steps

- Add more repositories: one directory under `overlays/` per repository — then `ghfanout deploy --all` updates the whole fleet at once
- Learn the composition rules (profiles, precedence, per-branch overrides) in [Configuration](configuration.md)
- Embed per-repository values in shared files with [Templates](templates.md)
- Switch to [GitHub App authentication](authentication.md) for CI

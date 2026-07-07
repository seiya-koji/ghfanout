# ghfanout

[![GitHub Pages](https://github.com/seiya-koji/ghfanout/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/seiya-koji/ghfanout/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/seiya-koji/ghfanout/graph/badge.svg)](https://codecov.io/gh/seiya-koji/ghfanout)
[![PyPI version](https://img.shields.io/pypi/v/ghfanout.svg)](https://pypi.org/project/ghfanout/)
[![Python versions](https://img.shields.io/pypi/pyversions/ghfanout.svg)](https://pypi.org/project/ghfanout/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/seiya-koji/ghfanout/blob/main/LICENSE)

**Configuration-as-Code for many GitHub repositories.** Manage shared configuration files (`.gitignore`, `pom.xml`, `pyproject.toml`, `package.json`, …) in one place — a single source of truth — and fan them out as pull requests (or direct pushes). Edit a file once, run one command, and every affected repository gets a PR.

- **No `git clone` of target repositories** — everything goes through the GitHub Git Data API, so it works with nothing more than `pip install`
- **Kustomize-like base + overlay composition**, with Jinja2 templating for per-repository values
- **Designed for microservice fleets** that cannot be consolidated into a monorepo

## Installation

```bash
pip install ghfanout        # or: uv tool install ghfanout
```

Requires Python 3.11 or later (tested on 3.11–3.14).

## Quick start

```bash
# 1. Generate a config repository scaffold (with a working sample)
ghfanout init ./my-config --org myorg
cd my-config

# 2. Compose base + overlay locally and inspect the result (no git operations, no network)
ghfanout build example-service           # output: dist/example-service/

# 3. Authenticate, preview the diff against the real repository, then deploy
export GHFANOUT_TOKEN=ghp_xxxx
ghfanout deploy example-service --dry-run
ghfanout deploy example-service          # opens a PR if there is a diff
```

How it fits together — `base/` holds the content, `overlays/` selects who gets it:

```text
ghfanout.yaml                # destination host / org
.ghfanoutignore              # optional: files under base/ to exclude (.gitignore syntax)
base/
  common/                    # distributed to every repository
    .gitignore
  java-service/              # a profile: distributed only to repos that opt in
    pom.xml.jinja            # .jinja = Jinja2 template, rendered per repository
  python-service/
    pyproject.toml
  node-service/
    package.json
overlays/
  user-service/              # directory name = target repository name
    manifest.yaml            # which profiles / branches this repo receives
  api-gateway/
    manifest.yaml
```

## Documentation

Full documentation: **<https://seiya-koji.github.io/ghfanout/>**

- [Getting Started](docs/getting-started.md) — install → init → build → deploy, end to end
- [Configuration](docs/configuration.md) — config repository layout, `ghfanout.yaml`, `manifest.yaml`, per-branch overrides
- [Templates](docs/templates.md) — embedding per-repository values with Jinja2
- [Authentication](docs/authentication.md) — PAT and GitHub App
- [CLI Reference](docs/cli.md) — commands, options, and behavior details
- [Development](docs/development.md) — local setup, tests, lint, and commit conventions

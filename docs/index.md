# ghfanout

ghfanout is **Configuration-as-Code for many GitHub repositories**: a CLI tool that manages shared configuration files (such as `.gitignore`, `pom.xml`, `pyproject.toml`, and `package.json`) in one place — a single source of truth — and fans them out as pull requests (or direct pushes).

Edit a file once in your config repository, run one command, and every affected repository receives a pull request with the change.

- **No `git clone` required** — target repositories are read and written purely through the GitHub Git Data API, so the tool works with nothing more than `pip install`
- **Kustomize-like composition** — a `base` of shared files plus per-repository `overlays`, with Jinja2 templating for repository-specific values
- **Designed for microservices** — made for fleets of repositories that cannot be consolidated into a monorepo

## Where to start

New to ghfanout? Follow the [Getting Started](getting-started.md) tutorial — it walks you through installing the tool, generating a config repository, and opening your first pull request.

| Page | What it covers |
| --- | --- |
| [Getting Started](getting-started.md) | Install → `init` → `build` → authenticate → `deploy`, end to end |
| [Configuration](configuration.md) | The config repository layout, `ghfanout.yaml`, `manifest.yaml`, per-branch overrides, and distribution excludes (`.ghfanoutignore`) |
| [Templates](templates.md) | Embedding repository-specific values with `.jinja` files (Jinja2) |
| [Authentication](authentication.md) | PAT and GitHub App authentication in detail |
| [CLI Reference](cli.md) | All commands, options, and behavior details |
| [Development](development.md) | Local setup, running tests/lint/type-check, and commit conventions |

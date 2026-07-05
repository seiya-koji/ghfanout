# Contributing to ghfanout

Thanks for taking the time to contribute! This document covers what you need to get a change from idea to merged pull request.

## Development setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11 or later.

```bash
git clone https://github.com/seiya-koji/ghfanout.git
cd ghfanout
uv sync                     # Install dependencies
uv run pre-commit install   # Set up git hooks (once per clone)
```

See [Development](https://seiya-koji.github.io/ghfanout/development/) for the full local workflow, including running the documentation site locally.

## Before opening a pull request

Run these locally — all of them run in CI on every pull request and must pass:

```bash
uv run pytest                              # Unit tests (GitHub API is mocked, no network access)
uv run ruff check src tests                # Lint
uv run ruff format src tests               # Format
uv run mypy                                # Type-check in strict mode (src and tests)
uv run --group docs mkdocs build --strict  # Docs build, if you touched docs/ or mkdocs.yml
```

If your change affects behavior, a feature, or CLI options, update the corresponding page under `docs/` in the same pull request (and `README.md` too, if it's something a new user needs to know up front).

## Commit messages and pull request titles

Both commit messages and the pull request title must follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat: ...`, `fix: ...`, `docs: ...`, `refactor: ...`, `chore: ...`, `test: ...`, `style: ...`, `perf: ...`):

- Enforced locally by the `commit-msg` pre-commit hook
- Enforced on GitHub by the PR title check
- The `feat`/`fix` prefix drives the version bump via [release-please](https://github.com/googleapis/release-please), so pick it deliberately

## Pull request checklist

- [ ] `uv run pytest`, `uv run ruff check`, and `uv run mypy` all pass
- [ ] `docs/` (and `README.md` if relevant) updated for any behavior, feature, or CLI change
- [ ] Commit messages and the PR title follow Conventional Commits
- [ ] `uv run pre-commit run --all-files` passes

## Reporting bugs and requesting features

Please open a [GitHub issue](https://github.com/seiya-koji/ghfanout/issues) with a clear description and, for bugs, steps to reproduce.

## License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).

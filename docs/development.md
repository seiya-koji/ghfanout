# Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11 or later.

## Setup

```bash
git clone https://github.com/seiya-koji/ghfanout.git
cd ghfanout
uv sync                     # Install dependencies
uv run pre-commit install   # Set up git hooks (once per clone)
```

## Common tasks

```bash
uv run pytest                              # Unit tests (GitHub API is mocked, no network access)
uv run ruff check src tests                 # Lint (add --fix to auto-fix)
uv run ruff format src tests                # Format
uv run mypy                                 # Type-check in strict mode (src and tests)
uv run --group docs mkdocs serve            # Preview the documentation site locally
uv run --group docs mkdocs build --strict   # Verify the documentation site builds without warnings
```

CI runs pytest across Python 3.11–3.14, plus ruff, mypy, and a strict docs build on every pull request — all of them should pass locally before you open one.

## Commit messages

Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/) (e.g. `feat: ...`, `fix: ...`). This is enforced locally by the `commit-msg` hook and on GitHub by the PR title check, and it drives automated releases via [release-please](https://github.com/googleapis/release-please).

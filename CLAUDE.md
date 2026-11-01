# Claude Code Instructions

> See [AGENTS.md](AGENTS.md) for full architectural invariants, codebase map, and security rules.

## Core Invariants

- **Never lose a failure:** Any code path that catches an exception must write a record or re-raise. Never silently swallow errors.
- **The folder is the API:** `.quarantine/` is plain files; `index.json` is an expendable, rebuildable cache.
- **Atomic writes:** Staging directory/file -> fsync -> rename/replace.
- **Redact before writing:** Redaction runs *before* serialization and never mutates caller objects.
- **Zero runtime dependencies:** `dependencies = []` in `pyproject.toml` must remain empty.

## Quick Commands

- **Run all checks:** `ruff check . && ruff format --check . && mypy && pytest --cov --cov-report=term-missing --cov-config=.coveragerc.nonlinux`
- **Build docs strictly:** `mkdocs build --strict`
- **Regression tests:** `pytest -m regression`
- **Target branch:** PRs must target `dev` with branch name `feature/dev/<name>`.

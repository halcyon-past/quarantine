# Contributing

Thanks for looking. This is a small library with a deliberately small surface:
if a change makes the common case (`@quarantine` on one function) harder to
understand, it probably belongs in a different package.

## Getting set up

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install            # optional, but it runs what CI runs
```

## The checks

```bash
make check      # or, without make:
ruff check . && ruff format --check .
mypy
pytest --cov --cov-report=term-missing
```

All three must pass, on Python 3.10 through 3.13, on Linux, macOS and Windows.
Coverage must stay at or above 90%.

## Ground rules for changes

- **Never lose a failure.** Any code path that catches an exception must either
  write a record or re-raise. Silently dropping an item is the one bug this
  library cannot have.
- **The folder is the API.** `.quarantine/` is plain files that people read,
  `grep` and delete by hand. Record directories must stay self-describing, and
  `index.json` must stay a rebuildable cache - never the source of truth.
- **Writes stay atomic.** Build in a temp directory, rename into place. If you
  add a file to a record, add it before the rename.
- **Redact before writing, not after.** `redact_call()` runs before anything
  reaches the disk, and never mutates the caller's object.
- **No runtime dependencies.** `pip install quarantine-py` should stay a
  no-questions-asked install in someone else's messy environment.
- **Tests describe behaviour**, not implementation: `test_loop_survives_a_bad_item`,
  not `test_wrapper_calls_store_add`.

## Releasing

1. Update `CHANGELOG.md` and `__version__` in `src/quarantine/_version.py`.
2. `make build` and check the artefacts in `dist/`.
3. Tag `v<version>`; CI builds and validates the wheel.

# Contributing

Thanks for looking. This is a small library with a deliberately small surface:
if a change makes the common case (`@quarantine` on one function) harder to
understand, it probably belongs in a different package.

Looking for something to work on? [ENHANCEMENTS.md](ENHANCEMENTS.md) is the
planned roadmap, with the reasoning behind each item.

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

## The regression suite

`tests/test_regression.py` walks complete user journeys through the public
surface only - real subprocesses, the installed CLI, real HTTP against the
dashboard. It runs as its own required CI job, and you can run it alone with:

```bash
pytest -m regression      # or: make regression
```

Keep it current as the library changes:

- **New feature?** Add (or extend) a journey showing a user exercising it
  end to end - not just unit tests of the internals.
- **Changed a workflow, CLI output, `--json` shape, or exit code?** The suite
  pins these as contracts, so update the affected journey in the same PR and
  call the behaviour change out in the PR description.
- **Fixed a workflow-level bug?** Add the failing journey first, then the fix.

A regression test that no longer matches documented behaviour is a bug in the
test - bring it up to date rather than deleting it.

## Docs and decision records

The `docs/` pages are rendered with mkdocs-material and deployed on every
merge to `main`. Build locally with:

```bash
pip install -e ".[docs]"
make docs          # mkdocs build --strict - broken links fail the build
mkdocs serve       # live preview at http://127.0.0.1:8000
```

Design decisions live in `docs/adr/`. If your change makes a decision worth
defending later - a format, a guarantee, a rejected alternative - add a short
ADR in the same PR; if it contradicts an existing one, supersede that record
rather than editing it.

## Git Flow and Pull Requests

To maintain stability across environments, we enforce strict repository rules and promotion flows:

1. **Target Branch**: All feature Pull Requests **must** be raised against the `dev` branch. The only exception is for critical production fixes, which may be raised against `main` as a `hotfix`.
2. **Promotion Flow**: Do not raise PRs against `uat` or `main` for new features. The maintainers will handle promoting code from `dev` -> `uat` -> `main` (prod).
3. **Protected Branches**: Direct commits to `main`, `dev`, and `uat` are blocked. You must use a Pull Request.
4. **Branch Naming**: Your PR branches must follow one of these naming conventions, or the CI checks will fail (unless you are submitting a PR from a fork):
   - `feature/<target-branch>/<feature-name>` (e.g. `feature/dev/add-retry-logic`)
   - `hotfix/<feature-name>` (e.g. `hotfix/fix-type-error`)
5. **Mandatory Tests**: All CI jobs (tests, linting, building) must pass completely before a PR can be merged.
6. **Mandatory Reviews**: At least 1 approving review from the Code Owner (`@halcyon-past`) is required to merge a PR.

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

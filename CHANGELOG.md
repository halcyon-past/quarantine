# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **End-to-end regression suite:** `tests/test_regression.py` walks complete user journeys through the public surface only (real subprocesses, the installed CLI, real HTTP against the dashboard) and pins the `--json` shapes and CLI exit codes as contracts. Run it alone with `pytest -m regression` or `make regression`.
- A dedicated `regression-suite` CI job (Linux and Windows); the `all-tests-pass` aggregate check now requires it, so PRs cannot merge without the journeys passing.
- `CONTRIBUTING.md` now documents when and how to keep the regression suite up to date.

## [0.2.0] - 2026-08-25

### Added
- **Local Web Dashboard:** Added a `quarantine ui` CLI command that spins up a lightweight, zero-dependency local web server to view tracebacks and payloads in a clean browser interface, and allows clicking to retry them.
- **Transient Retries:** Added built-in transient retries (`retries` and `backoff` config options) to retry transient failures before committing items to quarantine. Support added for both synchronous and asynchronous functions.

## [0.1.3] - 2026-08-24

### Fixed
- Fixed documentation to consistently reference the `quarantine-py` PyPI package and correct GitHub URLs.
- Fixed a `PermissionError` on Windows when acquiring the `.index.lock` file concurrently.

## [0.1.2] - 2026-08-23

### Fixed
- Fixed project URLs in `pyproject.toml` to point to the correct `halcyon-past` GitHub repository instead of the original placeholder.
## [0.1.1] - 2026-08-23

### Fixed
- Fixed documentation relative links in the README so they resolve correctly on PyPI.
- Updated package name to `quarantine-py` to avoid naming collision on PyPI.

## [0.1.0] - 2026-08-22

First release.

### Added

- `@quarantine` decorator (bare or configured) for sync **and** `async def`
  functions: exceptions are written to a `.quarantine/` folder and the caller
  keeps going instead of crashing.
- `shield(items, using=...)` / `ashield(...)` for protecting a loop without
  decorating anything.
- `Quarantine` class - the explicit, injectable form of the decorator, with
  `wrap()`, `call()`, `acall()`, `records()`, `retry()`, `clear()` and `stats`.
- On-disk record format: `input.pkl` / `input.json`, `input.txt`,
  `traceback.txt`, `meta.json` per record, plus a rebuildable `index.json`.
  Records are built in a temp directory and atomically renamed into place, so a
  crash mid-save can never corrupt the folder.
- Serialization fallback chain: pickle -> JSON -> `repr`, so *something*
  readable is always saved.
- `redact=[...]` scrubbing (case-insensitive, glob-aware, recursive, never
  mutates the caller's object) applied *before* anything touches the disk.
- Consecutive-failure circuit breaker (`halt_after`, default 50) raising
  `SystemicFailure`, and a disk cap (`max_items`, default 10 000) raising
  `QuarantineFull`.
- Deduplication: an item already in quarantine is skipped on a rerun
  (`skip_known_bad`), which also keeps the folder free of duplicates.
- `on_quarantine=` hook for alerting, and an end-of-run summary line printed to
  stderr (`report=`), with an ASCII fallback for non-UTF-8 consoles.
- CLI: `quarantine list`, `show`, `retry`, `debug`, `clear`, `stats`,
  `reindex`, each with `--json` where it makes sense.
- `retry --import job.py` / `debug --import job.py`, so functions defined in a
  script (module name `__main__`, which a later process cannot import) can
  still be replayed. Records store the defining file for exactly this, and the
  error message names the flag when it is needed.
- Console-safe output: every glyph falls back to ASCII when the stream cannot
  encode it, checked *before* writing. `sys.stderr` uses `backslashreplace`, so
  waiting for a `UnicodeEncodeError` would print an escape sequence rather than
  a readable `OK`.
- Transient `PermissionError` on the commit rename - a scanner or sync client
  holding a brand-new folder open on Windows - is retried with backoff instead
  of costing a record.
- Documentation: `docs/` covering installation, usage, the CLI, the API, the
  on-disk format, troubleshooting and an FAQ, with tests that fail if the docs
  drift from the code.
- Full type annotations and a `py.typed` marker.

[Unreleased]: https://github.com/halcyon-past/quarantine/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/halcyon-past/quarantine/releases/tag/v0.2.0

[0.1.3]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.3
[0.1.2]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.2
[0.1.1]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.1
[0.1.0]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.0

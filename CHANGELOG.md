# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Framework recipes** ([#43](https://github.com/halcyon-past/quarantine/issues/43)): a new [docs/recipes.md](docs/recipes.md) with worked, tested integrations — a pandas ETL job, a web scraper, an Airflow task, and a FastAPI background worker. Recipes rather than plugins, so there is no operator to maintain against someone else's release cycle.

## [0.3.0] - 2026-08-30

### Added
- **Exponential backoff with jitter:** `backoff_factor=` grows the transient-retry delay per attempt (`2.0` doubles it; the default `1.0` keeps today's fixed delay), and `jitter=` adds up to that many random seconds so parallel workers back off out of step. Same schedule for sync and `async def`.
- **Poison-item detection:** `dead_after=` (and `quarantine retry --dead-after N`) treats a record that has already failed N attempts as *dead* — blanket retries skip it and report why, while retrying it by explicit id always runs it. Derived from the `attempts` counter already on disk, so it works across processes and versions ([ADR 0005](docs/adr/0005-stateless-poison-detection.md)).
- **Retry lifecycle hooks:** `on_retry_success=` and `on_retry_failure=` join `on_quarantine=`, all reported-not-raised, so metrics can follow a record through its whole life.
- **Observability recipes:** a new [docs/observability.md](docs/observability.md) with copy-pasteable hook wiring for Prometheus (long-running and Pushgateway), Datadog, Sentry and plain logging — the hook-based resolution of [#14](https://github.com/halcyon-past/quarantine/issues/14) ([ADR 0006](docs/adr/0006-hooks-not-servers.md)).
- **Property-based tests:** a Hypothesis suite asserting the load-bearing invariants over arbitrary inputs — serialization always produces something readable, redaction never leaks and never mutates, fingerprints ignore dict ordering.
- **Architecture decision records:** `docs/adr/` documents the design decisions with their reasoning — rename-based atomicity, directory-per-record over JSONL, pickle-first serialization, zero dependencies, and the proposed object-store commit point.
- **Documentation site:** the docs are now built with mkdocs-material (`make docs`, deployed to GitHub Pages on merge; `pip install "quarantine-py[docs]"` for the toolchain).
- **End-to-end regression suite:** `tests/test_regression.py` walks complete user journeys through the public surface only (real subprocesses, the installed CLI, real HTTP against the dashboard) and pins the `--json` shapes and CLI exit codes as contracts. Runs as its own required CI job (`pytest -m regression` / `make regression`).
- **Security posture:** `SECURITY.md` with a disclosure policy and an honest threat model, all CI actions pinned to commit SHAs, and PyPI attestations enabled on release.
- `Quarantine(...)` now accepts `retries=` and `backoff=` directly, matching the decorator (previously they were reachable only via `Config`/`replace()`).

### Fixed
- Repaired `docs/api.md` and `docs/usage.md` sections that were garbled by a bad merge in 0.2.0 (option rows embedded inside code signatures, an orphaned options fragment), and gave transient retries a proper usage-guide section.

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

[Unreleased]: https://github.com/halcyon-past/quarantine/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/halcyon-past/quarantine/releases/tag/v0.3.0
[0.2.0]: https://github.com/halcyon-past/quarantine/releases/tag/v0.2.0

[0.1.3]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.3
[0.1.2]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.2
[0.1.1]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.1
[0.1.0]: https://github.com/halcyon-past/quarantine/releases/tag/v0.1.0

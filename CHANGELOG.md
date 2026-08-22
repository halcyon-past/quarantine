# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/quarantine-py/quarantine/releases/tag/v0.1.0

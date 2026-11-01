# Agent Guidelines for quarantine-py

This document provides system instructions and architectural context for autonomous AI agents, coding assistants (including Antigravity, Claude Code, Cursor, Copilot, Cline, Codex, Grok), and developer tools working on this codebase.

---

## 1. Project Philosophy & Invariants

`quarantine-py` is a zero-runtime-dependency resilience and dead-letter queue (DLQ) library for batch pipelines and ETL jobs in Python. When an item in a loop fails, `quarantine` captures the failure, stores its input, metadata, and traceback, and lets the pipeline continue.

Any agent modifying this codebase **must strictly uphold these five invariants**:

1. **Never lose a failure:** Any code path that catches an exception must either persist a complete record or re-raise. Silently swallowing or dropping a failure is the single catastrophic bug this library cannot have.
2. **The folder is the API:** `.quarantine/` consists of plain, human-readable files that operators can inspect, `grep`, or delete by hand. Record directories (`0001/`, `0002/`) are self-describing. `index.json` is strictly an expendable, rebuildable cache — never the source of truth.
3. **Writes stay atomic:** Never write directly to a committed record path. New records are assembled in hidden staging directories (`.tmp-*`), flushed and fsync'd, and committed via atomic directory rename (`os.rename`). Single files are written via atomic replace (`os.replace`).
4. **Redact before writing:** Scrubbing sensitive fields (`redact_call()`) happens *before* any data is serialized or touches disk/network. Redaction must never mutate the caller's objects.
5. **Zero core dependencies:** The core package must have **zero runtime dependencies** (`dependencies = []` in `pyproject.toml`). Remote backends (`boto3`, `google-cloud-storage`) are optional extras (`quarantine-py[s3]`, `quarantine-py[gcs]`).

---

## 2. Architecture & Codebase Map

```
src/quarantine/
├── __init__.py         # Package exports (@quarantine, shield, ashield, Quarantine, sentinels)
├── api.py              # Functional module-level interface and interned instance registry
├── cli.py              # CLI implementation (list, show, retry, debug, clear, stats, reindex, ui)
├── core.py             # Quarantine class, Config dataclass, Stats, wrap/call/acall, capacity logic
├── errors.py           # Exception hierarchy (QuarantineError, SystemicFailure, QuarantineFull, StorageError)
├── fingerprint.py      # Deduplication hashing (BLAKE2b over redacted canonical payload)
├── gcs_store.py        # Google Cloud Storage backend (gs://bucket/prefix with preconditions)
├── record.py           # Record dataclass, meta.json encoder, traceback and input accessors
├── redact.py           # Redactor engine: case-insensitive glob matching, recursive object walk
├── reporting.py        # Terminal formatting, ASCII fallback for non-UTF8 consoles, safe emitters
├── resolve.py          # Dynamic function re-importing for retries (including script __main__ loading)
├── s3_store.py         # AWS S3 backend (s3://bucket/prefix with conditional writes)
├── sentinels.py        # QUARANTINED and SKIPPED singleton markers (falsy, picklable)
├── serialize.py        # Serialization fallback chain: pickle -> JSON -> lossy repr
├── store.py            # Local filesystem Store, FileLock (deadlock-free with inode verification)
└── ui.py               # Local read-only web dashboard (ThreadingHTTPServer, CSRF protection)
```

---

## 3. Development Workflow & Commands

All development should occur in a Python 3.10+ virtual environment (`.venv`).

### Running Checks

Before completing any task, run the mandatory check suite:

```bash
# Linting
ruff check .

# Code Formatting
ruff format --check .

# Type Checking (Strict mode across all source files)
mypy

# Unit Tests with Coverage (Must maintain >= 90% coverage)
pytest --cov --cov-report=term-missing --cov-config=.coveragerc.nonlinux

# Documentation Build (Strict mode: broken links or syntax warnings fail the build)
mkdocs build --strict

# End-to-end Regression Suite
pytest -m regression
```

### Formatting Code

```bash
ruff format .
ruff check --fix .
```

---

## 4. Git & Contribution Rules

1. **Target Branch:** All feature work and non-critical bug fixes **must target `dev`**. Only emergency production fixes target `main` as hotfixes.
2. **Branch Naming:** PR branches must follow the strict naming convention:
   - `feature/<target-branch>/<description>` (e.g., `feature/dev/add-concurrency-guards`)
   - `hotfix/<description>` (e.g., `hotfix/fix-lock-timeout`)
3. **Internal Documentation:** Internal audit reports, scratch notes, or temporary research documents must **never be staged** into git.
4. **Packaging Hygiene:**
   - Development files, test suites, media reels, and documentation build outputs are excluded from wheels.
   - Wheels (`.whl`) must only contain the `quarantine` package, `py.typed`, and dist-info metadata.
   - Source distributions (`.tar.gz`) exclude media assets (`trailer/`, `trailer_reels/`), IDE configurations, and temporary files.

---

## 5. Security & Concurrency Considerations

- **FileLock Safety:** Always acquire locks with explicit deadlines. If breaking a stale lock fails due to `OSError`, return `False` so `acquire()` respects the timeout deadline. On lock release, verify the open file descriptor inode against the disk file inode to prevent deleting a re-created or stolen lock.
- **Symlink Protection:** Directory scans (`record_dirs()`, `purge_temp()`) must pass `follow_symlinks=False` and verify `not entry.is_symlink()` to prevent path traversal or recursive deletion attacks.
- **Web UI Security:** The `quarantine ui` dashboard binds to `127.0.0.1` by default and validates `Host`, `Origin`, and `Referer` headers on all state-modifying requests (`do_POST`) to block CSRF and DNS rebinding attacks.
- **Input Encoding:** Always read text files with `errors="replace"` to gracefully handle non-UTF-8 bytes without crashing on `UnicodeDecodeError`.

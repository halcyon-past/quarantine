# Architecture decision records

The choices that shaped quarantine, with the reasoning attached — so they are
revisited deliberately, not re-litigated by accident. Each record states the
context it was made in; if the context changes, supersede the record rather
than editing history.

| # | Decision | Status |
|---|---|---|
| [0001](0001-rename-based-atomicity.md) | Atomicity by temp-dir + rename, not locks | Accepted |
| [0002](0002-directory-per-record.md) | A directory per record, not a JSONL file | Accepted |
| [0003](0003-pickle-first-serialization.md) | Pickle-first serialization with a lossy fallback chain | Accepted |
| [0004](0004-zero-runtime-dependencies.md) | Zero runtime dependencies | Accepted |
| [0005](0005-stateless-poison-detection.md) | Poison items derived from `attempts`, not a stored flag | Accepted |
| [0006](0006-hooks-not-servers.md) | Observability through hooks, never an embedded server | Accepted |
| [0007](0007-object-store-commit-point.md) | Commit point for object-store backends | Proposed |

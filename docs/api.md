# Python API reference

[← docs index](index.md)

Everything below is importable straight from the top-level package:

```python
from quarantine import quarantine, shield, Quarantine, records, retry, QUARANTINED
```

The package is fully typed and ships a `py.typed` marker, so mypy and Pyright
see real signatures.

---

## `quarantine`

```python
quarantine(fn=None, /, *, dir=None, only=(Exception,), exclude=(),
           halt_after=50, max_items=10_000, retries=0, backoff=0.0,
           backoff_factor=1.0, jitter=0.0, dead_after=None, redact=(),
           on_quarantine=None, on_retry_success=None, on_retry_failure=None,
           skip_known_bad=True, report=True, verbose=False)
```

Wrap a function so a raised exception becomes a record on disk instead of a
crash. Usable bare (`@quarantine`) or configured (`@quarantine(...)`), on `def`
and on `async def`.

| Parameter | Default | Meaning |
|---|---|---|
| `dir` | `$QUARANTINE_DIR` or `./.quarantine` | Where records are written: a folder, or a [backend URL](remote-storage.md) like `s3://bucket/prefix`. |
| `only` | `(Exception,)` | Exception types to quarantine; anything else propagates. A bare class is accepted. |
| `exclude` | `()` | Exception types to let through even if `only` matches. |
| `halt_after` | `50` | Raise `SystemicFailure` after this many *consecutive* failures. `None` disables it. |
| `max_items` | `10_000` | Raise `QuarantineFull` rather than grow the folder past this. `None` disables it. |
| `retries` | `0` | Attempt the function this many additional times before quarantining. |
| `backoff` | `0.0` | Base delay in seconds between transient retries. |
| `backoff_factor` | `1.0` | Multiply the delay by this per retry; `2.0` gives exponential backoff. |
| `jitter` | `0.0` | Add up to this many random seconds to each delay, so parallel workers spread out. |
| `dead_after` | `None` | Records that have failed this many attempts are *dead*: a blanket `retry()` skips them; retrying by explicit id still runs them. |
| `redact` | `()` | Field-name patterns (case-insensitive, globs allowed) scrubbed before writing. |
| `on_quarantine` | `None` | `Callable[[Record], None]`, called after each record is safely written. Exceptions from it are reported, not raised. |
| `on_retry_success` | `None` | `Callable[[Record], None]`, called for each record a retry recovers. Reported, not raised. |
| `on_retry_failure` | `None` | `Callable[[Record], None]`, called for each record that fails a retry again. Reported, not raised. |
| `skip_known_bad` | `True` | Skip inputs already in quarantine instead of re-running them. |
| `report` | `True` | Print the one-line summary when the process exits. |
| `verbose` | `False` | Also print a line as each item is quarantined. |

Invalid values raise `TypeError`/`ValueError` **at decoration time**.

Wrapping a **generator function** raises `TypeError`: its body runs during
iteration, not during the call, so the decorator would protect nothing. Use
`shield()`.

The returned wrapper carries `__wrapped__` (the original function) and
`.quarantine` (the `Quarantine` instance behind it).

## `shield` / `ashield`

```python
shield(items, using=None, **options) -> Iterator[Any]
ashield(items, using=None, **options) -> AsyncIterator[Any]
```

Run `using` over `items`, yielding **only the results that worked**. Failures
are quarantined and never appear in the output, so the consumer never has to
check for a sentinel. Lazy. `**options` are the `quarantine()` options.

`ashield` accepts a sync or async iterable and an async `using`.

Omitting `using` raises `TypeError`: a generator cannot catch exceptions raised
in your loop *body*, only in the callable it hands work to.

## Sentinels

| | |
|---|---|
| `QUARANTINED` | Returned by a call that raised and was quarantined. |
| `SKIPPED` | Returned when the input was already in quarantine, so it was not re-run. |
| `is_quarantined(value)` | Explicit test for the first. |
| `is_skipped(value)` | Explicit test for the second. |
| `Sentinel` | The class behind both. Falsy, `repr` is `<quarantined>`, survives pickling as the same object. |

## `Quarantine`

```python
Quarantine(dir=None, *, only=(Exception,), exclude=(), halt_after=50,
           max_items=10_000, retries=0, backoff=0.0, backoff_factor=1.0,
           jitter=0.0, dead_after=None, redact=(), on_quarantine=None,
           on_retry_success=None, on_retry_failure=None, skip_known_bad=True,
           report=True, verbose=False, config=None)
```

The object the decorator is sugar over. Construct it when you would rather
inject something explicit — in tests, in a library, or when one process writes
to several folders.

| Member | Meaning |
|---|---|
| `wrap(fn)` | Return a protected version of `fn` (same thing the decorator does). Also available as `q(fn)`. |
| `call(fn, *args, **kwargs)` | Protect a single call without wrapping anything. |
| `acall(fn, *args, **kwargs)` | Awaitable form of `call`. |
| `records(function=None)` | `list[Record]`, oldest first, optionally filtered by function name. |
| `retry(ids=None, *, using=None, function=None, dry_run=False, import_from=None)` | Re-run records; returns `RetryResult`. |
| `aretry(...)` | Awaitable `retry`, for `async def` records. |
| `clear()` | Delete every record; returns how many. |
| `summary_line()` | The end-of-run line, or `None` when there is nothing to report. |
| `replace(**changes)` | A new instance with some settings changed. |
| `stats` | `Stats` counters. |
| `config` | The frozen `Config`. |
| `dir` | `Path` of the folder. |
| `store` | The `Store` behind it, for direct folder access. |
| `len(q)`, `iter(q)` | Number of records; iterate them. |

Instances are **interned by configuration**: `get_quarantine()` (and therefore
the decorator) returns the same object for the same options, so two decorated
functions sharing a folder share one set of counters and print one summary.

## Module-level shortcuts

These act on the default folder, or on `dir=` if you pass one.

| | |
|---|---|
| `records(function=None, *, dir=None)` | Everything currently quarantined. |
| `retry(ids=None, *, using=None, function=None, dir=None, dry_run=False, import_from=None)` | Re-run records. |
| `aretry(...)` | Awaitable `retry`. |
| `clear(*, dir=None)` | Empty the folder. |
| `summary(*, dir=None)` | The summary line, or `None`. |
| `get_quarantine(dir=None, **options)` | The shared instance for those options. |
| `default()` | The instance a bare `@quarantine` uses. |
| `reset()` | Forget every interned instance (test helper). |

## `Record`

What `records()` returns; also readable straight off the disk with
`Record.load(path)`.

| Field | Meaning |
|---|---|
| `id` | Integer id, matching the folder name (`1` ⇢ `0001`). |
| `function` | Qualified name of the function (`Loader.load`). |
| `module`, `source_file` | Where it was defined — used by `retry` to import it back. |
| `qualified_name` | `module.function`. |
| `error_type`, `error` | `"ValueError"`, and the message. |
| `summary` | `"ValueError: could not convert…"` on one line. |
| `created_at`, `last_failed_at` | UTC ISO-8601 timestamps. |
| `when` | Local `HH:MM:SS` of the last failure, for tables. |
| `attempts` | How many times it has failed, including retries. |
| `fingerprint` | Content hash used for deduplication. |
| `payload_format` | `"pickle"`, `"json"` or `"repr"`. |
| `payload_lossy`, `payload_reason` | Whether fidelity was lost, and why. |
| `redacted` | Field names that were scrubbed. |
| `preview` | One-line preview of the input. |
| `python`, `platform`, `quarantine_version`, `pid` | Provenance. |
| `path` | `Path` of the record folder. |
| `input_path` | Path of the machine-readable input, or `None`. |

| Method | Meaning |
|---|---|
| `traceback_text()` | The stored traceback. |
| `input_text()` | The human-readable input rendering. |
| `load_call()` | Rebuild the original `Call`. Raises `StorageError` if the input could only be stored as text. |
| `to_meta()` | The dict written to `meta.json`. |

## `Call`

```python
Call(args=(), kwargs={})
```

The call that failed. `call.item` is the first positional argument (else the
first keyword value, else `None`) — the "item" in the loop sense.

## `Stats` and `RetryResult`

```python
Stats(processed, quarantined, skipped, recovered, consecutive_failures)
Stats.total  # processed + quarantined + skipped
Stats.as_dict()

RetryResult(recovered, still_failing, unretryable)
RetryResult.attempted  # how many were actually re-run
RetryResult.as_dict()
```

`unretryable` is a list of `(id, reason)` — records left completely untouched
because the function could not be imported or the input could not be rebuilt.

## `Config`

The frozen, hashable settings object behind every `Quarantine`. Values are
normalised on construction (`dir` to `Path`, `only` to a tuple, `redact` to a
tuple of patterns) and validated eagerly.

## `Store`

Direct access to a folder, independent of any policy:

```python
from quarantine import Store

store = Store(".quarantine")
store.records()  # list[Record]; unreadable ones land in store.problems
store.get(1)
store.count()
store.delete(1)
store.clear()
store.rebuild_index()  # regenerate index.json from the record folders
store.purge_temp()  # sweep .tmp-* staging left by a hard crash
store.fingerprints()  # {fingerprint: id}
```

## Storage backends

`dir=` (and `--dir`, and `$QUARANTINE_DIR`) also accepts a backend URL —
`dir="s3://bucket/prefix"` stores records in a bucket instead of a folder.
See [remote storage](remote-storage.md) for setup, credentials and IAM.

| | |
|---|---|
| `StorageBackend` | The abstract interface every backend implements; `Store` is the local reference implementation. Subclass it to build your own. |
| `open_store(dir)` | Open the right backend for a path or URL: `open_store("s3://bucket/prefix")`, `open_store(".quarantine")`. |
| `register_backend(scheme, factory)` | Claim a URL scheme for a custom backend: `register_backend("mystore", MyStore)`. Registering an existing scheme replaces it. |

## Exceptions

All of them subclass `QuarantineError`, and none of them is ever quarantined —
including by `only=(BaseException,)`.

| | |
|---|---|
| `QuarantineError` | Base class. |
| `SystemicFailure` | The `halt_after` breaker tripped. `.count`, `.last_error`; chained from the original exception. |
| `QuarantineFull` | `max_items` reached. `.max_items`, `.directory`; chained from the original exception, so nothing is lost. |
| `StorageError` | The folder could not be read or written, or a record is corrupt. |
| `ResolutionError` | A retry could not import the function a record came from. |

## Constants and environment

| | |
|---|---|
| `PLACEHOLDER` | `"***REDACTED***"`, what a scrubbed value becomes. |
| `__version__` | The installed version. |
| `QUARANTINE_DIR` | Environment variable setting the default folder for the library and the CLI. |

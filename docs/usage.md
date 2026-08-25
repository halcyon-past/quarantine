# Usage guide

[← docs index](index.md)

- [The 30-second version](#the-30-second-version)
- [What a wrapped call returns](#what-a-wrapped-call-returns)
- [Options](#options)
- [Choosing which errors to catch](#choosing-which-errors-to-catch)
- [The circuit breaker](#the-circuit-breaker)
- [The disk cap](#the-disk-cap)
- [Redacting secrets](#redacting-secrets)
- [Reruns and deduplication](#reruns-and-deduplication)
- [Retrying from Python](#retrying-from-python)
- [Loops without a decorator](#loops-without-a-decorator)
- [Async](#async)
- [Threads and processes](#threads-and-processes)
- [Alerting](#alerting)
- [The explicit object](#the-explicit-object)
- [Recipes](#recipes)
- [Things it deliberately does not do](#things-it-deliberately-does-not-do)

## The 30-second version

```python
from quarantine import quarantine


@quarantine
def process(row):
    price = float(row["price"])
    save_to_db(row["id"], price)


for row in rows:
    process(row)
```

A row that raises is written to `.quarantine/` — the input, the traceback, and
the metadata — and the loop keeps going. At the end you get one line on stderr:

```
✓ 9,996 processed · ✗ 4 quarantined → .quarantine/  (run `quarantine retry` after fixing)
```

Then: `quarantine list`, fix the bug, `quarantine retry`.

## What a wrapped call returns

| Situation | Return value |
|---|---|
| Success | whatever your function returned |
| Raised, and quarantined | `QUARANTINED` |
| Input already in quarantine from an earlier run | `SKIPPED` |

Both sentinels are **falsy**, and both are singletons that survive pickling
(so they behave under `multiprocessing`). Test them explicitly when it matters:

```python
from quarantine import is_quarantined, is_skipped

result = process(row)
if is_quarantined(result):
    continue  # nothing to do; it is on disk with its traceback
```

If you need the *value* or nothing, filter with a truthiness check, or use
[`shield()`](#loops-without-a-decorator), which never yields a sentinel.

## Options

Every option is optional. These are the defaults:

```python
@quarantine(
    dir=".quarantine",  # or $QUARANTINE_DIR
    only=(Exception,),  # what to quarantine
    exclude=(),  # what to let through anyway
    halt_after=50,  # consecutive-failure circuit breaker
    max_items=10_000,  # cap on the folder
    retries=2,  # retry transient failures
    backoff=0.5,  # delay between retries
    redact=(),  # field names to scrub before saving
    on_quarantine=None,  # callback for each new record
    skip_known_bad=True,  # skip inputs already quarantined
    report=True,  # print the summary at exit
    verbose=False,  # also print a line per item
)
def process(item): ...
```

Bad options fail immediately, at decoration time, rather than three hours into
a run:

```python
>>> @quarantine(halt_after=0)
ValueError: halt_after must be >= 1 or None, got 0
```

Two decorators with the *same* options share one instance — one set of
counters, one summary line:

```python
@quarantine(dir="bad")
def parse(row): ...


@quarantine(dir="bad")
def load(row): ...


parse.quarantine is load.quarantine  # True
```

## Choosing which errors to catch

`only` is an allowlist, `exclude` a denylist that wins over it:

```python
@quarantine(only=(ValueError, KeyError))  # anything else still crashes
def process(row): ...


@quarantine(exclude=(MemoryError,))  # everything except this
def process(row): ...
```

Four things are **never** quarantined, whatever you pass:
`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and quarantine's own
`QuarantineError` subclasses. Ctrl-C must interrupt, `sys.exit()` must exit,
and "halt, this looks systemic" must not be swallowed by the thing that said it.

## The circuit breaker

If 50 items fail *in a row*, the data is probably fine and your database is
not. Quarantining the remaining 9,950 items would produce noise, not
information, so the run stops instead:

```
quarantine.errors.SystemicFailure: ✋ 50 consecutive failures - this looks
systemic, not bad data. Halting.
   Last error: ConnectionError: db.internal:5432 refused
```

```python
@quarantine(halt_after=100)    # more patient
@quarantine(halt_after=None)   # no breaker at all
```

The streak resets on every success, and the item that trips the breaker is
still saved before the exception is raised. `SystemicFailure` carries `.count`
and `.last_error`, and is chained from the original error, so a `try/except`
around your loop can tell the operator exactly what happened:

```python
from quarantine import SystemicFailure

try:
    for row in rows:
        process(row)
except SystemicFailure as halt:
    alert(f"pipeline halted after {halt.count} failures: {halt.last_error}")
    raise
```

## The disk cap

`max_items` (default 10,000) stops the folder eating a disk. When it is
reached, the failure is **not** dropped — `QuarantineFull` is raised instead,
chained from the original exception:

```
quarantine.errors.QuarantineFull: quarantine is full: 10000 items already in
.quarantine. Fix and `quarantine retry`, run `quarantine clear`, or raise
max_items.
```

Successful retries free capacity again. `max_items=None` removes the cap.
    retries=2,         # retry transient failures
    backoff=0.5,       # delay between retries


## Redacting secrets

Field names listed in `redact` are replaced with `***REDACTED***` **before
anything is written**:

```python
@quarantine(redact=["password", "api_key", "*token*"])
def charge(payload, *, api_key): ...
```

- Matching is **case-insensitive** and supports globs: `*token*` catches
  `AccessToken` and `refresh_token`.
- Walking is recursive through dicts, lists, tuples, sets, namedtuples,
  dataclasses and plain objects' attributes.
- Keyword-argument *names* are matched too, so `api_key=` above is scrubbed
  even though the secret is not inside a container.
- Your object is **never mutated** — everything is rebuilt as a copy.
- What was scrubbed is recorded in `meta.json` (`"redacted": ["api_key"]`), so
  a later reader knows the input is incomplete on purpose.

Two caveats worth knowing:

1. Redaction happens *before* fingerprinting, so two inputs that differ **only**
   in a redacted field count as the same item for deduplication. That is a
   deliberate trade: no secret, not even a hash of one, reaches the disk.
2. It scrubs *fields*. A secret pasted into an exception *message* is part of
   the traceback; do not put credentials in error strings.

## Reruns and deduplication

Every call is fingerprinted (a hash of the redacted input plus the function
name). On a rerun, an input already sitting in quarantine is **skipped** — not
re-run, not re-recorded:

```
✓ 9,996 processed · ⏭ 4 skipped (already quarantined) → .quarantine/
```

This is what makes "just run it again" safe: the folder does not fill up with
duplicates, and your logs do not fill up with the same four errors. Fingerprints
are order-insensitive, so `{"a": 1, "b": 2}` and `{"b": 2, "a": 1}` are the same
item.

A record that a retry recovers is deleted, so the item is processed normally on
the next run. If you *want* the item re-attempted in place, pass
`skip_known_bad=False`.

## Retrying from Python

The CLI's `quarantine retry` is a thin wrapper around this:

```python
from quarantine import retry

result = retry()  # everything
result = retry([2, 5])  # specific ids
result = retry(function="parse_row")  # one function
result = retry(dry_run=True)  # what would be retried

result.recovered  # [1, 3] - deleted from the folder
result.still_failing  # [2] - attempts incremented, traceback refreshed
result.unretryable  # [(4, "reason")] - left untouched
```

Retries replay against the **undecorated** function, so a retry that fails
again updates the existing record instead of creating a second one.

When quarantine cannot import the function a record came from — a closure, a
`__main__` script, a renamed function — the record is reported in
`unretryable` and left alone. Point it at the right callable yourself:

```python
retry(using=my_fixed_function)
```

For records produced by `async def` functions, use `await aretry(...)` (the
synchronous `retry()` will run them with `asyncio.run` when no loop is
running, and tell you to use `aretry` when one is).

## Loops without a decorator

```python
from quarantine import shield

for result in shield(items, using=process):
    write(result)
```

`shield()` yields only the results that worked; failures are quarantined and
simply do not appear. It is lazy, and it takes the same options as the
decorator:

```python
shield(rows, using=parse, dir="bad-rows", halt_after=None)
```

`shield()` needs a callable. A generator physically cannot catch an exception
raised in your `for`-loop body, and pretending otherwise would be a decorator
that protects nothing — so `shield(items)` raises `TypeError` rather than
quietly doing less than you think.

For the same reason, `@quarantine` **refuses to wrap a generator function**: its
body runs during iteration, not during the call. You get a clear `TypeError`
pointing you at `shield()` instead of silent non-protection.

## Async

`@quarantine` detects `async def` and returns a coroutine function, so
`inspect.iscoroutinefunction()` still says yes and the decorator is invisible:

```python
@quarantine(halt_after=None)
async def fetch(url):
    async with session.get(url) as response:
        return await response.json()


results = await asyncio.gather(*(fetch(url) for url in urls))
good = [r for r in results if not is_quarantined(r)]
```

`ashield()` is the async loop form, over sync *or* async iterables:

```python
async for record in ashield(rows, using=load):
    ...
```

## Threads and processes

Safe in both, and the design is the reason:

- Record ids are allocated by *creating* the directory; a collision means
  someone else won the race, so the next id is taken.
- Each record is assembled in a hidden `.tmp-*` directory and **renamed** into
  place. The rename is the commit point, so a reader never sees a half-written
  record, and `kill -9` leaves an ignorable temp folder rather than corruption.
- `index.json` is written under a lock file, and is only ever a *cache*: if it
  is missing, stale or corrupt it is rebuilt from the record directories.
- Counters are guarded by a lock, so `stats` and the circuit breaker are
  accurate across threads.

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor() as pool:
    list(pool.map(process, items))  # process is @quarantine-decorated
```

With `multiprocessing`, each worker keeps its own counters (so you get a
summary line per process), but they share the folder correctly. Run
`quarantine reindex` afterwards if you want one tidy index.

## Alerting

```python
def notify(record):
    slack.post(f"{record.function} failed on {record.preview}: {record.summary}")


@quarantine(on_quarantine=notify)
def process(item): ...
```

The hook receives the `Record` after it is safely on disk. If your hook raises,
the exception is reported on stderr and the run continues — Slack being down is
not a reason to lose 9,996 items of work.

For a per-item line without writing a hook, use `verbose=True`.

## The explicit object

The decorator is sugar over a `Quarantine` object you can construct, inject and
inspect:

```python
from quarantine import Quarantine

q = Quarantine("build/bad-rows", halt_after=10, report=False)

safe = q.wrap(process)
q.call(process, row)
q.stats  # Stats(processed=…, quarantined=…, skipped=…, recovered=…)
q.records()  # list[Record]
q.summary_line()  # the one-liner, or None
q.replace(halt_after=None)  # a copy with one setting changed
```

Useful in tests: point it at `tmp_path`, pass `report=False`, and assert on
`q.records()`.

## Recipes

**Fail the job if anything got quarantined**

```python
import sys
from quarantine import records

for row in rows:
    process(row)

if records():
    sys.exit(f"{len(records())} rows need attention: quarantine list")
```

**Retry in the same run, after a pause** (e.g. a flaky API)

```python
import time
from quarantine import retry

for url in urls:
    fetch(url)

time.sleep(60)
result = retry(using=fetch.__wrapped__)
print(f"{len(result.recovered)} recovered on the second pass")
```

**Separate folders per stage**, so a parse failure and a load failure never mix

```python
@quarantine(dir=".quarantine/parse")
def parse(row): ...


@quarantine(dir=".quarantine/load")
def load(row): ...
```

**Quarantine data, crash on bugs**

```python
@quarantine(only=(ValueError, KeyError, UnicodeDecodeError))
def parse(row): ...
```

A `TypeError` or `AttributeError` is usually *your* bug, not the row's; letting
those crash keeps them from hiding in a folder.

**In a notebook**, where `report=True` at exit is not much use:

```python
from quarantine import quarantine, summary


@quarantine(verbose=True, report=False)
def process(item): ...


...
print(summary())
```

## Things it deliberately does not do

- **It does not retry automatically.** No backoff, no jitter, no schedule. One
  failure, one record, one deliberate retry when *you* have fixed something.
- **It does not swallow `KeyboardInterrupt`, `SystemExit` or its own errors.**
- **It is not a queue.** No broker, no workers, no ordering guarantees beyond
  "ids increase". If you need those, you need Celery or Kafka, and they have
  dead-letter queues already.
- **It does not touch your logging setup.** Output is a single stderr line, and
  you can turn it off.

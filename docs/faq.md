# FAQ

[← docs index](index.md)

## Is this just a dead-letter queue?

Yes — that is exactly the pattern, moved from message-queue infrastructure to a
plain Python `for`-loop. No broker, no server, no workers: one decorator and a
folder.

## Can I not just use try/except?

You are using `try/except` — quarantine *is* a `try/except`, plus the
bookkeeping you would otherwise write in every script forever: durable storage,
the traceback, the exact input, retry-only-the-failures, a circuit breaker,
deduplication, redaction. The [README](https://github.com/halcyon-past/quarantine#readme) has the side-by-side.

## Does it work on async functions?

Yes, and the wrapper stays a coroutine function, so `await`,
`asyncio.gather()` and `inspect.iscoroutinefunction()` all behave. `ashield()`
is the loop form. Records from async functions are retried with
`await aretry()`.

## Threads? Processes?

Both. Ids are allocated atomically by creating the folder, records are renamed
into place, and `index.json` is written under a lock file and can always be
rebuilt. Each process keeps its own counters, so you get one summary line per
process.

## What does a quarantined call return?

`QUARANTINED` — a falsy singleton — or `SKIPPED` if the input was already known
bad. Use `is_quarantined()` / `is_skipped()` to be explicit, or use `shield()`,
which only ever yields real results.

## Will it hide my bugs?

It files them, which is not the same thing — but the risk is real, and the
control is `only=`:

```python
@quarantine(only=(ValueError, KeyError, UnicodeDecodeError))
def parse(row): ...
```

Data errors get quarantined; a `TypeError` — usually *your* bug — still crashes.

## What if everything fails?

After 50 consecutive failures (`halt_after`) it stops and tells you the problem
looks systemic, rather than quarantining 10,000 items. That number is a guess
about your data; tune it.

## How big can the folder get?

`max_items` (10,000 by default) caps it. Hitting the cap raises
`QuarantineFull` — the failure is never silently dropped.

## Can I put secrets through it?

Name them and they are scrubbed before anything is written:

```python
@quarantine(redact=["password", "api_key", "*token*"])
```

Matching is case-insensitive, understands globs, walks containers, dataclasses
and object attributes, and never mutates your object. It cannot un-say a secret
you interpolated into an *exception message*, so do not do that.

## Does it need a database, a broker, or a config file?

No. Standard library only, zero runtime dependencies, no configuration.

## What does it cost?

A successful call costs a function call, one lock acquisition for the counters,
and — with `skip_known_bad=True` — a fingerprint of the input. A *failure* costs
a few small file writes and an `fsync`, which is the price of a record that
survives the crash. For very hot loops over large inputs,
`skip_known_bad=False` removes the per-item hashing. Measured numbers and the
methodology behind them are in [the benchmarks](benchmarks.md).

## Can I use it with Celery / Airflow / Kafka?

You can, but you probably should not: those have dead-letter queues already.
quarantine is for the 95% of scripts that will never justify that machinery.

## When should I *not* use it?

- When you want the crash. In a bank transfer pipeline, stopping on the first
  error may well be correct. Continuing is a choice — make it deliberately.
- When failures are expected and meaningless ("404 means skip"). Handle those
  with a normal `if`/`except`; quarantine is for *unexpected* failures you
  intend to investigate.

## Is the on-disk format stable?

The layout (`0001/input.pkl`, `input.txt`, `traceback.txt`, `meta.json`,
`index.json`) is part of the public interface and will not change incompatibly
within a major version. `meta.json` carries `meta_version`, extra keys are
ignored, and missing optional keys get defaults. See
[the on-disk format](on-disk-format.md).

## Which Pythons are supported?

3.10 and newer, on Linux, macOS and Windows — all tested in CI.

## How do I get rid of it?

```bash
quarantine clear --yes     # or: rm -rf .quarantine
pip uninstall quarantine
```

Then delete the decorator. Your function is unchanged underneath it.

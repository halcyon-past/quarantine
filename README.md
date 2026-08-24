# 🏥 quarantine

[![PyPI version](https://img.shields.io/pypi/v/quarantine-py.svg)](https://pypi.org/project/quarantine-py/)
[![Python versions](https://img.shields.io/pypi/pyversions/quarantine-py.svg)](https://pypi.org/project/quarantine-py/)
[![CI](https://github.com/halcyon-past/quarantine/actions/workflows/ci.yml/badge.svg)](https://github.com/halcyon-past/quarantine/actions/workflows/ci.yml)

**When one bad item crashes your loop of 10,000 — don't crash. Set it aside, keep going, fix it later.**

```bash
pip install quarantine-py
```

```python
from quarantine import quarantine


@quarantine
def process(item): ...  # your normal code, unchanged


for item in items:
    process(item)
```

That's it. Bad items no longer kill your job. They get saved to a `.quarantine/` folder — with their full error and the exact input that caused it — and your loop keeps running.

At the end:

```
✓ 9,996 processed · ✗ 4 quarantined → .quarantine/  (run `quarantine retry` after fixing)
```

---

## The problem (explained like you're new to this)

Imagine you're processing 10,000 records — rows from a CSV, URLs to scrape, images to resize. You write a loop:

```python
for item in items:
    process(item)
```

You run it. It works... until **item #5,247**, which is malformed in some way you didn't expect. Your script crashes. You lost 5,246 items of finished work and 3 hours.

So you fix the bug for that one weird item and rerun from the start. Three hours later it crashes again — at item #7,913, for a *different* reason.

This cycle is one of the most common, most painful experiences in programming. `quarantine` ends it.

## "Can't I just use try/except?"

Yes — try/except is exactly what quarantine uses under the hood. But here's the try/except version once you make it actually safe:

```python
failed = []
for item in items:
    try:
        process(item)
    except Exception as e:
        failed.append(item)  # ❌ lost forever if the script dies later
        print(f"failed: {e}")  # ❌ traceback gone — good luck debugging tomorrow
        # ❌ how do I re-run JUST these failures after I fix the bug?
        # ❌ what if 500 fail in a row because the API is down — keep going?!
        # ❌ how do I save a weird object (DataFrame row? bytes?) to look at later?
```

Every ❌ is a real problem you'd have to solve yourself, in every script, forever. quarantine solves them once:

| | `try/except` by hand | `@quarantine` |
|---|---|---|
| Loop survives bad items | ✅ | ✅ |
| Failures survive a crash/restart | ❌ in RAM, gone | ✅ saved to disk instantly |
| Full traceback kept for later | ❌ usually just printed | ✅ stored with the item |
| The exact bad input saved | ❌ you'd have to serialize it | ✅ automatic |
| Re-run *only* the failures | ❌ build it yourself | ✅ `quarantine retry` |
| Debug with the real bad input | ❌ archaeology in logs | ✅ `quarantine debug 2` |
| Detects "everything is failing, stop" | ❌ | ✅ halts on failure streaks |
| Skips already-known-bad items on rerun | ❌ | ✅ |

**quarantine is not a replacement for try/except. It's the 200 lines of bookkeeping you'd have to write around it — done correctly, once.**

## The idea in one picture

Hospitals don't shut down when one patient has an infection. They **quarantine** the patient, treat everyone else, and come back with the right medicine.

```
items ──▶ process() ──▶ ✓ done
              │
              ✗ raises an exception
              ▼
        .quarantine/          ◀── the "sick bay" folder
          ├── item + its data
          ├── the full error traceback
          └── when/why it failed
```

Your job finishes. The sick items wait for you, with their full medical charts.

---

## Installation

```bash
pip install quarantine-py
```

That is the whole install story. Some alternatives, if you prefer:

```bash
uv add quarantine-py            # uv projects
uv pip install quarantine-py    # uv, without a project
python -m pip install --user quarantine-py
pip install git+https://github.com/halcyon-past/quarantine   # unreleased main
```

**Requirements**

| | |
|---|---|
| Python | 3.10 or newer (CPython; tested on 3.10 - 3.13) |
| Runtime dependencies | none - it is standard library only |
| Operating systems | Linux, macOS, Windows (tested on all three in CI) |

Installing also puts a `quarantine` command on your `PATH`. Check both halves:

```bash
$ quarantine --version
quarantine 0.1.0
$ python -c "import quarantine; print(quarantine.__version__)"
0.1.0
```

If the command is not found (a common `--user` install wrinkle), the module
form always works and takes the same arguments:

```bash
python -m quarantine list
```

Nothing needs configuring. The first time a call fails, `.quarantine/` appears
next to wherever you started Python. Add it to your `.gitignore` -
**quarantined inputs are real data, and real data does not belong in git.**

```gitignore
.quarantine/
```

---

## Usage

### 1. Basic — decorate and forget

```python
from quarantine import quarantine


@quarantine
def process(row):
    price = float(row["price"])  # crashes on "N/A"? quarantined.
    save_to_db(row["id"], price)


for row in rows:
    process(row)
```

### 2. See what got quarantined

```bash
$ quarantine list
  #  when         function   error                          input preview
  1  09:14:02     process    ValueError: could not convert  {'id': 8812, 'price': 'N/A', ...}
  2  09:31:44     process    KeyError: 'price'              {'id': 9107, ...}
```

### 3. Fix your code, then retry only the failures

```bash
$ quarantine retry
✓ 3 recovered · ✗ 1 still failing (kept in quarantine)
```

No rerunning the 9,996 items that already worked. 

*(Note: If your function lives in a standalone script rather than an installed package, use `-i` to tell quarantine where to import it from: `quarantine retry -i my_script.py`)*

### 4. Debug with the actual bad input

```bash
$ quarantine debug 2
# opens a Python debugger with `item` set to the exact input that failed
```

*(You can use `-i` here too: `quarantine debug 2 -i my_script.py`)*

The single biggest time-saver: you never have to *reproduce* the bug. The bug's exact input is sitting on disk.

### 5. Safety valve — when it's not the data's fault

If 50 items fail **in a row**, that's not bad data — that's your database being down. Quarantining 10,000 items would be silly. quarantine halts instead:

```
✋ 50 consecutive failures — this looks systemic, not bad data. Halting.
   Last error: ConnectionError: db.internal:5432 refused
```

Tune it: `@quarantine(halt_after=100)`.

### 6. Options (all optional)

```python
@quarantine(
    dir=".quarantine",  # where the sick bay lives
    only=(ValueError, KeyError),  # only quarantine these; others still crash
    halt_after=50,  # consecutive-failure circuit breaker
    max_items=10_000,  # cap disk usage
    redact=["api_key", "password"],  # scrub these fields before saving inputs
    on_quarantine=my_alert_fn,  # e.g., send a Slack ping
)
def process(item): ...
```

### 7. Works on loops too, without a decorator

```python
from quarantine import shield

for item in shield(items, using=process):
    ...
```

---

## What's in the `.quarantine/` folder?

Plain files. No database, no magic — you can inspect everything yourself:

```
.quarantine/
├── 0001/
│   ├── input.pkl        # the exact item (pickle, JSON fallback for simple data)
│   ├── input.txt        # human-readable repr, so you can just *look* at it
│   ├── traceback.txt    # full error, exactly as it would have printed
│   └── meta.json        # function name, timestamp, attempt count, python/pkg versions
└── index.json
```

Design rules:
- **Atomic writes** — a crash mid-save never corrupts the folder.
- **Redaction before disk** — fields you mark as secret never touch the filesystem.
- **Dedup on rerun** — an item already in quarantine is skipped (no log spam), unless you `quarantine retry` it.
- **Serialization fallbacks** — pickle → JSON → repr. Something readable is *always* saved, even for exotic objects.

---

---

## Command-line reference

Every command takes `-d/--dir PATH` (default: `$QUARANTINE_DIR`, else
`./.quarantine`), and `list`, `show`, `retry` and `stats` all take `--json` so
you can pipe them somewhere useful.

| Command | What it does |
|---|---|
| `quarantine list` | Table of everything quarantined. `ls` works too. `-f/--function NAME`, `-n/--limit N`. |
| `quarantine show ID [ID...]` | One record in full: metadata, the input, the whole traceback. |
| `quarantine retry [ID...]` | Re-run records; delete the ones that now succeed. `-f/--function NAME`, `--dry-run`, `-i/--import FILE.py` (for functions that live in a script). |
| `quarantine debug ID` | Re-run one record and drop you into `pdb` **on the frame that raised**. `-p/--print` to just dump it, `--no-post-mortem` to skip re-running and get the input in scope, `-i/--import FILE.py` as above. |
| `quarantine clear [ID...]` | Delete records. With no ids it clears everything and asks first; `-y/--yes` skips the prompt. `rm` works too. |
| `quarantine stats` | Counts by function and by error type, plus how much disk the folder is using. |
| `quarantine reindex` | Rebuild `index.json` from the record folders and sweep up leftover temp files from a hard crash. |

Exit codes, for scripts and CI:

| Code | Meaning |
|---|---|
| `0` | Everything you asked for succeeded. |
| `1` | The command ran, but something is still wrong - a retry failed again, or a record could not be replayed. |
| `2` | Bad usage, or the folder could not be read. |

```bash
# fail a nightly job if anything is still sitting in quarantine
quarantine retry || echo "still broken - look at: $(quarantine list -n 3)"
```

## Python API reference

```python
from quarantine import quarantine, shield, Quarantine, QUARANTINED, records, retry
```

**Decorating and looping**

| | |
|---|---|
| `@quarantine` / `@quarantine(...)` | Wrap one function. Options are listed under [Options](#6-options-all-optional). Works on `async def`. |
| `shield(items, using=fn, **options)` | Iterator yielding only the results that worked. |
| `ashield(items, using=fn, **options)` | Same, for `async def` work and/or async iterables. |

**Return values.** A quarantined call returns the `QUARANTINED` sentinel; an
input recognised as already-bad returns `SKIPPED`. Both are falsy, so
`if process(item):` does the sensible thing. Use `is_quarantined(result)` /
`is_skipped(result)` when you want to be explicit.

**The explicit object**, when you would rather pass something around than rely
on a decorator:

```python
from quarantine import Quarantine

q = Quarantine("build/bad-rows", halt_after=10, redact=["api_key"])

safe = q.wrap(process)  # same as the decorator
q.call(process, item)  # one-off call, same protection
await q.acall(fetch, url)  # async one-off
q.records()  # list[Record], oldest first
q.retry()  # -> RetryResult(recovered, still_failing, unretryable)
await q.aretry()  # for records from async functions
q.clear()  # empty the folder
q.stats  # Stats(processed, quarantined, skipped, recovered)
q.summary_line()  # the one-line report, or None
len(q), list(q)  # how many records; iterate them
```

**Module-level shortcuts** operate on the default folder (or `dir=`):
`records()`, `retry()`, `aretry()`, `clear()`, `summary()`.

**A `Record`** is what you get back from `records()`:

```python
record = records()[0]
record.id  # 1
record.function  # "process" (qualified name in .qualified_name)
record.error_type  # "ValueError"
record.summary  # "ValueError: could not convert string to float: 'N/A'"
record.attempts  # 2, after one retry
record.redacted  # ["api_key"] - what was scrubbed
record.path  # Path(".quarantine/0001")
record.traceback_text()  # the stored traceback
record.load_call()  # Call(args=({...},), kwargs={}) - the original input
record.load_call().item  # the item itself
```

**Exceptions** (all subclass `QuarantineError`, and none of them are ever
quarantined themselves):

| | |
|---|---|
| `SystemicFailure` | The `halt_after` circuit breaker tripped. `.count`, `.last_error`. |
| `QuarantineFull` | `max_items` reached. Nothing is dropped silently - this is raised instead, chained from the original error. |
| `StorageError` | The folder could not be read or written. |
| `ResolutionError` | A retry could not import the function a record came from. |

**Environment**

| | |
|---|---|
| `QUARANTINE_DIR` | Default folder for both the library and the CLI. |

## Documentation

| | |
|---|---|
| [docs/installation.md](https://github.com/halcyon-past/quarantine/blob/main/docs/installation.md) | Installing, verifying, upgrading, uninstalling. |
| [docs/usage.md](https://github.com/halcyon-past/quarantine/blob/main/docs/usage.md) | The full guide: options, async, threads, retry loops, alerting, recipes. |
| [docs/examples.md](https://github.com/halcyon-past/quarantine/blob/main/docs/examples.md) | Practical real-world examples and the file structure behind the scenes. |
| [docs/cli.md](https://github.com/halcyon-past/quarantine/blob/main/docs/cli.md) | Every command, flag and exit code, with output samples. |
| [docs/api.md](https://github.com/halcyon-past/quarantine/blob/main/docs/api.md) | Complete Python API reference. |
| [docs/on-disk-format.md](https://github.com/halcyon-past/quarantine/blob/main/docs/on-disk-format.md) | What is in `.quarantine/`, and the guarantees about it. |
| [docs/troubleshooting.md](https://github.com/halcyon-past/quarantine/blob/main/docs/troubleshooting.md) | "It skipped my item", "retry says it cannot import", and friends. |
| [docs/faq.md](https://github.com/halcyon-past/quarantine/blob/main/docs/faq.md) | Longer answers to the questions below. |
| [CHANGELOG.md](https://github.com/halcyon-past/quarantine/blob/main/CHANGELOG.md) | What changed, and when. |
| [CONTRIBUTING.md](https://github.com/halcyon-past/quarantine/blob/main/CONTRIBUTING.md) | Setup, the checks, and the ground rules for changes. |

## When NOT to use quarantine

Honesty section:

- **You want the crash.** In a bank transfer pipeline, stopping on the first error might be correct. Silently continuing is a choice — make it deliberately.
- **You're already on Celery/Kafka/Airflow.** Those have dead-letter queues; use them. quarantine is for the 95% of scripts that will never justify that machinery.
- **Failures are expected and normal** (e.g., "404 means skip"). Handle those with a normal `if`/`except` — quarantine is for *unexpected* failures you'll want to investigate.

## FAQ

**Is this just a dead-letter queue?**
Yes — that's exactly the pattern, ported from message-queue infrastructure to a plain Python for-loop. No broker, no server, one decorator.

**Async?**
`@quarantine` works on `async def` too.

**Threads/processes?**
Yes — writes are atomic and the folder is append-only per item.

---

**Created by [Aritro Saha](https://aritro.cloud)**

*Made for everyone whose overnight job died at item 5,247.*


## Developer Reference #12
Resolves issue #12: Feature Request: Local Web Dashboard (quarantine ui).

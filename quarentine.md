# 🏥 quarantine

**When one bad item crashes your loop of 10,000 — don't crash. Set it aside, keep going, fix it later.**

```bash
pip install quarantine
```

```python
from quarantine import quarantine

@quarantine
def process(item):
    ...  # your normal code, unchanged

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
        failed.append(item)          # ❌ lost forever if the script dies later
        print(f"failed: {e}")        # ❌ traceback gone — good luck debugging tomorrow
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

## Usage

### 1. Basic — decorate and forget

```python
from quarantine import quarantine

@quarantine
def process(row):
    price = float(row["price"])     # crashes on "N/A"? quarantined.
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

### 4. Debug with the actual bad input

```bash
$ quarantine debug 2
# opens a Python debugger with `item` set to the exact input that failed
```

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
    dir=".quarantine",        # where the sick bay lives
    only=(ValueError, KeyError),  # only quarantine these; others still crash
    halt_after=50,            # consecutive-failure circuit breaker
    max_items=10_000,         # cap disk usage
    redact=["api_key", "password"],  # scrub these fields before saving inputs
    on_quarantine=my_alert_fn,       # e.g., send a Slack ping
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

*Made for everyone whose overnight job died at item 5,247.*

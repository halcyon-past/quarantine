# Framework recipes

[← docs index](index.md)

Worked integrations for the places quarantine earns its keep. These are
recipes rather than plugins, deliberately: an operator or extension per
framework is a permanent maintenance treadmill against someone else's release
cycle, while a page of working code delivers the value at none of the cost.
Shorter, single-feature examples live in [examples.md](examples.md); the
library idioms they build on are in the [usage guide](usage.md).

Each recipe was run against a real quarantine folder before it was written
down.

## A pandas ETL job

One malformed row should not cost a 500,000-row load. Clean row-wise through
[`shield()`](usage.md#loops-without-a-decorator), which yields only the rows
that worked, and rebuild the frame from the survivors:

```python
# etl.py
import pandas as pd

from quarantine import shield


def clean(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "price": float(row["price"]),  # raises on "N/A" - quarantined, loop continues
        "qty": int(row["qty"]),
        "total": float(row["price"]) * int(row["qty"]),
    }


def run(csv_path: str) -> pd.DataFrame:
    raw = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    good = shield(
        raw.to_dict("records"), using=clean, dir=".quarantine/etl", redact=["card_number"]
    )
    return pd.DataFrame(list(good))


if __name__ == "__main__":
    frame = run("orders.csv")
    frame.to_parquet("orders.parquet")
```

Two details are load-bearing:

- **`dtype=str, keep_default_na=False`** — left to its defaults, `read_csv`
  silently turns junk like `"N/A"` into `NaN`, and `float(nan)` does *not*
  raise, so the bad row would sail through as NaNs instead of being
  quarantined. Reading raw strings makes `clean()` the one place where
  parsing happens — and failures surface.
- **`to_dict("records")` instead of `df.apply`** — a plain dict pickles
  cleanly and reads well in `input.txt`, and `apply` would abort the whole
  column on the first bad value: the opposite of what you want.

Afterwards, from the same directory:

```bash
quarantine list --dir .quarantine/etl
# fix clean() ...
quarantine retry --dir .quarantine/etl --import etl.py
```

`--import etl.py` is needed because `clean` lives in a script that ran as
`__main__` (the [CLI reference](cli.md) explains why). The recovered rows are
deleted from the folder; append them to the parquet on the next run, or re-run
the whole job — [deduplication](usage.md#reruns-and-deduplication) skips the
rows that are still bad instead of re-recording them.

## A web scraper

Scrapers meet two very different failures: a wobbly network (retry it) and a
genuinely broken page (quarantine it and move on). One decorator handles both,
and the [circuit breaker](usage.md#the-circuit-breaker) covers the third case —
the whole site going down:

```python
# scraper.py
import requests

from quarantine import quarantine


@quarantine(
    dir=".quarantine/scrape",
    retries=2,  # a timeout is not a bad page: try again first
    backoff=1.0,
    backoff_factor=2.0,  # 1s, then 2s
    jitter=0.5,  # parallel scrapers back off out of step
    halt_after=20,  # 20 failures in a row = the site is down, stop
)
def fetch(url: str) -> str:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.text


if __name__ == "__main__":
    for url in load_urls():
        page = fetch(url)
        if page:  # QUARANTINED is falsy, so failures skip the parse
            parse(page)
```

A page that 404s twice lands in quarantine with the exact URL as its input; a
page that timed out once and loaded on the second attempt costs nothing. When
the site's bug is fixed, replay only the failures:

```bash
quarantine retry --dir .quarantine/scrape --import scraper.py
```

For a long-running crawl, add `dead_after=5` so a permanently broken URL stops
being re-attempted by every blanket retry — see
[poison items](usage.md#poison-items).

## An Airflow task

Keep the load-bearing code in a plain module — importable by the Airflow
worker, by your laptop, and by `quarantine retry` alike — and keep the DAG
wiring thin:

```python
# etl_tasks.py - no Airflow imports, so `quarantine retry --import` works anywhere
from quarantine import Quarantine

QDIR = "/shared/quarantine/nightly_load"  # a volume workers and your laptop both see


def load_row(row: dict) -> None: ...  # your real work


def load_partition(rows: list[dict]) -> int:
    q = Quarantine(QDIR, halt_after=100, report=False)
    load = q.wrap(load_row)
    for row in rows:
        load(row)
    if len(q):
        raise RuntimeError(
            f"{len(q)} rows quarantined - inspect with: quarantine list --dir {QDIR}"
        )
    return q.stats.processed
```

```python
# dags/nightly_load.py
import pendulum
from airflow.decorators import dag, task

from etl_tasks import load_partition


@dag(schedule="@daily", start_date=pendulum.datetime(2026, 1, 1))
def nightly_load():
    @task(retries=2)
    def load(rows: list[dict]) -> int:
        return load_partition(rows)

    load(rows=[])  # wire up your real source here


nightly_load()
```

How the pieces interact, because this is where quarantine and Airflow fit
together unusually well:

- **The task processes everything it can, then fails loudly.** Every good row
  loads, every bad row is preserved with its traceback, and the red task is
  what makes a human look — nothing is silently dropped.
- **Airflow's own task retries become cheap.** On the re-run,
  [deduplication](usage.md#reruns-and-deduplication) skips rows already in
  quarantine instead of re-failing them. (Good rows *do* re-run — keep
  `load_row` idempotent, which an Airflow task should be anyway.)
- **`halt_after=100`** turns "the warehouse is down" into one fast
  `SystemicFailure` instead of a partition's worth of identical records.
- **`report=False`** because the end-of-run summary line is for terminals;
  the task's return value and the raise carry the numbers into Airflow's UI.

Point `QDIR` at storage that outlives the worker pod. Inspect and replay from
anywhere that mounts it: `quarantine list --dir /shared/quarantine/nightly_load`.

## A FastAPI background worker

A `BackgroundTasks` failure happens *after* the response is sent, so by
default it is a log line nobody reads. Quarantined, it is a record you can
inspect and a button you can click:

```python
# app.py
from fastapi import BackgroundTasks, FastAPI

from quarantine import quarantine

app = FastAPI()


@quarantine(dir=".quarantine/receipts", retries=2, backoff=1.0, report=False)
def send_receipt(order: dict) -> None: ...  # render and email the receipt


@app.post("/orders")
def create_order(order: dict, background: BackgroundTasks) -> dict:
    background.add_task(send_receipt, order)
    return {"status": "accepted"}
```

The endpoint keeps returning `202`-shaped answers whatever the receipt does:
a transient SMTP wobble is retried, a genuinely bad order is quarantined with
its payload, and the request/response path never sees the failure.

- `report=False` — a server never exits, so the exit summary would never
  print. Wire [`on_quarantine=`](observability.md) to your metrics instead.
- Multiple uvicorn workers share one folder safely — writes are atomic and
  ids cannot collide (see [the on-disk format](on-disk-format.md)).
- Inspect and replay visually while the server runs:
  `quarantine ui --dir .quarantine/receipts`, or from the CLI:
  `quarantine retry --dir .quarantine/receipts --import app.py`.

## The common shape

All four recipes are the same three decisions:

1. **A folder per concern** (`dir=`), so a scrape failure and a load failure
   never mix.
2. **Retries for transient noise, quarantine for real failures** — the
   split between `retries=` and the record.
3. **A replay path that works after the process is gone** — an importable
   function (or `--import file.py`) and a folder that survives the run.

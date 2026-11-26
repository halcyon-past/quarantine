---
title: Redis Backend, TLS, Cluster & TTL Guide · quarantine-py
description: Complete guide for Redis dead-letter queues. redis:// and rediss:// TLS URLs, prefix isolation, automatic TTL expiration, atomic MULTI/EXEC writes, and local testing.
---

# Redis Remote Storage

[← remote storage overview](remote-storage.md) · [docs index](index.md)

The Redis backend gives short-lived, high-churn, or low-latency workloads **one shared dead-letter queue** backed by Redis. Workers write quarantined records atomically as Redis hashes using `MULTI`/`EXEC` transactions, and operators inspect, diagnose, and replay them from any terminal or the local web dashboard.

---

## Installation & Quick Start

The core `quarantine-py` library has zero runtime dependencies. To use Redis, install the optional `redis` extra (which installs `redis`):

```bash
pip install "quarantine-py[redis]"
```

Use a `redis://` (or `rediss://` for TLS) URL anywhere a local folder path is accepted:

### In Python Code

```python
from quarantine import quarantine


@quarantine(
    dir="redis://localhost:6379/0?prefix=etl:quarantine&ttl=3600",
    halt_after=30,
    report=True,
)
def process_event(event: dict) -> None:
    if not event.get("id"):
        raise ValueError("Missing event id")
    # ... process item ...
```

### In Asynchronous Code

```python
from quarantine import ashield


async def main():
    async for item in ashield(
        fetch_stream(),
        using=handle_stream_item,
        dir="redis://localhost:6379/0?prefix=etl:stream",
    ):
        print("Processed successfully:", item)
```

### From the Command-Line Interface (CLI)

```bash
# List all quarantined records under the prefix
quarantine list --dir "redis://localhost:6379/0?prefix=etl:quarantine"

# Show details of record #1
quarantine show 1 --dir "redis://localhost:6379/0?prefix=etl:quarantine"

# Inspect Python traceback
quarantine debug 1 --dir "redis://localhost:6379/0?prefix=etl:quarantine"

# Replay and retry failed inputs after deploying a fix
quarantine retry --dir "redis://localhost:6379/0?prefix=etl:quarantine" --import worker.py

# Launch the visual web dashboard
quarantine ui --dir "redis://localhost:6379/0?prefix=etl:quarantine" --port 8080
```

> **Tip**: Set `QUARANTINE_DIR` to avoid passing `--dir` on every CLI invocation (credentials in the URL are masked in `self.dir`, `__repr__`, and error output):
> ```bash
> export QUARANTINE_DIR="redis://localhost:6379/0?prefix=etl:quarantine"
> quarantine list
> ```

---

## URL Format & Query Parameters

```
redis[s]://[[username:]password@]host[:port][/db][?prefix=<ns>&ttl=<seconds>]
```

| Parameter | Default | Description |
| :--- | :--- | :--- |
| Scheme | — | `redis://` for plain TCP, `rediss://` for TLS. |
| `db` | `0` | Redis logical database index. |
| `prefix` | `quarantine` | Key namespace prefix. Use different prefixes to isolate pipelines sharing one Redis instance. |
| `ttl` | `None` (no expiry) | Positive integer TTL in seconds applied to each record hash (`EXPIRE`). |

---

## Key Layout & Atomicity

1. **ID allocation:** Record IDs are allocated atomically via `INCR` on `{<prefix>}:next_id`, skipping any ID that already exists.
2. **Atomic commit:** All record fields (`meta.json`, `traceback.txt`, `input.txt`, and `input.pkl`/`input.json`) are written together inside a `MULTI`/`EXEC` pipeline on `{<prefix>}:record:<id>`. Readers never observe half-written records.
3. **Concurrent updates:** `update()` and `write_traceback()` use `WATCH`/`MULTI`/`EXEC` on the record key so concurrent retries fail cleanly instead of silently overwriting each other.
4. **Redis Cluster compatibility:** Keys use hash tags (`{<prefix>}:record:<id>`, `{<prefix>}:next_id`) so all keys for one quarantine map to the same hash slot.

---

## Authentication & TLS

* **Password / ACL auth:** Pass `redis://:password@host:6379/0` or `redis://username:password@host:6379/0`. Credentials are automatically redacted from `store.dir`, `repr(store)`, and error messages.
* **TLS (`rediss://`):** Use `rediss://` for managed Redis services (AWS ElastiCache, Azure Cache for Redis, Upstash, Redis Cloud).
* **TTL & Memory Management:** For high-volume pipelines, pass `?ttl=86400` (or another retention window) so old dead-letter records expire automatically without unbounded Redis memory growth.

---

## Local Testing with Docker

```bash
docker run -d --name quarantine-redis -p 6379:6379 redis:7-alpine
export QUARANTINE_DIR="redis://localhost:6379/0?prefix=dev:quarantine"
```

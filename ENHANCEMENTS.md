# Enhancements

This is the forward-looking companion to [CHANGELOG.md](CHANGELOG.md): the
changes we are about to make, in the order we intend to make them, and the
reasoning behind each one. Items graduate from here into the changelog when
they ship. If you want to influence any of this, open an issue - the storage
backend work, for example, started as
[#11](https://github.com/halcyon-past/quarantine/issues/11).

## Already in place (the baseline)

Before the roadmap, what v0.2.0 already gives you - each links to the page
that explains it properly:

| Feature | Where it is documented |
|---|---|
| `@quarantine` on sync and `async def` functions, `shield()` / `ashield()` | [docs/usage.md](docs/usage.md) |
| Crash-safe atomic writes (temp dir + rename; a `kill -9` can never corrupt the folder) | [docs/on-disk-format.md](docs/on-disk-format.md) |
| Serialization fallback chain: pickle → JSON → `repr`, so something readable is always saved | [docs/on-disk-format.md](docs/on-disk-format.md) |
| Secret redaction (`redact=[...]`), applied before anything touches the disk | [docs/usage.md](docs/usage.md) |
| Transient retries (`retries=`, `backoff=`) before an item is quarantined | [docs/usage.md](docs/usage.md) |
| Deduplication: known-bad items are skipped on reruns | [docs/usage.md](docs/usage.md) |
| Circuit breaker (`halt_after=`) and disk cap (`max_items=`) | [docs/api.md](docs/api.md) |
| `on_quarantine=` hook for alerting | [docs/api.md](docs/api.md) |
| Full CLI: `list`, `show`, `retry`, `debug`, `clear`, `stats`, `reindex`, `ui` - stable exit codes, `--json` everywhere | [docs/cli.md](docs/cli.md) |
| Local web dashboard (`quarantine ui`) | [docs/cli.md](docs/cli.md) |
| Multi-process / multi-thread safety, proven with real processes in the test suite | [docs/faq.md](docs/faq.md) |
| Zero runtime dependencies, full type hints, `py.typed`, strict mypy | [docs/installation.md](docs/installation.md) |

The test suite enforces more than coverage: documentation drift fails the
build (every CLI flag, public name and documented default is checked against
the code), and an end-to-end regression suite replays whole user journeys -
real subprocesses, the installed CLI, real HTTP against the dashboard.

## 1. Pluggable storage backends

**The problem.** `.quarantine/` lives on the local disk. That is exactly right
for a script on your laptop, and exactly wrong for Kubernetes, AWS Lambda,
Docker, or any fleet of workers whose disks evaporate with the container.

**The plan.** A storage backend interface, selected by the same `dir=` you
already use - a URL picks the backend, a plain path keeps today's behaviour:

```python
@quarantine(dir="s3://my-bucket/quarantine")
def process(item):
    ...
```

```bash
quarantine list --dir s3://my-bucket/quarantine
quarantine retry --dir s3://my-bucket/quarantine --import job.py
```

Distributed workers quarantine into one shared store; you inspect and retry
from your laptop.

**Install.** The core stays zero-dependency - `pip install quarantine-py`
keeps working exactly as it does today, local folder only. Each backend is an
optional extra that pulls in only its own client library:

```bash
pip install quarantine-py                # local folder backend (today's behaviour)
pip install "quarantine-py[s3]"          # + Amazon S3            (boto3)
pip install "quarantine-py[gcs]"         # + Google Cloud Storage (google-cloud-storage)
pip install "quarantine-py[azure]"       # + Azure Blob Storage   (azure-storage-blob)
pip install "quarantine-py[redis]"       # + Redis                (redis)
pip install "quarantine-py[databricks]"  # + Databricks Unity Catalog volumes (databricks-sdk)
pip install "quarantine-py[all]"         # everything above
```

| Backend | URL form | Notes |
|---|---|---|
| Local folder | `/path/to/.quarantine` | The default. Unchanged. |
| Amazon S3 | `s3://bucket/prefix` | Per-record objects; no atomic rename exists on S3, so commit-last-write ordering replaces it (see the ADR). |
| Google Cloud Storage | `gs://bucket/prefix` | Same object layout as S3. |
| Azure Blob Storage | `azure://container/prefix` | Same object layout as S3. |
| Redis | `redis://host:6379/0` | For short-lived, high-churn quarantines; records under a key prefix with optional TTL. |
| Databricks | `/Volumes/catalog/schema/volume/quarantine` | Unity Catalog volumes, so quarantined items are governed, shareable and inspectable next to the data they came from. |

**Design constraints we will not trade away:**

- The local format does not change. Records stay self-describing plain files.
- Object stores have no atomic rename, so the commit point becomes writing
  `meta.json` last: a record is visible only once it is complete, which
  preserves today's "a reader never sees a partial record" guarantee.
- The interface is public and documented, so a third party can ship their own
  backend without touching this package.
- Every backend passes the same regression journeys the local store passes.

## 2. Retry engine upgrades

- **Exponential backoff with jitter**: `backoff=` grows per attempt instead of
  sleeping a fixed interval, so a struggling downstream service is not hammered
  on a fixed beat.
- **Poison-item detection**: a record that has failed retry `N` times is marked
  dead and excluded from blanket `quarantine retry` (still retryable by
  explicit id). The `attempts` counter that powers this is already on disk.
- **More hooks**: `on_retry_success=` and `on_retry_failure=` alongside the
  existing `on_quarantine=`, so Sentry, Prometheus or plain logging can watch
  the full lifecycle of a record, not just its birth.
- **Observability recipes**
  ([#14](https://github.com/halcyon-past/quarantine/issues/14)): documented,
  copy-pasteable hook wiring for Prometheus (`Counter` + Pushgateway for batch
  jobs), Datadog (statsd) and Sentry. The hooks are the integration point -
  a three-line `on_quarantine=` gets you `quarantine_items_total` labelled by
  function and error type, with your process still owning its own servers and
  ports (see *Considered and rejected* below on why the library will not run
  a metrics server for you).

## 3. Property-based tests (Hypothesis)

The library's one promise is *never lose a failure*. Hypothesis lets us state
that as a property over inputs we did not think of: any object it can
construct either round-trips through serialization or degrades losslessly to a
readable fallback - and redaction never leaks a value and never mutates the
caller's object, no matter the shape it is buried in.

## 4. Architecture decision records

A `docs/adr/` folder capturing the decisions already made and the ones the
backend work will force: why rename-based atomicity instead of locks, why a
directory per record instead of a single JSONL file, why pickle-first
serialization, why zero runtime dependencies, and how the commit point
translates to object stores.

## 5. Security posture

- `SECURITY.md` with a disclosure policy.
- CI actions pinned to commit SHAs instead of mutable tags.
- PyPI attestations on release (publishing is already keyless via OIDC
  trusted publishing; attestations are the remaining flag).

## 6. Documentation site and recipes

The `docs/` pages, rendered with mkdocs-material and published on every merge,
plus recipes for the places quarantine earns its keep: a pandas ETL job, a web
scraper, an Airflow task, a FastAPI background worker.

## 7. Benchmarks

A published, reproducible answer to "what does the decorator cost me when
nothing fails?" - overhead per successful call against a bare `try/except`,
measured with `pyperf`, with the methodology alongside the numbers.

## Considered and rejected

Decisions worth recording so they are not re-litigated by default:

- **Replacing the folder format with JSONL.** Appends to a shared file need
  locks where atomic renames need none; a torn append corrupts every reader,
  while a torn record today is invisible; pickled payloads are binary; and
  deleting one record from the middle of a file means compaction. The folder
  stays the source of truth. A `quarantine export --jsonl` command may arrive
  for shipping metadata into log pipelines - additive, not a replacement.
- **A distributed queue mode (SQS-style leases and visibility timeouts).**
  That is a message broker, and building a small one inside an error-handling
  library serves neither well. The shared-store backends above cover the
  fleet use case without inventing coordination.
- **Framework plugins (Airflow operators, Dagster resources).** Each one is a
  permanent maintenance treadmill against someone else's release cycle. A
  recipe in the docs delivers most of the value at none of the cost.
- **An embedded metrics server (`metrics_port=`,
  [#14](https://github.com/halcyon-past/quarantine/issues/14)).** The need is
  real; the mechanism is not. A library that opens an HTTP port as a decorator
  side effect fights its own core use cases: under `multiprocessing` every
  worker would race to bind the same port, batch jobs exit before Prometheus
  can scrape them (Pushgateway - a hook - is the correct Prometheus answer for
  batch), and nobody owns the server's lifecycle or its security surface. The
  `on_quarantine=` / `on_retry_*` hooks plus the recipes above deliver the
  same metrics with the application, not the library, owning the port.
- **A `[tool.quarantine]` pyproject section.** Ten keyword arguments and one
  environment variable do not justify config-file discovery, precedence rules
  and a TOML parser in a zero-dependency library.

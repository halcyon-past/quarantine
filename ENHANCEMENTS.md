# Enhancements

This is the forward-looking companion to [CHANGELOG.md](CHANGELOG.md): the
changes we are about to make, in the order we intend to make them, and the
reasoning behind each one. Items graduate from here into the changelog when
they ship — the retry engine upgrades, property-based tests, decision records,
security posture and documentation site that used to be sections here all
shipped in **0.3.0**. If you want to influence any of this, open an issue —
the storage backend work, for example, started as
[#11](https://github.com/halcyon-past/quarantine/issues/11).

## Already in place (the baseline)

What v1.0.0 already gives you — each links to the page that explains it
properly:

| Feature | Where it is documented |
|---|---|
| `@quarantine` on sync and `async def` functions, `shield()` / `ashield()` | [docs/usage.md](docs/usage.md) |
| Crash-safe atomic writes (temp dir + rename; a `kill -9` can never corrupt the folder) | [docs/on-disk-format.md](docs/on-disk-format.md), [ADR 0001](docs/adr/0001-rename-based-atomicity.md) |
| Serialization fallback chain: pickle → JSON → `repr`, so something readable is always saved | [docs/on-disk-format.md](docs/on-disk-format.md), [ADR 0003](docs/adr/0003-pickle-first-serialization.md) |
| Secret redaction (`redact=[...]`), applied before anything touches the disk | [docs/usage.md](docs/usage.md) |
| Transient retries with exponential backoff and jitter (`retries=`, `backoff=`, `backoff_factor=`, `jitter=`) | [docs/usage.md](docs/usage.md) |
| Poison-item detection (`dead_after=`, `retry --dead-after`) | [docs/usage.md](docs/usage.md), [ADR 0005](docs/adr/0005-stateless-poison-detection.md) |
| Deduplication: known-bad items are skipped on reruns | [docs/usage.md](docs/usage.md) |
| Circuit breaker (`halt_after=`) and disk cap (`max_items=`) | [docs/api.md](docs/api.md) |
| Lifecycle hooks (`on_quarantine=`, `on_retry_success=`, `on_retry_failure=`) with Prometheus / Datadog / Sentry recipes | [docs/observability.md](docs/observability.md) |
| Full CLI: `list`, `show`, `retry`, `debug`, `clear`, `stats`, `reindex`, `ui` — stable exit codes, `--json` everywhere | [docs/cli.md](docs/cli.md) |
| Local web dashboard (`quarantine ui`) | [docs/cli.md](docs/cli.md) |
| Multi-process / multi-thread safety, proven with real processes in the test suite | [docs/faq.md](docs/faq.md) |
| Zero runtime dependencies, full type hints, `py.typed`, strict mypy | [docs/installation.md](docs/installation.md), [ADR 0004](docs/adr/0004-zero-runtime-dependencies.md) |
| Framework recipes: pandas ETL, web scraping, Airflow, FastAPI ([#43](https://github.com/halcyon-past/quarantine/issues/43)) | [docs/recipes.md](docs/recipes.md) |
| Reproducible success-path benchmarks with pyperf ([#44](https://github.com/halcyon-past/quarantine/issues/44)) | [docs/benchmarks.md](docs/benchmarks.md) |
| Pluggable storage backends with S3 built in ([#11](https://github.com/halcyon-past/quarantine/issues/11)): `dir="s3://bucket/prefix"`, `quarantine-py[s3]`, public `StorageBackend` interface | [docs/remote-storage.md](docs/remote-storage.md) |
| Architecture decision records | [docs/adr/](docs/adr/README.md) |
| Security policy, SHA-pinned CI, trusted publishing with attestations | [SECURITY.md](SECURITY.md) |

The test suite enforces more than coverage: documentation drift fails the
build (every CLI flag, public name and documented default is checked against
the code), an end-to-end regression suite replays whole user journeys — real
subprocesses, the installed CLI, real HTTP against the dashboard — and a
Hypothesis suite hunts for counterexamples to the load-bearing invariants.
The docs are built with mkdocs-material and deployed on every merge.

## 1. More storage backends

The backend interface and the S3 backend shipped
([#11](https://github.com/halcyon-past/quarantine/issues/11),
[docs/remote-storage.md](docs/remote-storage.md)): `dir="s3://bucket/prefix"`
gives a fleet one shared quarantine, ids are claimed with conditional writes,
and `meta.json` uploaded last is the commit point
([ADR 0007](docs/adr/0007-object-store-commit-point.md)). What remains is
more built-in schemes, each an optional extra that pulls in only its own
client library:

| Backend | URL form | Notes |
|---|---|---|
| Google Cloud Storage | `gs://bucket/prefix` | Same object layout and commit protocol as S3 (`If-None-Match` is supported there too). `quarantine-py[gcs]`. |
| Azure Blob Storage | `azure://container/prefix` | Same object layout and commit protocol as S3. `quarantine-py[azure]`. |
| Redis | `redis://host:6379/0` | Not an object store: hash-per-record with `SETNX` id allocation and optional TTL, in its own ADR. `quarantine-py[redis]`. |
| Databricks | `/Volumes/catalog/schema/volume/quarantine` | Unity Catalog volumes, so quarantined items are governed and shareable next to the data they came from. `quarantine-py[databricks]`. |

Until then, the interface is public — `StorageBackend` +
`register_backend()` — so any of these can be built outside the package
today. The constraints that bound the S3 backend bind every future one: the
local format does not change, every backend keeps "a reader never sees a
partial record", and every backend passes the same regression journeys.

## Considered and rejected

Decisions worth recording so they are not re-litigated by default (the ADRs
in [docs/adr/](docs/adr/README.md) carry the full reasoning):

- **Replacing the folder format with JSONL.** Appends to a shared file need
  locks where atomic renames need none; a torn append corrupts every reader,
  while a torn record today is invisible; pickled payloads are binary; and
  deleting one record from the middle of a file means compaction. The folder
  stays the source of truth ([ADR 0002](docs/adr/0002-directory-per-record.md)).
  A `quarantine export --jsonl` command may arrive for shipping metadata into
  log pipelines — additive, not a replacement.
- **An embedded metrics server (`metrics_port=`,
  [#14](https://github.com/halcyon-past/quarantine/issues/14)).** The need is
  real; the mechanism is not. Under `multiprocessing` every worker would race
  to bind the same port, batch jobs exit before Prometheus can scrape them,
  and nobody owns the server's lifecycle. The hooks plus
  [docs/observability.md](docs/observability.md) deliver the same metrics with
  the application owning the port ([ADR 0006](docs/adr/0006-hooks-not-servers.md)).
- **A distributed queue mode (SQS-style leases and visibility timeouts).**
  That is a message broker, and building a small one inside an error-handling
  library serves neither well. The shared-store backends above cover the
  fleet use case without inventing coordination.
- **Framework plugins (Airflow operators, Dagster resources).** See recipes
  above — docs deliver the value without the maintenance treadmill.
- **A `[tool.quarantine]` pyproject section.** Ten keyword arguments and one
  environment variable do not justify config-file discovery, precedence rules
  and a TOML parser in a zero-dependency library
  ([ADR 0004](docs/adr/0004-zero-runtime-dependencies.md)).

# 0007 — Commit point for object-store backends

**Status:** Accepted (v1.0.0, S3 backend) · Resolves [issue #11](https://github.com/halcyon-past/quarantine/issues/11)

## Context

Remote backends (S3, GCS, Azure Blob) are the next major feature, so that
fleets of ephemeral workers can share one quarantine. The local format's
correctness rests on two primitives object stores do not have: **atomic
rename** (the commit point, [0001](0001-rename-based-atomicity.md)) and
**atomic directory creation** (id allocation).

## Decision (implemented by the S3 backend)

Keep the record layout — per-record objects under a prefix
(`<prefix>/0001/meta.json`, `input.pkl`, …) — and replace the two primitives:

- **Commit point: `meta.json` is written last.** A record without `meta.json`
  does not exist; readers ignore key prefixes that lack it. Writing the
  payload objects first and the metadata object last reproduces "a reader
  never sees a partial record" without rename.
- **Id allocation: conditional create.** An id is claimed by writing a
  zero-byte ``.claim`` object with ``If-None-Match: *`` (supported by
  S3/GCS/Azure alike). On ``PreconditionFailed``, take the next id — the same
  loser-moves-on behaviour the local ``mkdir`` gives.

Deduplication reads fingerprints from the live records, accepting that remote
dedup is advisory under concurrency rather than strict — two workers may
quarantine the same input in the same instant, and that is a duplicate
record, not a lost one. *Never lose a failure* outranks *never store twice*.

The open questions resolved as follows:

- **No remote `index.json`.** Object-store listings are strongly consistent,
  so the listing is the index and nothing can go stale.
- **Crash debris** — a claim or payload objects without `meta.json` — is
  invisible to every reader, and `quarantine reindex` sweeps it.
- **Reads materialise into a per-URL local cache**, so `Record`, the CLI,
  `retry`/`debug` and the dashboard work unchanged against a bucket.
- **Redis stays out of scope for this record**: it is not an object store and
  will get its own scheme (likely hash-per-record with `SETNX` allocation)
  in its own ADR when built.

## Consequences

- The backend passes the same journeys as the local store, including a
  fleet-level regression test (two worker processes, one bucket, a separate
  "laptop" replaying), plus torn-write and claim-race tests.
- The interface (`StorageBackend`, `register_backend`) is public, so GCS,
  Azure and third-party backends can follow without touching core.
- Extras keep core zero-dependency:
  [0004](0004-zero-runtime-dependencies.md).

# 0007 — Commit point for object-store backends

**Status:** Proposed (target: v0.4.0) · Tracks [issue #11](https://github.com/halcyon-past/quarantine/issues/11)

## Context

Remote backends (S3, GCS, Azure Blob) are the next major feature, so that
fleets of ephemeral workers can share one quarantine. The local format's
correctness rests on two primitives object stores do not have: **atomic
rename** (the commit point, [0001](0001-rename-based-atomicity.md)) and
**atomic directory creation** (id allocation).

## Proposed decision

Keep the record layout — per-record objects under a prefix
(`<prefix>/0001/meta.json`, `input.pkl`, …) — and replace the two primitives:

- **Commit point: `meta.json` is written last.** A record without `meta.json`
  does not exist; readers ignore key prefixes that lack it. Writing the
  payload objects first and the metadata object last reproduces "a reader
  never sees a partial record" without rename.
- **Id allocation: conditional create.** Claim an id by writing a zero-byte
  marker with an if-absent condition (`If-None-Match: *` on S3/GCS/Azure —
  all three support it). On conflict, take the next id — the same
  loser-moves-on behaviour the local `mkdir` gives.

Deduplication reads fingerprints from a listing (or a small manifest object),
accepting that remote dedup is advisory under concurrency rather than
strict — two workers may quarantine the same input in the same instant, and
that is a duplicate record, not a lost one. *Never lose a failure* outranks
*never store twice*.

## Open questions (to resolve before Accepted)

- Whether `index.json` has a remote equivalent or listings are always live.
- Redis is not an object store; it likely gets a hash-per-record scheme with
  `SETNX` id allocation and optional TTL, documented separately.
- Crash between payload write and `meta.json`: orphaned payload objects need
  a `reindex`-style sweep (lifecycle rule or explicit command).

## Consequences (anticipated)

- Every backend must pass the same end-to-end regression journeys as the
  local store, plus a backend-specific torn-write test.
- Extras keep core zero-dependency:
  [0004](0004-zero-runtime-dependencies.md).

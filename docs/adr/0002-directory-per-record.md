# 0002 — A directory per record, not a JSONL file

**Status:** Accepted (v0.1.0)

## Context

The obvious modern format for "a stream of failure events" is a single
append-only JSONL file with schema versioning. It was proposed for this
library and deliberately rejected.

## Decision

Every record is a self-describing directory (`0001/`, `0002/`, …) holding
`input.pkl`/`input.json`, `input.txt`, `traceback.txt` and `meta.json`.
`index.json` is only a cache and can always be rebuilt from the directories.
Schema versioning lives *inside each record* as `meta_version`, with forward
tolerance: unknown keys are ignored, missing optional keys get defaults.

## Why not JSONL

- **Concurrency.** Appends to a shared file need locking to avoid interleaved
  writes; atomic directory renames need none (see
  [0001](0001-rename-based-atomicity.md)).
- **Crash isolation.** A torn append corrupts the file for every reader. A
  torn record today is an invisible `.tmp-*` directory; the damage radius of
  any corruption is one record, never the store.
- **Binary payloads.** The primary serialization is pickle
  ([0003](0003-pickle-first-serialization.md)). JSONL would force base64 —
  ~33% bigger and no longer greppable.
- **Deletion.** `retry` and `clear 3` remove single records — a directory
  delete. Removing a line from the middle of a file means tombstones plus
  compaction.
- **Human debugging.** `cat .quarantine/0001/traceback.txt` is the product.

## Consequences

- Listing costs a directory scan when the index is cold; `index.json`
  amortises it.
- Many small files instead of one big one — the right trade for a store whose
  normal population is dozens, not millions.
- A `quarantine export --jsonl` command remains open as an *additive* format
  for shipping metadata into log pipelines.

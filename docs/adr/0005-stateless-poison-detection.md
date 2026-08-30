# 0005 — Poison items derived from `attempts`, not a stored flag

**Status:** Accepted (v0.3.0)

## Context

A record that fails retry after retry eventually makes every blanket
`quarantine retry` slower and noisier. Dead-letter systems usually solve this
by *moving* the item to a second queue or *marking* it with a persistent
`dead` flag. Both were considered.

## Decision

There is no `dead` flag and no second folder. `meta.json` already persists
`attempts` — incremented on every failed retry since v0.1.0 — so "dead" is
*derived*: a record is dead when `attempts >= dead_after`, a threshold the
caller chooses (`dead_after=` in Python, `--dead-after` on the CLI, default
off).

A blanket retry skips dead records and reports them in `unretryable` with the
reason. Retrying a record **by explicit id always runs it**: naming the record
is the deliberate human decision the blanket retry refuses to make.

## Why derived, not stored

- **No new write path.** A stored flag needs setting, clearing, and migrating;
  a derived one cannot drift out of sync with the count it summarises.
- **The threshold is a policy, not a fact about the record.** Two teams can
  run the same folder with different `dead_after` values; a stored flag would
  bake one team's policy into shared data.
- **Reversible by construction.** Lowering the threshold or naming the id
  "un-deads" a record with no state change; nothing was moved, nothing must
  be moved back.

## Consequences

- Dead records still occupy folder capacity until cleared — deliberate,
  because deleting data is `clear`'s job and nobody else's.
- The check is skipped entirely when ids are given explicitly, which is the
  documented escape hatch.
- Records written by older versions work unchanged: `attempts` has been in
  `meta.json` since the first release.

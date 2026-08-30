# 0003 — Pickle-first serialization with a lossy fallback chain

**Status:** Accepted (v0.1.0)

## Context

The saved input must be good enough to *re-run the call* (`quarantine retry`),
not merely to look at. Inputs are arbitrary Python objects: dataclasses,
ORM rows, numpy arrays, things with reference cycles.

## Decision

Serialize with the highest-fidelity format that works, falling back in order:

1. **pickle** — round-trips exactly, handles cycles. The normal case.
2. **JSON** — when pickling fails (a lock, a socket, a lambda in the payload).
   Objects JSON cannot express become their `repr`, and the record is marked
   `payload_lossy: true` with the reason.
3. **repr only** — when both fail. `input.txt` is all there is, and
   `load_call()` refuses to reconstruct rather than lying.

`input.txt` (a human-readable rendering) is *always* written, whatever format
won. A broken `__repr__` cannot break the record either — `safe_repr` never
raises.

## Why pickle first, despite its reputation

Pickle's security problem is *loading untrusted data*. A quarantine folder
holds data your own process wrote on your own machine — the same trust
boundary as your virtualenv. The alternative (JSON-first) silently degrades
every dataclass and every tuple on the happy path, which breaks `retry` for
the majority of real inputs to protect against a threat model that does not
apply. The trade-off is documented (`S301` is suppressed deliberately in the
lint config) and the on-disk format page tells users not to load folders they
do not trust.

## Consequences

- `retry` replays the *exact* call for the common case.
- Records are marked honestly when fidelity was lost (`payload_lossy`,
  `payload_reason`), so a reader knows what they have.
- Pickled inputs need a compatible interpreter and importable classes to
  reload; the error message says so when that bites.
- Property-based tests (Hypothesis) assert the chain never raises and never
  loses a record, whatever the input's shape.

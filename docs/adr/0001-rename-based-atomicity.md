# 0001 — Atomicity by temp-dir + rename, not locks

**Status:** Accepted (v0.1.0)

## Context

A record must never be half-written. The library's one promise is *never lose
a failure*, and a reader (the CLI, the dashboard, another process) can arrive
at any moment — including the moment a `kill -9` lands mid-write. Multiple
processes write to one folder concurrently, on three operating systems, with
no daemon and no server to coordinate them.

## Decision

A record is assembled in a hidden `.tmp-<random>/` directory and **renamed**
into place. The rename is the commit point. Sequential ids are allocated by
*creating* the numbered directory, which is atomic on every supported
platform, so two processes cannot claim the same id — the loser takes the next
one.

No lock files guard record writes. The only lock in the system protects
`index.json`, which is a rebuildable cache, not the source of truth.

## Alternatives considered

- **A lock file around the folder.** Locks need an owner, a timeout, and a
  story for the process that dies while holding one. Windows makes each of
  those harder. A design where torn states are unrepresentable beats a design
  that detects and repairs them.
- **SQLite.** Solves atomicity, loses `cat`/`grep`/`rm -rf` — the folder being
  plain files people can read by hand is a feature users rely on.

## Consequences

- A crash mid-write leaves an ignorable `.tmp-*` directory, never corruption.
  `quarantine reindex` sweeps them.
- Windows sync clients and virus scanners can hold a brand-new directory open,
  making the commit rename fail transiently — so the rename retries with
  backoff (v0.1.3 hardened this).
- Object stores have no rename, which is why remote backends need their own
  commit-point decision: see [0007](0007-object-store-commit-point.md).

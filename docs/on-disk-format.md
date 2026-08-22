# The on-disk format

[← docs index](index.md)

`.quarantine/` is plain files. No database, no lock service, no daemon — you can
read it with `cat`, search it with `grep`, and delete it with `rm -rf`.

```
.quarantine/
├── 0001/
│   ├── input.pkl        # the exact call (pickle; or input.json, or neither)
│   ├── input.txt        # human-readable repr, so you can just *look* at it
│   ├── traceback.txt    # the full traceback, exactly as it would have printed
│   └── meta.json        # function, timestamps, attempt count, versions
├── 0002/
│   └── ...
└── index.json           # a cache; rebuildable from the folders above
```

## Guarantees

**Atomic writes.** A record is assembled in a hidden `.tmp-<random>/` directory
and then *renamed* into place. The rename is the commit point, so a reader never
sees a partial record, and `kill -9` mid-write leaves an ignorable temp directory
rather than corruption. `quarantine reindex` sweeps those up.

**Self-describing records.** Each folder stands alone. `index.json` is only a
cache: if it is missing, stale or corrupt it is rebuilt from the record folders,
so losing it costs a directory scan, not data.

**Sequential ids.** Ids are allocated by *creating* the folder, which is atomic
on every supported platform. Two processes writing at once cannot collide; the
loser simply takes the next id.

**Forward tolerance.** `meta.json` carries `meta_version`. Unknown keys are
ignored and missing optional keys get defaults, so a folder written by another
version stays readable. A record that genuinely cannot be parsed is *reported*
and skipped — it never takes the whole listing down.

## meta.json

```json
{
  "id": 1,
  "function": "process",
  "module": "__main__",
  "fingerprint": "8f14e45fceea167a5a36dedd4bea2543",
  "source_file": "/home/ann/job.py",
  "error_type": "ValueError",
  "error": "could not convert string to float: 'N/A'",
  "created_at": "2026-08-22T15:22:35+00:00",
  "last_failed_at": "2026-08-22T15:22:35+00:00",
  "attempts": 1,
  "payload_format": "pickle",
  "payload_lossy": false,
  "payload_reason": null,
  "redacted": [],
  "preview": "{'id': 8813, 'price': 'N/A', 'qty': 1}",
  "python": "3.13.3",
  "platform": "Linux 6.8.0",
  "quarantine_version": "0.1.0",
  "pid": 48211,
  "meta_version": 1
}
```

| Field | Notes |
|---|---|
| `function`, `module`, `source_file` | How `quarantine retry` finds the function again. `source_file` is what makes `retry --import` possible for scripts. |
| `fingerprint` | Hash of the **redacted** input plus the function name; drives deduplication. Order-insensitive for dicts. |
| `attempts` | Starts at 1, and increases each time a retry fails again. |
| `payload_lossy`, `payload_reason` | Set when fidelity was lost, and why (for example, pickle refusing a lock object). |
| `redacted` | Field names replaced with `***REDACTED***`, so a reader knows the input is deliberately incomplete. |
| `preview` | One-line preview, also used by `quarantine list`. |

## The input files

The serializer tries formats in descending fidelity, so *something* readable is
always written:

| `payload_format` | File | When |
|---|---|---|
| `pickle` | `input.pkl` | The normal case. Round-trips exactly, handles reference cycles. |
| `json` | `input.json` | The input could not be pickled (a lock, a socket, a lambda). Objects JSON cannot express become their `repr`, and `payload_lossy` is `true`. |
| `repr` | *(neither)* | Both failed — for example an unpicklable object inside a reference cycle. `input.txt` is all there is, and `load_call()` refuses rather than lying. |

`input.txt` is always written:

```
# call: process({'id': 8813, 'price': 'N/A', 'qty': 1}, attempt=3)

args[0] = {'id': 8813, 'price': 'N/A', 'qty': 1}
kwargs['attempt'] = 3
```

A broken `__repr__` cannot break the record either — it is rendered as
`<unreprable Row: RuntimeError: nope>`.

## index.json

```json
{
  "schema": 1,
  "updated": "2026-08-22T15:22:35+00:00",
  "count": 2,
  "records": [{ "id": 1, "fingerprint": "...", "function": "process" }]
}
```

A cache of the fields needed to list records and to answer "have I seen this
input before" without opening every folder. It is written under a
`.index.lock` file, which is broken automatically if a dead process left it
behind. Delete `index.json` whenever you like; the next read, or
`quarantine reindex`, regenerates it.

## Reading it yourself

```python
from quarantine import Store

for record in Store(".quarantine").records():
    print(record.id, record.summary)
    call = record.load_call()  # the original args/kwargs
    print(call.item)
```

Or without the library at all:

```bash
jq -r .error .quarantine/*/meta.json | sort | uniq -c | sort -rn
```

## Housekeeping

- **Do not commit it.** Records contain your real inputs. Add `.quarantine/` to
  `.gitignore`.
- **It is safe to copy** to another machine — records are self-contained. Pickled
  inputs need a compatible interpreter to reload, and `retry` needs the function
  to be importable there.
- **It is safe to delete**, wholesale or per record: `quarantine clear`,
  `quarantine clear 3`, or `rm -rf .quarantine`.

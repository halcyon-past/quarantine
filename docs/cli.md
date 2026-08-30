# Command-line reference

[← docs index](index.md)

Installing the package puts a `quarantine` command on your `PATH`.
`python -m quarantine` is identical in every respect, and is the reliable form
if the script directory is not on your `PATH`.

```
usage: quarantine [-h] [--version] COMMAND ...
```

Every subcommand accepts:

| Flag | Meaning |
|---|---|
| `-d PATH`, `--dir PATH` | Which quarantine folder to work on. Defaults to `$QUARANTINE_DIR`, else `./.quarantine`. |
| `-h`, `--help` | Help for that subcommand. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything you asked for succeeded. |
| `1` | The command ran, but something is still wrong — a retry failed again, a record could not be replayed, or an id did not exist. |
| `2` | Bad usage, or the folder could not be read. |
| `130` | Interrupted with Ctrl-C. |

## `quarantine list`

Alias: `ls`.

| Flag | Meaning |
|---|---|
| `-f NAME`, `--function NAME` | Only records from this function. |
| `-n N`, `--limit N` | Show at most N records. |
| `--json` | Full metadata as JSON, instead of the table. |

```
$ quarantine list
#  when      function  error                           input preview
1  20:52:35  process   ValueError: could not convert…  {'id': 8813, 'price': 'N/A', …
2  20:52:35  process   KeyError: 'price'               {'id': 9107, 'qty': 4}

2 in .quarantine - run `quarantine retry` after fixing your code.
```

Times are shown in **local** time; the stored timestamps are UTC. The table
truncates to your terminal width — use `--json` when you want everything:

```bash
quarantine list --json | jq -r '.[] | "\(.id) \(.error_type) \(.error)"'
```

An empty folder is not an error:

```
$ quarantine list
Nothing quarantined - .quarantine does not exist yet.
```

## `quarantine show ID [ID...]`

Everything about one or more records: metadata, the readable input, and the
full traceback exactly as it would have printed.

| Flag | Meaning |
|---|---|
| `--json` | The records' metadata as JSON. |

```
$ quarantine show 1
── record 0001 ────────────────────────────────
function   __main__.process
error      ValueError: could not convert string to float: 'N/A'
first seen 2026-08-22T15:22:35+00:00
last seen  2026-08-22T15:22:35+00:00   attempts: 1
stored as  pickle
folder     .quarantine/0001

--- input ---
# call: process({'id': 8813, 'price': 'N/A', 'qty': 1})

args[0] = {'id': 8813, 'price': 'N/A', 'qty': 1}

--- traceback ---
Traceback (most recent call last):
  ...
ValueError: could not convert string to float: 'N/A'
```

Exits `1` if any requested id does not exist (the others are still shown).

## `quarantine retry [ID...]`

Re-runs records and **deletes the ones that now succeed**. With no ids, it
retries everything.

| Flag | Meaning |
|---|---|
| `-f NAME`, `--function NAME` | Only records from this function. |
| `-i FILE.py`, `--import FILE.py` | Import this file to find the functions. Needed when they live in a script that ran as `__main__`. **The file's top level is executed**, so keep the script's own work behind `if __name__ == "__main__":`. |
| `--dry-run` | Report what would be retried; change nothing. |
| `--dead-after N` | Treat records that already failed `N` attempts as *dead* and skip them, so one poison item cannot dominate every blanket retry. A record retried by explicit ID is always run. |
| `--json` | Machine-readable result. |

```
$ quarantine retry
✓ 3 recovered · ✗ 1 still failing (kept in quarantine)
```

A record that fails again keeps its place, with `attempts` incremented and a
fresh traceback. Records whose function cannot be imported are left completely
untouched and reported:

```
$ quarantine retry
✓ 0 recovered
  ! 0001 skipped: process was defined in /home/ann/job.py, which ran as a
    script, so a separate process cannot import it as '__main__'.
      Retry it with: quarantine retry --import /home/ann/job.py
```

Exit code is `1` if anything is still failing or unretryable — which makes it
usable as a gate:

```bash
quarantine retry --json > retry.json || notify-team < retry.json
```

## `quarantine debug ID`

Re-runs one record and drops you into `pdb` **on the frame that raised**, with
the exact input that caused it. You never have to reproduce the bug.

| Flag | Meaning |
|---|---|
| `-p`, `--print` | Print the input and traceback instead of starting a debugger. |
| `--no-post-mortem` | Do not re-run. Start `pdb` with `item`, `args`, `kwargs` and `record` in scope. |
| `-i FILE.py`, `--import FILE.py` | As for `retry`. |

```
$ quarantine debug 2
record 0002  __main__.process
error   KeyError: 'price'
re-running the failing call; you will land in the frame that raised.
> /home/ann/job.py(5)process()
-> return float(row["price"]) * row["qty"]
(Pdb) row
{'id': 9107, 'qty': 4}
```

If the function no longer fails, it says so rather than pretending:

```
it succeeded this time - nothing to debug. `quarantine retry` will clear it.
```

## `quarantine clear [ID...]`

Alias: `rm`.

| Flag | Meaning |
|---|---|
| `-y`, `--yes` | Do not ask for confirmation. |

With ids, deletes exactly those. With no ids, it clears the whole folder and
asks first. When stdin is not a terminal (CI, a pipeline) it refuses to delete
everything without `--yes`, rather than guessing:

```
$ quarantine clear < /dev/null
quarantine: refusing to delete without --yes when input is not a terminal
Left alone.
```

## `quarantine stats`

| Flag | Meaning |
|---|---|
| `--json` | Machine-readable summary. |

```
$ quarantine stats
2 record(s) in .quarantine  (3.0 KB on disk)
oldest 2026-08-22T15:22:35+00:00   newest 2026-08-22T15:22:35+00:00

by function:
       2  process
by error:
       1  ValueError
       1  KeyError
```

## `quarantine reindex`

Rebuilds `index.json` from the record folders, and sweeps up `.tmp-*` staging
entries left behind by a hard crash. You rarely need it — the index rebuilds
itself on demand — but it is the right tool after `kill -9`, after copying a
folder around by hand, or after several processes wrote to one folder.

```
$ quarantine reindex
indexed 2 record(s) into .quarantine/index.json
cleaned up 1 leftover temp entry
```

Exits `1` if any record folder is unreadable, and names it.

## Output and encoding

Human output goes to **stdout**; warnings and the end-of-run summary go to
**stderr**. On a console that cannot encode `✓` (a plain Windows `cmd`, for
instance) every glyph falls back to ASCII — `OK`, `FAIL`, `->` — instead of
printing mojibake or raising.

## `quarantine ui`

Starts a local, read-only web dashboard to view quarantined records in your browser.
This provides a cleaner interface for inspecting tracebacks and payloads than `quarantine show`.

```bash
quarantine ui
quarantine ui --port 9090
```

### Options

* `--port PORT`: The port to bind the local HTTP server to (defaults to `8080`).

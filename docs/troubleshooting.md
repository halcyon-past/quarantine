# Troubleshooting

[← docs index](index.md)

## "My item was skipped instead of processed"

```
✓ 900 processed · ⏭ 4 skipped (already quarantined) → .quarantine/
```

That input is already in the folder from an earlier run, so it was not re-run.
This is deliberate: it keeps reruns from duplicating records. Either fix the
cause and `quarantine retry`, or start clean:

```bash
quarantine clear --yes
```

Or turn the behaviour off with `@quarantine(skip_known_bad=False)`.

## "quarantine retry says it cannot import `__main__`"

```
! 0001 skipped: process was defined in /home/ann/job.py, which ran as a script,
  so a separate process cannot import it as '__main__'.
    Retry it with: quarantine retry --import /home/ann/job.py
```

`__main__` means "whatever is running right now" — in a new process that is the
`quarantine` command, not your script. Two ways out:

```bash
quarantine retry --import job.py     # imports the file to find the function
```

The file's top level **is executed**, so keep the script's own work behind a
guard (which is good practice anyway):

```python
if __name__ == "__main__":
    main()
```

Or retry in-process, where `__main__` really is your script:

```python
from quarantine import retry

retry()  # at the end of job.py
retry(using=process)  # or point it straight at the callable
```

## "quarantine retry says the function is defined inside another function"

Closures cannot be imported by name. Retry from Python and say which callable to
use:

```python
retry(using=my_function)
```

## "It stopped with SystemicFailure"

The circuit breaker did its job: that many failures *in a row* is a systemic
problem — a dead database, an expired token — not bad data. Fix the cause and
rerun; the already-quarantined items will be skipped. If long failure streaks
are genuinely normal for your data, raise or disable the limit:

```python
@quarantine(halt_after=500)
@quarantine(halt_after=None)
```

## "It stopped with QuarantineFull"

The folder hit `max_items` (10,000 by default). Nothing was dropped — that is
precisely why it raised. Clear it, retry it, or raise the cap:

```bash
quarantine retry     # then rerun
quarantine clear --yes
```

```python
@quarantine(max_items=100_000)
```

## "quarantine debug says there is no replayable input"

```
quarantine: record 0003 has no replayable input (stored as 'repr'); see input.txt
```

The input could be neither pickled nor JSON-encoded — typically an unpicklable
object (a lock, a database connection, an open file) inside a reference cycle.
`input.txt` still shows what it was and the traceback is intact. Reconstruct the
input by hand, or pass simpler values into the decorated function (a row id
rather than a live cursor).

## "The decorator refuses to wrap my generator"

```
TypeError: cannot wrap stream: it is a generator function, so its body runs
during iteration, not during the call - the decorator would catch nothing.
```

Exactly what it says: wrapping a generator would silently protect nothing.
Protect the consumer instead:

```python
from quarantine import shield

for row in shield(stream(), using=process):
    ...
```

## "I see escaped characters or question marks instead of ✓"

`quarantine` checks whether the stream can encode its glyphs and falls back to
`OK` / `FAIL` / `->` when it cannot, so this should not happen. If it does,
something else in your pipeline is re-encoding stderr; force UTF-8 with
`PYTHONIOENCODING=utf-8` or `python -X utf8`.

## "No summary line appeared"

The summary prints at process exit, only when something was quarantined, skipped
or recovered, and only to **stderr**. It is suppressed by `report=False`, and
`os._exit()` or a hard signal skips `atexit` handlers entirely. Print it
yourself whenever you like:

```python
from quarantine import summary

print(summary())
```

## "Two summary lines appeared"

Two `Quarantine` instances with *different* settings each report. Give them
identical options (they are then interned into one instance), or pass
`report=False` to one of them.

## "The folder is in the wrong place"

The default is relative to the process's working directory, so a job started
elsewhere writes elsewhere. Pin it:

```python
@quarantine(dir="/var/lib/myjob/quarantine")
```

```bash
export QUARANTINE_DIR=/var/lib/myjob/quarantine
```

## "quarantine list is empty but I know it failed"

- Check the folder you think it is: `quarantine stats --dir path/to/.quarantine`.
- The exception may not have matched `only=`, in which case it propagated
  normally — look for the traceback in your logs.
- `KeyboardInterrupt`, `SystemExit`, `GeneratorExit` and quarantine's own errors
  are never quarantined, by design.

## "A record folder is corrupt"

Individual bad records are reported and skipped, never fatal:

```
quarantine: skipping unreadable record: .quarantine/0007/meta.json is not valid JSON
```

Rebuild the index and clean up with `quarantine reindex`, then delete the bad
folder by hand or with `quarantine clear 7`.

## "StorageError: timed out waiting for .index.lock"

Another process is writing to the same folder and did not finish within ten
seconds, or a stale lock survived a crash (those are broken automatically after
sixty seconds). If nothing else is running, delete `.quarantine/.index.lock`.

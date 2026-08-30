# quarantine — documentation

[← back to the README](https://github.com/halcyon-past/quarantine#readme) ·
[package on PyPI](https://pypi.org/project/quarantine-py/) ·
[latest releases](https://github.com/halcyon-past/quarantine/releases)

`quarantine` keeps a loop running when one item goes bad: the failure is written
to a folder — input, traceback, metadata — and you deal with it later.

| Page | Read it when |
|---|---|
| [Installation](installation.md) | Installing, verifying, choosing where the folder lives, upgrading. |
| [Usage guide](usage.md) | You want the full picture: options, async, threads, retrying, alerting, recipes. |
| [Practical examples](examples.md) | You want to see real-world scenarios and what gets created behind the scenes. |
| [CLI reference](cli.md) | You are at a terminal with a folder full of failures. |
| [API reference](api.md) | You are writing code against it. |
| [On-disk format](on-disk-format.md) | You want to read, ship or process `.quarantine/` yourself. |
| [Observability](observability.md) | You want failures on a dashboard: Prometheus, Datadog, Sentry, logging. |
| [Troubleshooting](troubleshooting.md) | Something surprised you. |
| [FAQ](faq.md) | The "is this just a dead-letter queue?" questions. |
| [Decision records](adr/README.md) | Why the design is the way it is — atomicity, formats, dependencies. |

## The shortest possible tour

```bash
pip install quarantine-py
```

```python
from quarantine import quarantine


@quarantine
def process(row):
    save(row["id"], float(row["price"]))


for row in rows:
    process(row)
```

```
✓ 9,996 processed · ✗ 4 quarantined → .quarantine/  (run `quarantine retry` after fixing)
```

```bash
quarantine list         # what broke, and on what input
quarantine debug 2      # a debugger, on the frame that raised
# ... fix your code ...
quarantine retry        # re-run only the failures
```

## The five ideas worth knowing

1. **A failure is a file, not a log line.** Every record is a folder holding the
   exact input, the full traceback and metadata — enough to reproduce the bug
   without reproducing the run. See the [on-disk format](on-disk-format.md).
2. **Nothing is lost, ever.** Writes are atomic, the disk cap raises instead of
   dropping, and quarantine's own errors are never swallowed.
3. **Reruns are cheap.** Known-bad inputs are skipped, so "just run it again"
   neither duplicates records nor spams your logs.
4. **A streak of failures is a different problem.** Fifty in a row is not bad
   data, it is a dead database — so the run halts instead of quarantining
   everything. See [the circuit breaker](usage.md#the-circuit-breaker).
5. **Secrets are scrubbed before the write, not after.** See
   [redacting secrets](usage.md#redacting-secrets).

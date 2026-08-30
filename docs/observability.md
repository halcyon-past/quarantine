# Observability

[← docs index](index.md)

When a pipeline that normally quarantines 5 items a day suddenly quarantines
5,000, someone should be paged. The hooks — `on_quarantine`,
`on_retry_success`, `on_retry_failure` — are the integration point: they
receive the `Record` after it is safely on disk, and an exception inside a
hook is reported on stderr, never raised, so a broken metrics pipeline cannot
lose your work.

quarantine deliberately does **not** run a metrics server for you. Your
application owns its ports and process lifecycle; the library hands you the
events. (Why: under `multiprocessing` every worker would race to bind the same
port, and batch jobs exit before a scraper ever arrives.)

## Prometheus — long-running services

For a process that lives long enough to be scraped, expose the standard
counter yourself:

```python
from prometheus_client import Counter, start_http_server

from quarantine import quarantine

QUARANTINED = Counter(
    "quarantine_items_total",
    "Items quarantined",
    ["function", "error_type"],
)
RECOVERED = Counter("quarantine_recovered_total", "Items recovered by retry", ["function"])

start_http_server(9000)  # your app's decision, made once, in one place


@quarantine(
    on_quarantine=lambda r: QUARANTINED.labels(r.function, r.error_type).inc(),
    on_retry_success=lambda r: RECOVERED.labels(r.function).inc(),
)
def process(item): ...
```

Prometheus scrapes `http://…:9000/metrics` and alerting rules do the rest.

## Prometheus — batch jobs (Pushgateway)

A nightly job is gone before the first scrape. Push the totals at the end of
the run instead:

```python
from prometheus_client import CollectorRegistry, Counter, push_to_gateway

from quarantine import quarantine

registry = CollectorRegistry()
QUARANTINED = Counter(
    "quarantine_items_total", "Items quarantined", ["function", "error_type"], registry=registry
)


@quarantine(on_quarantine=lambda r: QUARANTINED.labels(r.function, r.error_type).inc())
def process(item): ...


def main() -> None:
    for item in load_items():
        process(item)
    push_to_gateway("pushgateway.internal:9091", job="nightly-etl", registry=registry)
```

## Datadog

`statsd` is fire-and-forget UDP, so it suits both daemons and batch jobs:

```python
from datadog import statsd

from quarantine import quarantine


@quarantine(
    on_quarantine=lambda r: statsd.increment(
        "quarantine.items", tags=[f"function:{r.function}", f"error:{r.error_type}"]
    ),
    on_retry_failure=lambda r: statsd.increment(
        "quarantine.retry_failed", tags=[f"function:{r.function}"]
    ),
)
def process(item): ...
```

## Sentry

Quarantined failures never raise, so Sentry's exception hook never sees them.
Report them explicitly, with the record's own context:

```python
import sentry_sdk

from quarantine import quarantine


def report(record):
    sentry_sdk.capture_message(
        f"quarantined #{record.id:04d} {record.function}: {record.summary}",
        level="warning",
    )


@quarantine(on_quarantine=report)
def process(item): ...
```

## Plain logging

No infrastructure at all — just your logger, with the traceback already
captured on disk:

```python
import logging

from quarantine import quarantine

log = logging.getLogger("pipeline")


@quarantine(on_quarantine=lambda r: log.warning("quarantined %s: %s", r.function, r.summary))
def process(item): ...
```

## Watching the folder itself

Everything above is event-driven. For gauge-style monitoring — "how many
records are sitting in quarantine right now?" — the folder is the API:

```bash
quarantine stats --json | jq .records
```

`stats --json` also breaks the count down `by_function` and `by_error`, which
makes a fine input for a cron-driven exporter or a healthcheck endpoint.

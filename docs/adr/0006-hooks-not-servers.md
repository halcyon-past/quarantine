# 0006 — Observability through hooks, never an embedded server

**Status:** Accepted (v0.3.0) · Resolves [issue #14](https://github.com/halcyon-past/quarantine/issues/14)

## Context

In Kubernetes or under cron, terminal summaries reach nobody. Users asked for
built-in Prometheus support: `@quarantine(metrics_port=9000)` spinning up
`prometheus_client.start_http_server` inside the decorator.

## Decision

quarantine exposes lifecycle **hooks** — `on_quarantine`, `on_retry_success`,
`on_retry_failure`, each receiving the `Record` after it is safely on disk —
and will never open a network port itself. The observability docs ship
copy-pasteable wiring for Prometheus (both long-running and Pushgateway),
Datadog, Sentry and plain logging.

## Why the embedded server was rejected

- **It breaks under the library's own headline feature.** The decorator runs
  in every `multiprocessing` worker; each would race to bind the same port.
- **Batch jobs and scraping do not mix.** quarantine's core audience is
  scripts that exit before the first scrape arrives. The correct Prometheus
  pattern for batch is Pushgateway — a hook call at end of run, not a server.
- **Nobody owns the lifecycle.** A port opened as a decorator side effect has
  no shutdown story and an unaccounted security surface.
- **Dependencies.** `prometheus_client` in core violates
  [0004](0004-zero-runtime-dependencies.md); in the user's application it is
  three lines against the hook.

## Consequences

- Hooks are reported-not-raised: a broken metrics pipeline cannot lose a
  record or break a retry (the same rule `on_quarantine` has had since
  v0.1.0).
- Gauge-style questions ("how many records right now?") are served by
  `quarantine stats --json`, which a sidecar or healthcheck can poll.
- If hook-based wiring proves insufficient for a concrete case, the recorded
  fallback idea is a *separate* exporter process reading the folder — still
  never a port inside the library.

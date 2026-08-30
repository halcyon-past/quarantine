# 0004 — Zero runtime dependencies

**Status:** Accepted (v0.1.0)

## Context

quarantine is installed *into other people's environments*, usually messy
ones — a data scientist's conda env, a five-year-old ETL virtualenv, a
container built from a requirements file nobody remembers writing. Every
dependency is a chance to conflict with something already pinned there.

## Decision

The core package depends on the standard library only. `pip install
quarantine-py` must never trigger a resolver fight or upgrade someone's
`requests`.

Everything that would need a third-party library is either:

- built on stdlib instead — the CLI is `argparse`, the dashboard is
  `http.server`, serialization is `pickle`/`json`; or
- pushed to the integration point — metrics and alerting go through hooks
  (see [0006](0006-hooks-not-servers.md)), so `prometheus_client` lives in the
  *user's* application; or
- an optional extra — planned storage backends install as
  `quarantine-py[s3]`, `[gcs]`, `[azure]`, `[redis]`, `[databricks]`, each
  pulling in only its own client.

## Consequences

- Some conveniences stay out of core forever: no rich terminal tables, no
  async HTTP, no YAML config.
- The dashboard is deliberately plain HTML over `http.server` — fine for a
  local debugging tool, and exactly as far as stdlib comfortably goes.
- A `[tool.quarantine]` pyproject config section was rejected partly on this
  ground: ten keyword arguments and one environment variable do not justify
  config-file discovery and a TOML parser.
- Optional extras must degrade cleanly: importing quarantine without the
  extra installed works; *using* the backend without it raises with the exact
  `pip install` line to run.

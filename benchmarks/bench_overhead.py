"""Measure what ``@quarantine`` costs when nothing fails.

Measures the success path - the price paid on every call that does *not* go
wrong - against the do-nothing baselines users would write by hand. The
failure path is deliberately not benchmarked here: a failure costs file
writes by design, and that price buys a record that survives the crash.

Run it with::

    pip install "quarantine-py[bench]"
    python benchmarks/bench_overhead.py

Results and methodology are published in ``docs/benchmarks.md``. pyperf
spawns fresh worker processes and calibrates loops itself; pass ``--rigorous``
for more samples or ``--fast`` for a quick sanity check.
"""

from __future__ import annotations

import tempfile
from typing import Any

import pyperf  # type: ignore[import-untyped]

from quarantine import quarantine

ROW: dict[str, Any] = {"id": 8813, "price": 19.99, "qty": 3, "name": "widget"}
"""A realistically small item: what a CSV row or API record looks like."""


def process(row: dict[str, Any]) -> float:
    """The work being protected: trivial on purpose, so overhead dominates."""
    return float(row["price"] * row["qty"])


def process_try_except(row: dict[str, Any]) -> float:
    """What people write by hand instead: a bare try/except."""
    try:
        return process(row)
    except Exception:  # noqa: BLE001 - the catch-all is the point of the baseline
        return 0.0


def main() -> None:
    """Register the four call styles with pyperf."""
    runner = pyperf.Runner()

    # Each pyperf worker process gets its own folder; nothing is ever written
    # to it, because every call here succeeds.
    folder = tempfile.mkdtemp(prefix="quarantine-bench-")

    guarded = quarantine(dir=folder, report=False)(process)
    guarded_no_dedup = quarantine(dir=folder, report=False, skip_known_bad=False)(process)

    runner.bench_func("bare call", process, ROW)
    runner.bench_func("try/except", process_try_except, ROW)
    runner.bench_func("@quarantine (skip_known_bad=False)", guarded_no_dedup, ROW)
    runner.bench_func("@quarantine (defaults)", guarded, ROW)


if __name__ == "__main__":
    main()

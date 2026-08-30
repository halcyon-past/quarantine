# Benchmarks

[← docs index](index.md)

One question, answered reproducibly: **what does `@quarantine` cost on a call
that does not fail?** The failure path is deliberately not benchmarked — a
failure costs a few file writes and an `fsync` *by design*; that price buys a
record that survives the crash and is not worth optimising away.

## The numbers

Measured with the script below on an Intel Core Ultra laptop, Windows 11,
Python 3.13.3 — a working machine with a sync client and corporate antivirus
running, i.e. deliberately *not* a quiet lab box. Treat the numbers as
ceilings, and the ratios as the durable finding:

| Call style | Mean per call | vs bare |
|---|---|---|
| bare call | 0.10 µs ± 0.02 | 1× |
| hand-written `try/except` | 0.12 µs ± 0.02 | ~1.2× |
| `@quarantine(skip_known_bad=False)` | 1.1 µs ± 0.1 | ~11× |
| `@quarantine` (defaults) | 10.6 µs ± 3.6 | ~100× |

What the two quarantine rows are buying:

- **`skip_known_bad=False` (~1 µs/call)** is the wrapper itself: one function
  indirection, the success-path counter update under a lock. Roughly **one
  second of total overhead per million items**.
- **The defaults (~11 µs/call)** add deduplication: every input is
  fingerprinted (pickled and hashed) so a rerun can skip items already in
  quarantine. Roughly **eleven seconds per million items**, and the cost
  scales with the *size of your items*, since fingerprinting serializes them.

## How to read that

The work you are protecting dwarfs both numbers in almost every real
pipeline: a single `float(row["price"])` plus a database insert costs orders
of magnitude more than 11 µs, and any call that touches the network makes the
overhead invisible. The decorator is not free, but it is cheaper than the
`print` statements it replaces.

The one case that deserves a decision is a **very hot loop over large
objects** — millions of items, each big enough that pickling it for the
fingerprint shows up. There, `skip_known_bad=False` drops the overhead back
to ~1 µs/call, at the price of reruns re-attempting (and re-recording
attempts against) items already quarantined. The
[FAQ entry on cost](faq.md#what-does-it-cost) covers the same trade-off in
prose.

## Methodology

The benchmark lives in
[`benchmarks/bench_overhead.py`](https://github.com/halcyon-past/quarantine/blob/main/benchmarks/bench_overhead.py)
and uses [pyperf](https://pyperf.readthedocs.io/), which spawns fresh worker
processes, calibrates loop counts, discards warmup runs, and reports the
spread rather than a single flattering number.

- The protected function is trivial on purpose (`price * qty` on a small
  dict), so the *overhead* dominates the measurement instead of the work.
- The input is a realistic small record — four keys, scalar values — because
  the default configuration's fingerprint cost depends on input size.
- Every call succeeds, so nothing is ever written to disk; the quarantine
  folder exists but stays empty.
- The baselines are what people actually write by hand: a bare call, and the
  same call inside `try/except`.

Reproduce it with:

```bash
pip install "quarantine-py[bench]"
python benchmarks/bench_overhead.py            # or: make bench
python benchmarks/bench_overhead.py --rigorous # more samples, slower
```

pyperf will warn when your machine is noisy (this page's numbers carried
those warnings too — see the standard deviations). For steadier results run
`python -m pyperf system tune` on Linux, close the sync clients, and prefer
`--rigorous`. If your numbers differ wildly from the table above, trust
yours: they were measured on your hardware, on your Python, against your
antivirus.

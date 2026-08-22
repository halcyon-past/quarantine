"""`shield()` - the loop form, for when you do not want a decorator."""

from __future__ import annotations

import pytest

from quarantine import ashield, shield


def parse(row):
    return float(row["price"])


def test_shield_yields_only_the_results_that_worked(qdir):
    rows = [{"price": "1"}, {"price": "N/A"}, {"price": "3"}]
    assert list(shield(rows, using=parse, halt_after=None)) == [1.0, 3.0]
    assert len(list(qdir.glob("0*"))) == 1


def test_shield_is_lazy(qdir):
    seen = []

    def process(item):
        seen.append(item)
        return item

    stream = shield(range(5), using=process, halt_after=None)
    assert next(stream) == 0
    assert seen == [0]  # nothing beyond the first item has run yet


def test_shield_requires_something_to_run():
    with pytest.raises(TypeError, match="needs something to run"):
        list(shield([1, 2, 3]))


def test_shield_passes_options_through(tmp_path):
    other = tmp_path / "elsewhere"
    list(shield([{"price": "N/A"}], using=parse, dir=str(other), halt_after=None))
    assert (other / "0001").is_dir()


def test_shield_skips_known_bad_items_quietly(qdir):
    rows = [{"price": "N/A"}]
    assert list(shield(rows, using=parse, halt_after=None)) == []
    assert list(shield(rows, using=parse, halt_after=None)) == []
    assert len(list(qdir.glob("0*"))) == 1


def test_shield_lets_the_circuit_breaker_through(qdir):
    from quarantine.errors import SystemicFailure

    with pytest.raises(SystemicFailure):
        list(shield([{"price": "N/A"}] * 3, using=parse, halt_after=2, skip_known_bad=False))


async def test_ashield_over_a_sync_iterable(qdir):
    async def fetch(url):
        if "bad" in url:
            raise ConnectionError(url)
        return url.upper()

    urls = ["ok/1", "bad/2", "ok/3"]
    got = [result async for result in ashield(urls, using=fetch, halt_after=None)]
    assert got == ["OK/1", "OK/3"]
    assert len(list(qdir.glob("0*"))) == 1


async def test_ashield_over_an_async_iterable(qdir):
    async def source():
        for number in range(4):
            yield number

    async def halve(number):
        return 10 / number  # 0 raises ZeroDivisionError

    got = [result async for result in ashield(source(), using=halve, halt_after=None)]
    assert got == [10.0, 5.0, pytest.approx(10 / 3)]


async def test_ashield_requires_something_to_run():
    with pytest.raises(TypeError, match="needs something to run"):
        _ = [item async for item in ashield([1])]

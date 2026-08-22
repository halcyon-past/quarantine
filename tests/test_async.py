"""`@quarantine` works on `async def` too."""

from __future__ import annotations

import asyncio

import pytest

from quarantine import is_quarantined, quarantine
from quarantine.errors import SystemicFailure


async def test_async_function_is_protected(qdir):
    @quarantine
    async def fetch(url):
        await asyncio.sleep(0)
        if "bad" in url:
            raise ConnectionError(f"cannot reach {url}")
        return url.upper()

    good = await fetch("http://ok")
    bad = await fetch("http://bad")

    assert good == "HTTP://OK"
    assert is_quarantined(bad)
    assert (qdir / "0001" / "traceback.txt").read_text(encoding="utf-8").count("ConnectionError")


async def test_async_wrapper_is_still_a_coroutine_function():
    @quarantine
    async def fetch(url):
        return url

    assert asyncio.iscoroutinefunction(fetch)


async def test_async_gather_keeps_going(q):
    @q.wrap
    async def work(number):
        if number % 2:
            raise ValueError(number)
        return number

    results = await asyncio.gather(*(work(n) for n in range(6)))
    assert [r for r in results if not is_quarantined(r)] == [0, 2, 4]
    assert q.stats.quarantined == 3


async def test_async_circuit_breaker_still_fires(qdir):
    instance_dir = qdir / "async"

    @quarantine(dir=str(instance_dir), halt_after=2)
    async def work(number):
        raise ConnectionError("db down")

    assert is_quarantined(await work(1))
    with pytest.raises(SystemicFailure, match="consecutive failures"):
        await work(2)


async def test_async_records_can_be_retried(q, target_module):
    module = target_module(
        """
        FAIL = True


        async def load(item):
            if FAIL:
                raise ValueError("still broken")
            return item * 2
        """
    )
    await q.acall(module.load, 21)
    assert len(q.records()) == 1

    module.FAIL = False
    result = await q.aretry()
    assert result.recovered == [1]
    assert q.records() == []

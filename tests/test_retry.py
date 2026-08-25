"""Retry: fix the code, re-run only the failures."""

from __future__ import annotations

import re
from typing import Any

import pytest

from quarantine import Quarantine, quarantine, retry
from quarantine.resolve import ResolutionError, resolve_function, unwrap_quarantined

SOURCE = """
FAIL_ON = {"bad", "worse"}


def load(item):
    if item in FAIL_ON:
        raise ValueError(f"cannot load {item}")
    return item.upper()
"""


@pytest.fixture
def module(target_module):
    return target_module(SOURCE)


def test_fixing_the_code_recovers_the_records(q, module):
    for item in ["good", "bad", "worse"]:
        q.call(module.load, item)
    assert [r.id for r in q.records()] == [1, 2]

    module.FAIL_ON = set()
    result = q.retry()

    assert result.recovered == [1, 2]
    assert result.still_failing == []
    assert q.records() == []
    assert q.stats.recovered == 2


def test_transient_failure_is_retried_before_quarantine(q):
    q = q.replace(retries=2)
    attempts = 0

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("temporary failure")
        return "success"

    decorated = q.wrap(flaky)
    assert decorated() == "success"
    assert attempts == 3
    assert q.records() == []


async def test_transient_failure_is_retried_before_quarantine_async(q):
    q = q.replace(retries=2, backoff=0.01)
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("temporary failure")
        return "success"

    decorated = q.wrap(flaky)
    assert await decorated() == "success"
    assert attempts == 3
    assert q.records() == []


def test_a_partial_fix_keeps_what_still_fails(q, module):
    q.call(module.load, "bad")
    q.call(module.load, "worse")

    module.FAIL_ON = {"worse"}
    result = q.retry()

    assert result.recovered == [1]
    assert result.still_failing == [2]
    remaining = q.records()
    assert [r.id for r in remaining] == [2]
    assert remaining[0].attempts == 2
    assert "cannot load worse" in remaining[0].error
    assert "cannot load worse" in remaining[0].traceback_text()


def test_specific_ids_can_be_retried(q, module):
    q.call(module.load, "bad")
    q.call(module.load, "worse")
    module.FAIL_ON = set()

    result = q.retry([2])
    assert result.recovered == [2]
    assert [r.id for r in q.records()] == [1]


def test_unknown_ids_are_reported(q, module):
    q.call(module.load, "bad")
    module.FAIL_ON = set()
    result = q.retry([1, 99])
    assert result.recovered == [1]
    assert result.unretryable == [(99, f"no record 99 in {q.dir}")]


def test_retry_can_be_filtered_by_function(q, module):
    q.call(module.load, "bad")
    q.call(_broken, "x")
    module.FAIL_ON = set()

    result = q.retry(function="load")
    assert result.recovered == [1]
    assert [r.id for r in q.records()] == [2]


def _broken(item):
    raise RuntimeError("always")


def test_dry_run_changes_nothing(q, module):
    q.call(module.load, "bad")
    result = q.retry(dry_run=True)
    assert result.recovered == [1]
    assert len(q.records()) == 1


def test_using_overrides_the_stored_function(q):
    def process(item):
        raise ValueError("nope")

    q.wrap(process)(7)
    seen: list[Any] = []
    result = q.retry(using=seen.append)
    assert result.recovered == [1]
    assert seen == [7]


def test_locally_defined_functions_cannot_be_re_imported(q):
    def process(item):
        raise ValueError("nope")

    q.wrap(process)(1)
    result = q.retry()
    assert result.recovered == []
    assert "defined inside another function" in result.unretryable[0][1]
    assert len(q.records()) == 1  # left alone, not lost


def test_retry_replays_through_the_undecorated_function(q, module):
    decorated = q.wrap(module.load)
    decorated("bad")
    assert len(q.records()) == 1

    # Still failing: exactly one record, not a second one for the retry.
    result = q.retry(using=decorated)
    assert result.still_failing == [1]
    assert len(q.records()) == 1
    assert q.records()[0].attempts == 2


def test_kwargs_are_replayed(q, target_module):
    module = target_module(
        """
        FAIL = True
        SEEN = []


        def load(item, *, factor=1):
            SEEN.append((item, factor))
            if FAIL:
                raise ValueError("broken")
            return item * factor
        """,
        name="qtarget_kwargs",
    )
    q.call(module.load, 3, factor=4)
    module.FAIL = False
    assert q.retry().recovered == [1]
    assert module.SEEN == [(3, 4), (3, 4)]


def test_module_level_retry_helper_uses_the_default_folder(module, qdir):
    @quarantine(dir=str(qdir), halt_after=None)
    def wrapper(item):
        return module.load(item)

    wrapper("bad")
    module.FAIL_ON = set()
    result = retry(dir=str(qdir), using=module.load)
    assert result.recovered == [1]


def test_records_with_unreplayable_input_are_reported(q, monkeypatch, module):
    q.call(module.load, "bad")
    record_dir = q.dir / "0001"
    (record_dir / "input.pkl").unlink()

    result = q.retry()
    assert result.recovered == []
    assert (
        "no replayable input" in result.unretryable[0][1]
        or "cannot rebuild" in (result.unretryable[0][1])
    )


def test_corrupt_payload_is_reported(q, module):
    q.call(module.load, "bad")
    (q.dir / "0001" / "input.pkl").write_bytes(b"not a pickle")
    result = q.retry()
    assert "cannot rebuild the input" in result.unretryable[0][1]


def test_unwrap_only_strips_quarantine_wrappers():
    import functools

    def original(item):
        return item

    @functools.wraps(original)
    def other_decorator(item):
        return original(item)

    other_decorator.__wrapped__ = original
    assert unwrap_quarantined(other_decorator) is other_decorator

    instance = Quarantine("unused", report=False)
    assert unwrap_quarantined(instance.wrap(original)) is original


def test_resolve_function_error_messages(module):
    from quarantine.record import Record

    def make(module_name, function):
        return Record(
            id=1,
            function=function,
            module=module_name,
            fingerprint="fp",
            error_type="E",
            error="",
            created_at="",
            last_failed_at="",
        )

    assert resolve_function(make(module.__name__, "load")) is module.load

    with pytest.raises(ResolutionError, match="cannot import"):
        resolve_function(make("no_such_module_xyz", "load"))
    with pytest.raises(ResolutionError, match="no attribute path"):
        resolve_function(make(module.__name__, "missing"))
    with pytest.raises(ResolutionError, match="not callable"):
        resolve_function(make(module.__name__, "FAIL_ON"))
    with pytest.raises(ResolutionError, match="does not name a function"):
        resolve_function(make(module.__name__, ""))
    with pytest.raises(ResolutionError, match="does not name a module"):
        resolve_function(make("", "load"))


async def test_sync_retry_refuses_async_records_inside_a_loop(q, target_module):
    module = target_module(
        """
        async def load(item):
            raise ValueError("broken")
        """,
        name="qtarget_async",
    )
    await q.acall(module.load, 1)
    with pytest.raises(Exception, match=re.escape("await q.aretry()")):
        q.retry()

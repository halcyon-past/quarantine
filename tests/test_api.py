"""The module-level conveniences, and details that only bite in production."""

from __future__ import annotations

import pickle

import pytest

import quarantine as pkg
from quarantine import (
    QUARANTINED,
    SKIPPED,
    Quarantine,
    aretry,
    clear,
    default,
    quarantine,
    records,
    retry,
    summary,
)
from quarantine.sentinels import Sentinel


def test_default_instance_is_the_one_the_bare_decorator_uses():
    @quarantine
    def process(item):
        raise ValueError("bad")

    assert process.quarantine is default()  # type: ignore[attr-defined]


def test_module_level_helpers_operate_on_the_default_folder(qdir):
    @quarantine
    def process(item):
        raise ValueError("bad")

    process(1)
    process(2)

    assert [r.id for r in records()] == [1, 2]
    assert [r.id for r in records("process")] == []  # qualname is nested here
    line = summary()
    assert line is not None
    assert "2 quarantined" in line
    assert clear() == 2
    assert records() == []
    assert summary() is not None  # the counters remember what happened


def test_module_level_helpers_accept_an_explicit_dir(tmp_path):
    other = tmp_path / "somewhere"
    instance = Quarantine(other, halt_after=None, report=False)
    instance.call(_broken, 1)
    assert [r.id for r in records(dir=str(other))] == [1]
    assert retry(dir=str(other), using=lambda item: item).recovered == [1]
    assert clear(dir=str(other)) == 0


def _broken(item):
    raise RuntimeError("always")


async def test_module_level_aretry(qdir, target_module):
    module = target_module(
        """
        FAIL = True


        async def load(item):
            if FAIL:
                raise ValueError("broken")
            return item
        """,
        name="qtarget_api",
    )
    instance = Quarantine(qdir, halt_after=None, report=False)
    await instance.acall(module.load, 1)
    module.FAIL = False
    result = await aretry(dir=str(qdir))
    assert result.recovered == [1]


def test_sentinels_survive_pickling():
    """Multiprocessing pickles return values; the sentinel must stay itself."""
    assert pickle.loads(pickle.dumps(QUARANTINED)) is QUARANTINED
    assert pickle.loads(pickle.dumps(SKIPPED)) is SKIPPED
    assert QUARANTINED.name == "quarantined"
    assert Sentinel("custom").name == "custom"


def test_reset_forgets_interned_instances(qdir):
    first = default()
    pkg.reset()
    assert default() is not first


def test_records_are_returned_in_id_order(q):
    for index in range(5):
        q.call(_broken, index)
    assert [r.id for r in q.records()] == [1, 2, 3, 4, 5]


def test_stats_and_retry_result_serialise_for_json(q):
    q.call(_broken, 1)
    assert q.stats.as_dict() == {
        "processed": 0,
        "quarantined": 1,
        "skipped": 0,
        "recovered": 0,
    }
    assert q.stats.total == 1
    result = q.retry(using=lambda item: item)
    assert result.as_dict() == {"recovered": [1], "still_failing": [], "unretryable": []}
    assert result.attempted == 1


def test_call_accepts_an_already_wrapped_function(q):
    wrapped = q.wrap(_broken)
    # Calling through .call() must not double-wrap or double-record.
    assert q.call(wrapped, 1) is QUARANTINED
    assert len(q.records()) == 1


def test_threads_share_one_folder_safely(q):
    import threading

    def worker(index):
        q.call(_broken, index)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert q.stats.quarantined == 8
    assert len({r.id for r in q.records()}) == 8


@pytest.mark.parametrize("value", [QUARANTINED, SKIPPED])
def test_sentinels_are_falsy(value):
    assert not value
    assert bool(value) is False

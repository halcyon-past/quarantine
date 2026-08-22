"""The core promise: a bad item does not kill the loop."""

from __future__ import annotations

import json

import pytest

from quarantine import (
    QUARANTINED,
    SKIPPED,
    Quarantine,
    Record,
    is_quarantined,
    is_skipped,
    quarantine,
)


def test_loop_survives_a_bad_item(qdir):
    @quarantine
    def process(row):
        return float(row["price"])

    rows = [{"price": "1.5"}, {"price": "N/A"}, {"price": "2.5"}]
    results = [process(row) for row in rows]

    assert results[0] == 1.5
    assert results[2] == 2.5
    assert is_quarantined(results[1])
    assert len(list(qdir.iterdir())) == 2  # one record + index.json


def test_record_contains_input_traceback_and_meta(q, qdir):
    def process(row):
        return float(row["price"])

    safe = q.wrap(process)
    safe({"id": 8812, "price": "N/A"})

    folder = qdir / "0001"
    assert (folder / "input.pkl").exists()
    assert "8812" in (folder / "input.txt").read_text(encoding="utf-8")
    traceback_text = (folder / "traceback.txt").read_text(encoding="utf-8")
    assert "ValueError" in traceback_text
    assert "float(row" in traceback_text

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["function"] == "test_record_contains_input_traceback_and_meta.<locals>.process"
    assert meta["error_type"] == "ValueError"
    assert meta["attempts"] == 1
    assert meta["python"]
    assert meta["quarantine_version"]
    assert meta["fingerprint"]


def test_successful_calls_return_their_value_and_write_nothing(q, qdir):
    safe = q.wrap(lambda value: value * 2)
    assert safe(21) == 42
    assert not qdir.exists()
    assert q.stats.processed == 1
    assert q.stats.quarantined == 0


def test_sentinels_are_falsy_and_distinguishable():
    assert not QUARANTINED
    assert not SKIPPED
    assert is_quarantined(QUARANTINED)
    assert not is_quarantined(SKIPPED)
    assert is_skipped(SKIPPED)
    assert repr(QUARANTINED) == "<quarantined>"


def test_decorator_preserves_metadata():
    @quarantine
    def process(item):
        """Docstring survives."""

    assert process.__name__ == "process"
    assert process.__doc__ == "Docstring survives."
    assert callable(process.__wrapped__)  # type: ignore[attr-defined]
    assert isinstance(process.quarantine, Quarantine)  # type: ignore[attr-defined]


def test_only_limits_which_errors_are_caught(qdir):
    @quarantine(only=(ValueError,))
    def process(item):
        if item == "value":
            raise ValueError("caught")
        raise TypeError("not caught")

    assert is_quarantined(process("value"))
    with pytest.raises(TypeError, match="not caught"):
        process("type")
    assert (qdir / "0001").is_dir()
    assert not (qdir / "0002").exists()


def test_exclude_wins_over_only(q):
    safe = q.replace(exclude=(KeyError,)).wrap(_raise)
    with pytest.raises(KeyError):
        safe(KeyError("nope"))
    assert is_quarantined(safe(ValueError("fine")))


def _raise(exc):
    raise exc


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_base_exceptions_always_propagate(q, exc):
    safe = q.replace(only=(BaseException,)).wrap(_raise)
    with pytest.raises(type(exc)):
        safe(exc)


def test_quarantine_own_errors_are_never_swallowed(q):
    from quarantine.errors import SystemicFailure

    safe = q.replace(only=(BaseException,)).wrap(_raise)
    with pytest.raises(SystemicFailure):
        safe(SystemicFailure(3, ValueError("db down")))


def test_keyword_arguments_are_recorded(q, qdir):
    def process(row, *, attempt):
        raise RuntimeError("boom")

    q.wrap(process)({"id": 1}, attempt=3)
    text = (qdir / "0001" / "input.txt").read_text(encoding="utf-8")
    assert "attempt=3" in text
    call = q.records()[0].load_call()
    assert call.args == ({"id": 1},)
    assert call.kwargs == {"attempt": 3}
    assert call.item == {"id": 1}


def test_methods_can_be_wrapped(q):
    class Loader:
        def __init__(self):
            self.seen = []

        def load(self, item):
            if item < 0:
                raise ValueError("negative")
            self.seen.append(item)
            return item

    loader = Loader()
    safe = q.wrap(loader.load)
    assert safe(1) == 1
    assert is_quarantined(safe(-1))
    assert loader.seen == [1]
    assert q.records()[0].function.endswith("Loader.load")


def test_generator_functions_are_rejected_with_advice():
    with pytest.raises(TypeError, match="generator"):

        @quarantine
        def stream(items):
            yield from items


def test_async_generator_functions_are_rejected():
    with pytest.raises(TypeError, match="async generator"):

        @quarantine
        async def stream(items):
            for item in items:
                yield item


def test_positional_string_gets_a_helpful_error():
    with pytest.raises(TypeError, match="did you mean"):
        quarantine("mydir")  # type: ignore[call-overload]


def test_configured_decorator_is_shared_between_functions(qdir):
    @quarantine(dir=str(qdir), halt_after=None)
    def one(item):
        raise ValueError("one")

    @quarantine(dir=str(qdir), halt_after=None)
    def two(item):
        raise ValueError("two")

    shared = one.quarantine  # type: ignore[attr-defined]
    assert shared is two.quarantine  # type: ignore[attr-defined]
    one(1)
    two(2)
    assert shared.stats.quarantined == 2


def test_different_settings_get_different_instances(qdir):
    from quarantine import get_quarantine

    first = get_quarantine(qdir, halt_after=5)
    second = get_quarantine(qdir, halt_after=6)
    same = get_quarantine(qdir, halt_after=5)
    assert first is not second
    assert first is same


def test_on_quarantine_hook_receives_the_record(qdir):
    seen: list[Record] = []

    @quarantine(dir=str(qdir), on_quarantine=seen.append)
    def process(item):
        raise ValueError("bad")

    process({"id": 1})
    assert len(seen) == 1
    assert seen[0].error_type == "ValueError"
    assert seen[0].id == 1


def test_broken_hook_is_reported_but_does_not_stop_the_run(qdir, capsys):
    def explode(record):
        raise RuntimeError("slack is down")

    @quarantine(dir=str(qdir), on_quarantine=explode)
    def process(item):
        raise ValueError("bad")

    assert is_quarantined(process(1))
    assert "on_quarantine hook failed" in capsys.readouterr().err


def test_verbose_prints_each_item(qdir, capsys):
    @quarantine(dir=str(qdir), verbose=True)
    def process(item):
        raise ValueError("bad")

    process(1)
    assert "quarantined #0001" in capsys.readouterr().err


def test_wrapping_a_non_callable_is_a_type_error(q):
    with pytest.raises(TypeError, match="callable"):
        q.wrap(42)


def test_quarantine_instance_is_usable_as_a_decorator(qdir):
    instance = Quarantine(qdir, report=False)

    @instance
    def process(item):
        raise ValueError("bad")

    process(1)
    assert instance.stats.quarantined == 1


def test_len_and_iteration_expose_records(q):
    safe = q.wrap(_raise)
    safe(ValueError("a"))
    safe(ValueError("b"))
    assert len(q) == 2
    assert [r.id for r in q] == [1, 2]


def test_clear_empties_the_folder(q, qdir):
    safe = q.wrap(_raise)
    safe(ValueError("a"))
    assert q.clear() == 1
    assert q.records() == []
    assert not (qdir / "0001").exists()

"""The safety valves: circuit breaker, disk cap, and config validation."""

from __future__ import annotations

import pytest

from quarantine import Quarantine, is_quarantined, quarantine
from quarantine.core import Config
from quarantine.errors import QuarantineFull, SystemicFailure


def test_halts_after_consecutive_failures(qdir):
    @quarantine(dir=str(qdir), halt_after=3)
    def process(item):
        raise ConnectionError("db.internal:5432 refused")

    assert is_quarantined(process(1))
    assert is_quarantined(process(2))
    with pytest.raises(SystemicFailure) as caught:
        process(3)

    assert caught.value.count == 3
    assert isinstance(caught.value.last_error, ConnectionError)
    assert "looks systemic, not bad data" in str(caught.value)
    assert "db.internal:5432 refused" in str(caught.value)
    assert caught.value.__cause__ is not None
    assert len(list(qdir.glob("0*"))) == 3  # the halting item is still saved


def test_a_success_resets_the_streak(q):
    breaker = q.replace(halt_after=2)

    def process(item):
        if "bad" in item:
            raise ValueError(item)
        return item

    safe = breaker.wrap(process)
    safe("bad")
    assert breaker.stats.consecutive_failures == 1
    safe("good")
    assert breaker.stats.consecutive_failures == 0
    safe("other-bad")
    assert breaker.stats.consecutive_failures == 1  # no halt: not consecutive


def test_halting_can_be_disabled(q):
    safe = q.replace(halt_after=None).wrap(_always_fails)
    for index in range(10):
        assert is_quarantined(safe(index))


def _always_fails(item):
    raise ValueError(f"item {item}")


def test_max_items_caps_the_folder(qdir):
    @quarantine(dir=str(qdir), halt_after=None, max_items=2)
    def process(item):
        raise ValueError(f"item {item}")

    process(1)
    process(2)
    with pytest.raises(QuarantineFull) as caught:
        process(3)

    assert caught.value.max_items == 2
    assert "raise max_items" in str(caught.value)
    assert isinstance(caught.value.__cause__, ValueError)
    assert len(list(qdir.glob("0*"))) == 2


def test_the_cap_is_released_by_clearing(q):
    limited = q.replace(max_items=1, halt_after=None)
    safe = limited.wrap(_always_fails)
    safe(1)
    with pytest.raises(QuarantineFull):
        safe(2)
    limited.clear()
    assert is_quarantined(safe(3))


def test_retrying_frees_capacity(q, target_module):
    module = target_module(
        """
        FAIL = True


        def load(item):
            if FAIL:
                raise ValueError("broken")
            return item
        """
    )
    limited = q.replace(max_items=1, halt_after=None)
    limited.call(module.load, 1)
    with pytest.raises(QuarantineFull):
        limited.call(module.load, 2)

    module.FAIL = False
    assert limited.retry().recovered == [1]
    module.FAIL = True
    assert is_quarantined(limited.call(module.load, 3))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"halt_after": 0}, "must be >= 1"),
        ({"max_items": -1}, "must be >= 1"),
        ({"halt_after": "many"}, "must be an int or None"),
        ({"halt_after": True}, "must be an int or None"),
        ({"only": "ValueError"}, "must contain exception classes"),
        ({"only": 42}, "exception class or a tuple"),
        ({"exclude": (int,)}, "must contain exception classes"),
        ({"on_quarantine": "notcallable"}, "must be callable"),
        ({"redact": [123]}, "must be strings"),
    ],
)
def test_bad_configuration_fails_immediately(tmp_path, kwargs, message):
    with pytest.raises((TypeError, ValueError), match=message):
        Quarantine(tmp_path / "qq", report=False, **kwargs)


def test_config_is_hashable_and_normalised(tmp_path):
    config = Config(dir=tmp_path, only=(ValueError,), redact=("a",))
    assert config.only == (ValueError,)
    assert hash(config) == hash(Config(dir=tmp_path, only=(ValueError,), redact=("a",)))
    assert config.dir == tmp_path


def test_string_arguments_are_normalised(tmp_path):
    instance = Quarantine(str(tmp_path / "qq"), only=ValueError, redact=["a"], report=False)
    assert instance.dir == tmp_path / "qq"
    assert instance.config.only == (ValueError,)
    assert instance.config.redact == ("a",)


def test_repr_mentions_the_folder(tmp_path):
    assert "qq" in repr(Quarantine(tmp_path / "qq", report=False))

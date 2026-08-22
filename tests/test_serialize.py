"""Serialization must always produce *something* readable."""

from __future__ import annotations

import json
import pickle
import threading
from typing import Any

import pytest

from quarantine.redact import Redactor
from quarantine.serialize import (
    JSON,
    PICKLE,
    REPR,
    Call,
    Serialized,
    describe_call,
    deserialize,
    preview,
    redact_call,
    render_input_text,
    safe_repr,
    serialize,
)


def data_of(result: Serialized) -> bytes:
    """The serialized bytes, asserted to be present."""
    assert result.data is not None
    return result.data


def test_pickle_is_preferred_and_round_trips():
    call = Call((["a", 1],), {"flag": True})
    result = serialize(call)
    assert result.format == PICKLE
    assert not result.lossy
    restored = deserialize(result.format, data_of(result))
    assert restored.args == call.args
    assert restored.kwargs == call.kwargs


def test_unpicklable_input_falls_back_to_json():
    call = Call(({"id": 1, "lock": threading.Lock()},))
    result = serialize(call)
    assert result.format == JSON
    assert result.lossy
    assert result.reason  # says why pickle was not used
    payload = json.loads(data_of(result).decode("utf-8"))
    assert payload["args"][0]["id"] == 1
    assert "lock" in payload["args"][0]


def test_object_that_defeats_pickle_still_gets_json_with_a_repr():
    class Hostile:
        def __reduce__(self):
            raise RuntimeError("no pickling for you")

        def __repr__(self):
            raise RuntimeError("no repr either")

    result = serialize(Call((Hostile(),)))
    assert result.format == JSON
    assert result.lossy
    assert "unreprable Hostile" in data_of(result).decode("utf-8")


def test_input_that_defeats_both_formats_falls_back_to_repr_only():
    # unpicklable (a lock) *and* not JSON-encodable (a reference cycle)
    row: dict[str, Any] = {"lock": threading.Lock()}
    row["self"] = row

    result = serialize(Call((row,)))
    assert result.format == REPR
    assert result.data is None
    assert result.lossy
    with pytest.raises(ValueError, match="repr is not reversible"):
        deserialize(REPR, b"")


def test_cyclic_structure_survives_serialization():
    row: dict[str, Any] = {"id": 1}
    row["self"] = row
    result = serialize(Call((row,)))
    assert result.format == PICKLE  # pickle handles cycles natively
    assert deserialize(result.format, data_of(result)).args[0]["id"] == 1


def test_safe_repr_survives_a_broken_repr():
    class Broken:
        def __repr__(self):
            raise ValueError("nope")

    text = safe_repr(Broken())
    assert "unreprable Broken" in text
    assert "ValueError" in text


def test_safe_repr_truncates():
    text = safe_repr("x" * 500, limit=50)
    assert len(text) < 200
    assert "truncated" in text


def test_preview_is_one_line():
    call = Call(({"id": 1, "note": "line one\nline two"},))
    assert "\n" not in preview(call)
    assert len(preview(call, limit=20)) <= 20


def test_describe_and_render_input_text():
    call = Call((1, "two"), {"three": 3})
    assert describe_call("process", call) == "process(1, 'two', three=3)"
    text = render_input_text("process", call)
    assert "args[0] = 1" in text
    assert "kwargs['three'] = 3" in text
    assert "(called with no arguments)" in render_input_text("f", Call())


def test_call_item_prefers_first_positional():
    assert Call((1, 2)).item == 1
    assert Call((), {"row": "r"}).item == "r"
    assert Call().item is None


def test_deserialize_rejects_malformed_payloads():
    with pytest.raises(ValueError, match="malformed payload"):
        deserialize(PICKLE, pickle.dumps([1, 2, 3]))
    assert deserialize(JSON, b"{}") == Call((), {})


def test_redact_call_scrubs_values_and_keyword_names():
    redactor = Redactor(["password", "*token*"])
    call = Call(({"user": "ann", "password": "hunter2"},), {"api_token": "sk-live"})
    clean = redact_call(call, redactor)
    assert clean.args[0]["user"] == "ann"
    assert clean.args[0]["password"] == redactor.placeholder
    assert clean.kwargs["api_token"] == redactor.placeholder
    assert redactor.hits == {"password", "api_token"}
    # the original is untouched
    assert call.args[0]["password"] == "hunter2"


def test_redact_call_is_a_no_op_without_patterns():
    call = Call(({"password": "hunter2"},))
    assert redact_call(call, Redactor([])) is call

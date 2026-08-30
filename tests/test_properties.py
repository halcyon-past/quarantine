"""Property-based tests: the promises hold for inputs nobody thought to write.

The library's one promise is *never lose a failure*, and it rests on three
functions behaving for arbitrary inputs: serialization always produces
something, redaction never leaks and never mutates, and fingerprints do not
depend on incidental ordering. Hypothesis searches for the counterexample.

The suite-wide ``isolated`` fixture is function-scoped (it only resets global
state, which these pure-function properties never touch), so that health check
is suppressed deliberately rather than by accident.
"""

from __future__ import annotations

import copy
import threading

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from quarantine.fingerprint import fingerprint
from quarantine.redact import PLACEHOLDER, Redactor
from quarantine.serialize import Call, deserialize, preview, render_input_text, safe_repr, serialize

RELAXED = settings(
    max_examples=75,
    deadline=None,  # CI boxes are slow and shared; flakiness helps nobody
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False)
    | st.text()
    | st.binary(max_size=64)
)

picklable = st.recursive(
    scalars,
    lambda kids: (
        st.lists(kids, max_size=4)
        | st.dictionaries(st.text(max_size=8), kids, max_size=4)
        | st.tuples(kids)
    ),
    max_leaves=25,
)

anything = st.recursive(
    scalars | st.builds(threading.Lock) | st.just(serialize),
    lambda kids: (
        st.lists(kids, max_size=3) | st.dictionaries(st.text(max_size=6), kids, max_size=3)
    ),
    max_leaves=10,
)


class _BrokenRepr:
    def __repr__(self) -> str:
        raise RuntimeError("nope")


@RELAXED
@given(value=picklable)
def test_picklable_inputs_round_trip_exactly(value):
    call = Call((value,), {"key": value})
    stored = serialize(call)
    assert stored.format == "pickle"
    assert not stored.lossy
    assert stored.data is not None
    assert deserialize(stored.format, stored.data) == call


@RELAXED
@given(value=anything)
def test_any_input_serializes_to_something_readable(value):
    call = Call((value,), {})
    stored = serialize(call)
    assert stored.format in {"pickle", "json", "repr"}
    if stored.format != "repr":
        assert stored.data is not None
        assert isinstance(deserialize(stored.format, stored.data), Call)
    else:
        assert stored.lossy and stored.reason
    assert isinstance(render_input_text("process", call), str)
    assert isinstance(preview(call), str)


@RELAXED
@given(payload=picklable, key=st.sampled_from(["password", "api_token", "SECRET"]))
def test_redaction_never_leaks_and_never_mutates(payload, key):
    secret = "hunter2-xyzzy-do-not-leak"
    value = {"outer": [payload, {key: secret}], "kept": payload}
    snapshot = copy.deepcopy(value)

    cleaned = Redactor(("password", "*token", "secret")).apply(value)

    assert secret not in repr(cleaned)
    assert PLACEHOLDER in repr(cleaned)
    assert value == snapshot, "redaction must never mutate the caller's object"


@RELAXED
@given(entries=st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), max_size=6))
def test_fingerprint_ignores_dict_insertion_order(entries):
    reordered = dict(reversed(list(entries.items())))
    assert fingerprint("process", Call((entries,), {})) == fingerprint(
        "process", Call((reordered,), {})
    )
    assert fingerprint("process", Call((entries,), {})) != fingerprint(
        "another", Call((entries,), {})
    )


@RELAXED
@given(value=anything | st.builds(_BrokenRepr), limit=st.integers(min_value=10, max_value=200))
def test_safe_repr_never_raises_and_respects_its_limit(value, limit):
    text = safe_repr(value, limit)
    assert isinstance(text, str)
    assert len(text) <= limit + len("... [999999999 more chars truncated]")

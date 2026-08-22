"""Turn a failed call into bytes on disk, and back again.

The fallback chain is ``pickle -> JSON -> repr``. Fidelity degrades, but
*something* readable is always written: a record you cannot open is a record
that did not help you.
"""

from __future__ import annotations

import contextlib
import json
import pickle
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .redact import Redactor

__all__ = [
    "Call",
    "Serialized",
    "describe_call",
    "deserialize",
    "preview",
    "redact_call",
    "safe_repr",
    "serialize",
]

PICKLE = "pickle"
JSON = "json"
REPR = "repr"

REPR_LIMIT = 8_000
"""How much of ``input.txt`` we are willing to write per value."""

PREVIEW_LIMIT = 120
"""How much of the input shows up in ``quarantine list``."""


@dataclass(frozen=True)
class Call:
    """The exact call that failed: positional args plus keyword args."""

    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def item(self) -> Any:
        """The 'item' - first positional argument, else first keyword value, else ``None``."""
        if self.args:
            return self.args[0]
        for value in self.kwargs.values():
            return value
        return None

    def as_payload(self) -> dict[str, Any]:
        """Plain-dict form, which is what actually gets serialized."""
        return {"args": list(self.args), "kwargs": dict(self.kwargs)}


@dataclass(frozen=True)
class Serialized:
    """The outcome of serializing a call."""

    format: str
    data: bytes | None
    lossy: bool = False
    reason: str | None = None


class _LossyEncoder(json.JSONEncoder):
    """JSON encoder that reprs whatever it cannot encode, and says that it did."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.lossy = False

    def default(self, o: Any) -> Any:
        """Fall back to a repr string for objects JSON knows nothing about."""
        self.lossy = True
        return safe_repr(o, REPR_LIMIT)


def serialize(call: Call) -> Serialized:
    """Serialize *call* with the highest-fidelity format that works."""
    payload = call.as_payload()

    try:
        return Serialized(PICKLE, pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception as exc:  # noqa: BLE001 - any failure just means "try the next format"
        pickle_reason = f"{type(exc).__name__}: {exc}"

    with contextlib.suppress(Exception):
        return Serialized(JSON, json.dumps(payload, sort_keys=True).encode("utf-8"))

    encoder = _LossyEncoder(sort_keys=True)
    try:
        text = encoder.encode(payload)
    except Exception as exc:  # noqa: BLE001 - cyclic or hostile objects land here
        return Serialized(REPR, None, lossy=True, reason=f"{pickle_reason}; {exc}")
    return Serialized(JSON, text.encode("utf-8"), lossy=encoder.lossy, reason=pickle_reason)


def deserialize(fmt: str, data: bytes) -> Call:
    """Rebuild a :class:`Call` from ``input.pkl`` / ``input.json`` bytes."""
    if fmt == PICKLE:
        payload = pickle.loads(data)
    elif fmt == JSON:
        payload = json.loads(data.decode("utf-8"))
    else:
        raise ValueError(f"cannot rebuild the input from format {fmt!r} (repr is not reversible)")
    if not isinstance(payload, dict):
        # ValueError, not TypeError: the *file* is malformed, the argument is fine.
        raise ValueError("malformed payload: expected a mapping")  # noqa: TRY004
    args = tuple(payload.get("args") or ())
    kwargs = dict(payload.get("kwargs") or {})
    return Call(args, kwargs)


def safe_repr(obj: Any, limit: int = REPR_LIMIT) -> str:
    """``repr(obj)``, truncated, and never raising - not even for a broken ``__repr__``."""
    try:
        text = repr(obj)
    except Exception as exc:  # noqa: BLE001 - a broken __repr__ must not lose the record
        text = f"<unreprable {type(obj).__name__}: {type(exc).__name__}: {exc}>"
    if len(text) > limit:
        omitted = len(text) - limit
        return f"{text[:limit]}... [{omitted} more chars truncated]"
    return text


def preview(call: Call, limit: int = PREVIEW_LIMIT) -> str:
    """One-line, single-space preview of the input for table output."""
    text = safe_repr(call.item, limit * 4)
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "\u2026"
    return collapsed


def describe_call(name: str, call: Call, limit: int = REPR_LIMIT) -> str:
    """Human-readable rendering of the call, as it would have been typed."""
    parts = [safe_repr(a, limit) for a in call.args]
    parts += [f"{k}={safe_repr(v, limit)}" for k, v in call.kwargs.items()]
    return f"{name}({', '.join(parts)})"


def render_input_text(name: str, call: Call, formatter: Callable[[Any], str] = safe_repr) -> str:
    """Build the body of ``input.txt``: the call, then each argument on its own."""
    lines = [f"# call: {describe_call(name, call)}", ""]
    for index, value in enumerate(call.args):
        lines.append(f"args[{index}] = {formatter(value)}")
    for key, value in call.kwargs.items():
        lines.append(f"kwargs[{key!r}] = {formatter(value)}")
    if not call.args and not call.kwargs:
        lines.append("(called with no arguments)")
    return "\n".join(lines) + "\n"


def redact_call(call: Call, redactor: Redactor) -> Call:
    """Return a copy of *call* with every matching field scrubbed.

    Keyword *names* are matched too, which a plain value walk would miss:
    ``process(row, api_key="sk-live-...")`` must not put that on disk.
    """
    if not redactor.active:
        return call
    args = tuple(redactor.apply(value) for value in call.args)
    kwargs: dict[str, Any] = {}
    for key, value in call.kwargs.items():
        if redactor.matches(key):
            redactor.hits.add(str(key))
            kwargs[key] = redactor.placeholder
        else:
            kwargs[key] = redactor.apply(value)
    return Call(args, kwargs)

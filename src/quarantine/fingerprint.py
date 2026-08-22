"""Stable content hash of a call, used to dedupe reruns.

Two runs of the same script must produce the same fingerprint for the same
input, so the ordering of dict keys must not matter - which is why JSON with
sorted keys is tried before anything else.
"""

from __future__ import annotations

import hashlib
import json
import pickle
from collections.abc import Callable

from .redact import Redactor
from .serialize import Call, redact_call, safe_repr

__all__ = ["fingerprint", "fingerprint_source"]

_DIGEST_SIZE = 16


def _canonical(payload: object) -> bytes:
    """Best-effort stable bytes for *payload*, trying each strategy in turn."""
    strategies: tuple[Callable[[object], bytes], ...] = (
        lambda value: json.dumps(value, sort_keys=True, default=safe_repr).encode("utf-8"),
        lambda value: repr(value).encode("utf-8", "replace"),
        lambda value: pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL),
    )
    for strategy in strategies:
        try:
            return strategy(payload)
        except Exception:  # noqa: BLE001, S112 - fall through to the next strategy
            continue
    return b"<unfingerprintable>"


def fingerprint(function: str, call: Call) -> str:
    """Hex digest identifying *call* to *function*.

    Computed from the **redacted** payload, so secrets never enter the hash.
    Inputs that differ only in a redacted field therefore count as the same
    item - a deliberate trade of precision for safety.
    """
    hasher = hashlib.blake2b(digest_size=_DIGEST_SIZE)
    hasher.update(function.encode("utf-8", "replace"))
    hasher.update(b"\0")
    hasher.update(_canonical(call.as_payload()))
    return hasher.hexdigest()


def fingerprint_source(function: str, call: Call, redactor: Redactor) -> str:
    """Fingerprint *call* after redaction, which is what actually gets stored."""
    return fingerprint(function, redact_call(call, redactor))

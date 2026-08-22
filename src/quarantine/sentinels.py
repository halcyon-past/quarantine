"""Falsy sentinels returned in place of a value that never got computed."""

from __future__ import annotations

from typing import Any

__all__ = ["QUARANTINED", "SKIPPED", "Sentinel", "is_quarantined", "is_skipped"]


class Sentinel:
    """A named, falsy, singleton-ish marker object.

    Falsy on purpose: ``if process(item):`` does the sensible thing when the
    item never actually processed.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        """The sentinel's display name."""
        return self._name

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<{self._name}>"

    def __reduce__(self) -> Any:
        # Survive pickling as the same object (multiprocessing, caches).
        return (_lookup, (self._name,))


QUARANTINED = Sentinel("quarantined")
"""Returned by a wrapped function whose call raised and was quarantined."""

SKIPPED = Sentinel("skipped")
"""Returned when the input was already in quarantine, so it was not re-run."""

_BY_NAME = {"quarantined": QUARANTINED, "skipped": SKIPPED}


def _lookup(name: str) -> Sentinel:
    return _BY_NAME[name]


def is_quarantined(value: object) -> bool:
    """Return ``True`` if *value* is the marker for a quarantined call."""
    return value is QUARANTINED


def is_skipped(value: object) -> bool:
    """Return ``True`` if *value* is the marker for a skipped known-bad call."""
    return value is SKIPPED

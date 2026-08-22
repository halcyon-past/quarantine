"""Exceptions raised by quarantine itself.

These are deliberately *never* quarantined: if quarantine's own machinery says
stop, swallowing that decision would defeat the point.
"""

from __future__ import annotations

__all__ = [
    "QuarantineError",
    "QuarantineFull",
    "StorageError",
    "SystemicFailure",
]


class QuarantineError(Exception):
    """Base class for every error raised by quarantine itself."""


class SystemicFailure(QuarantineError):
    """Too many consecutive failures - the cause looks systemic, not per-item.

    Raised by the circuit breaker (``halt_after``). If 50 items fail in a row,
    the database is probably down; quarantining the remaining 9 950 items would
    be noise, not information.
    """

    def __init__(self, count: int, last_error: BaseException) -> None:
        self.count = count
        self.last_error = last_error
        detail = f"{type(last_error).__name__}: {last_error}"
        super().__init__(
            f"✋ {count} consecutive failures - this looks systemic, not bad data. Halting.\n"
            f"   Last error: {detail}"
        )


class QuarantineFull(QuarantineError):
    """The quarantine folder hit ``max_items``.

    Raised instead of silently dropping the failure, chained from the original
    exception so nothing is lost. Raise the cap, or clear the folder.
    """

    def __init__(self, max_items: int, directory: str) -> None:
        self.max_items = max_items
        self.directory = directory
        super().__init__(
            f"quarantine is full: {max_items} items already in {directory}. "
            f"Fix and `quarantine retry`, run `quarantine clear`, "
            f"or raise max_items."
        )


class StorageError(QuarantineError):
    """The quarantine folder could not be read or written."""

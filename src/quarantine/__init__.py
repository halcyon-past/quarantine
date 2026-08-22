"""quarantine - when one bad item crashes your loop of 10,000, don't crash.

Set it aside, keep going, fix it later::

    from quarantine import quarantine

    @quarantine
    def process(item):
        ...

    for item in items:
        process(item)

Bad items are written to ``.quarantine/`` with their input and traceback, and
the loop keeps running. Afterwards: ``quarantine list``, ``quarantine retry``,
``quarantine debug 2``.
"""

from __future__ import annotations

from ._version import __version__
from .api import (
    aretry,
    ashield,
    clear,
    default,
    get_quarantine,
    quarantine,
    records,
    reset,
    retry,
    shield,
    summary,
)
from .core import Config, Quarantine, RetryResult, Stats
from .errors import QuarantineError, QuarantineFull, StorageError, SystemicFailure
from .record import Record
from .redact import PLACEHOLDER
from .resolve import ResolutionError
from .sentinels import QUARANTINED, SKIPPED, Sentinel, is_quarantined, is_skipped
from .serialize import Call
from .store import Store

__all__ = [
    "PLACEHOLDER",
    "QUARANTINED",
    "SKIPPED",
    "Call",
    "Config",
    "Quarantine",
    "QuarantineError",
    "QuarantineFull",
    "Record",
    "ResolutionError",
    "RetryResult",
    "Sentinel",
    "Stats",
    "StorageError",
    "Store",
    "SystemicFailure",
    "__version__",
    "aretry",
    "ashield",
    "clear",
    "default",
    "get_quarantine",
    "is_quarantined",
    "is_skipped",
    "quarantine",
    "records",
    "reset",
    "retry",
    "shield",
    "summary",
]

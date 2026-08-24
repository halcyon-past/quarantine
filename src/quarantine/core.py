"""The engine: run a call, and if it blows up, file it away instead of dying."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar, cast

from .errors import QuarantineError, QuarantineFull, StorageError, SystemicFailure
from .fingerprint import fingerprint_source
from .record import Record, utcnow
from .redact import Redactor
from .reporting import register_reporter, warn
from .resolve import ResolutionError, resolve_function, unwrap_quarantined
from .sentinels import QUARANTINED, SKIPPED
from .serialize import Call, preview, redact_call, render_input_text, serialize
from .store import Store, default_dir

__all__ = ["Config", "Quarantine", "RetryResult", "Stats"]

F = TypeVar("F", bound=Callable[..., Any])

NEVER_QUARANTINE: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
    QuarantineError,
)
"""Exceptions that always propagate, whatever ``only`` says.

Ctrl-C must interrupt, ``sys.exit()`` must exit, and quarantine's own
decisions - "halt, this is systemic" - must not be swallowed by quarantine.
"""

DEFAULT_HALT_AFTER = 50
DEFAULT_MAX_ITEMS = 10_000


@dataclass(frozen=True)
class Config:
    """Immutable, hashable settings for a :class:`Quarantine`.

    Hashable on purpose: identical settings map to the *same* instance, so two
    decorated functions sharing a folder also share one counter and print one
    summary line.
    """

    dir: Path = field(default_factory=default_dir)
    only: tuple[type[BaseException], ...] = (Exception,)
    exclude: tuple[type[BaseException], ...] = ()
    halt_after: int | None = DEFAULT_HALT_AFTER
    max_items: int | None = DEFAULT_MAX_ITEMS
    redact: tuple[str, ...] = ()
    on_quarantine: Callable[[Record], None] | None = None
    skip_known_bad: bool = True
    report: bool = True
    verbose: bool = False
    retries: int = 0
    backoff: float = 2.0

    def __post_init__(self) -> None:
        """Validate eagerly: a typo in a decorator argument should fail loudly."""
        object.__setattr__(self, "dir", Path(self.dir))
        object.__setattr__(self, "only", _exception_tuple(self.only, "only"))
        object.__setattr__(self, "exclude", _exception_tuple(self.exclude, "exclude"))
        object.__setattr__(self, "redact", tuple(self.redact))
        _check_positive(self.halt_after, "halt_after")
        _check_positive(self.max_items, "max_items")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")
        if self.backoff < 0:
            raise ValueError("backoff must be >= 0")
        if self.on_quarantine is not None and not callable(self.on_quarantine):
            raise TypeError("on_quarantine must be callable")
        Redactor(self.redact)  # raises TypeError on non-string field names


def _exception_tuple(value: Any, name: str) -> tuple[type[BaseException], ...]:
    if isinstance(value, type):
        value = (value,)
    try:
        items = tuple(value)
    except TypeError:
        raise TypeError(f"{name} must be an exception class or a tuple of them") from None
    for item in items:
        if not (isinstance(item, type) and issubclass(item, BaseException)):
            raise TypeError(f"{name} must contain exception classes, got {item!r}")
    return items


def _check_positive(value: int | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int or None, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1 or None, got {value}")


@dataclass
class Stats:
    """Counters for one :class:`Quarantine`."""

    processed: int = 0
    quarantined: int = 0
    skipped: int = 0
    recovered: int = 0
    consecutive_failures: int = 0

    @property
    def total(self) -> int:
        """Every call that reached the wrapper."""
        return self.processed + self.quarantined + self.skipped

    def as_dict(self) -> dict[str, int]:
        """Plain-dict form, for ``--json`` output."""
        return {
            "processed": self.processed,
            "quarantined": self.quarantined,
            "skipped": self.skipped,
            "recovered": self.recovered,
        }


@dataclass
class RetryResult:
    """Outcome of retrying quarantined records."""

    recovered: list[int] = field(default_factory=list)
    still_failing: list[int] = field(default_factory=list)
    unretryable: list[tuple[int, str]] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        """How many records were actually re-run."""
        return len(self.recovered) + len(self.still_failing)

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict form, for ``--json`` output."""
        return {
            "recovered": list(self.recovered),
            "still_failing": list(self.still_failing),
            "unretryable": [{"id": rid, "reason": why} for rid, why in self.unretryable],
        }


class Quarantine:
    """A quarantine folder plus the policy for putting things in it."""

    def __init__(
        self,
        dir: str | Path | None = None,
        *,
        only: Any = (Exception,),
        exclude: Any = (),
        halt_after: int | None = DEFAULT_HALT_AFTER,
        max_items: int | None = DEFAULT_MAX_ITEMS,
        redact: Iterable[str] = (),
        on_quarantine: Callable[[Record], None] | None = None,
        skip_known_bad: bool = True,
        report: bool = True,
        verbose: bool = False,
        retries: int = 0,
        backoff: float = 2.0,
        config: Config | None = None,
    ) -> None:
        if config is None:
            config = Config(
                dir=Path(dir) if dir is not None else default_dir(),
                only=only,
                exclude=exclude,
                halt_after=halt_after,
                max_items=max_items,
                redact=tuple(redact),
                on_quarantine=on_quarantine,
                skip_known_bad=skip_known_bad,
                report=report,
                verbose=verbose,
                retries=retries,
                backoff=backoff,
            )
        self.config = config
        self.store = Store(config.dir)
        self.stats = Stats()
        self._mutex = threading.RLock()
        self._known: dict[str, int] | None = None
        self._count: int | None = None
        if config.report:
            register_reporter(self)

    def __repr__(self) -> str:
        return f"Quarantine({str(self.config.dir)!r})"

    @property
    def dir(self) -> Path:
        """The quarantine folder this instance writes to."""
        return self.config.dir

    def replace(self, **changes: Any) -> Quarantine:
        """A new instance with some settings changed."""
        return Quarantine(config=replace(self.config, **changes))

    # -- wrapping -------------------------------------------------------

    def wrap(self, fn: F) -> F:
        """Return *fn* with quarantine protection around it."""
        if not callable(fn):
            raise TypeError(f"@quarantine expects a callable, got {type(fn).__name__}")
        _reject_generator(fn)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self.acall(fn, *args, **kwargs)

            return cast(F, _finish_wrapper(async_wrapper, fn, self))

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return self.call(fn, *args, **kwargs)

        return cast(F, _finish_wrapper(wrapper, fn, self))

    __call__ = wrap

    # -- calling --------------------------------------------------------

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call *fn*, quarantining a failure instead of raising it, with retry support."""
        target = unwrap_quarantined(fn)
        prepared = self._precheck(target, args, kwargs)
        if prepared is SKIPPED:
            return SKIPPED

        attempts = self.config.retries + 1
        current_delay = 1.0

        for attempt in range(attempts):
            try:
                with self._mutex:
                    self.stats.processed += 1
                result = fn(*args, **kwargs)
                with self._mutex:
                    self.stats.consecutive_failures = 0
                return result
            except NEVER_QUARANTINE:
                raise
            except BaseException as e:
                if not isinstance(e, self.config.only) or isinstance(e, self.config.exclude):
                    raise
                
                if attempt < attempts - 1:
                    time.sleep(current_delay)
                    current_delay *= self.config.backoff
                    continue

                with self._mutex:
                    self.stats.consecutive_failures += 1
                    if self.config.halt_after and self.stats.consecutive_failures >= self.config.halt_after:
                        raise SystemicFailure(f"Halted after {self.stats.consecutive_failures} straight failures") from e
                
                return self._quarantine_failure(prepared, e)

    async def acall(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Async call *fn*, quarantining a failure instead of raising it, with retry support."""
        target = unwrap_quarantined(fn)
        prepared = self._precheck(target, args, kwargs)
        if prepared is SKIPPED:
            return SKIPPED

        attempts = self.config.retries + 1
        current_delay = 1.0

        for attempt in range(attempts):
            try:

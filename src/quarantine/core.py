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
    retries: int = 0
    backoff: float = 0.0
    redact: tuple[str, ...] = ()
    on_quarantine: Callable[[Record], None] | None = None
    skip_known_bad: bool = True
    report: bool = True
    verbose: bool = False

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
    """A quarantine folder plus the policy for putting things in it.

    The decorator is sugar over this class; use it directly when you want an
    explicit object to pass around, inspect or point at a custom folder::

        q = Quarantine("build/bad-rows", halt_after=10)
        safe = q.wrap(process)
    """

    def __init__(
        self,
        dir: str | Path | None = None,  # noqa: A002 - matches the documented keyword
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
        """Call *fn*, quarantining a failure instead of raising it."""
        target = unwrap_quarantined(fn)
        prepared = self._precheck(target, args, kwargs)
        if prepared is SKIPPED:
            return SKIPPED
        attempts = self.config.retries + 1
        for attempt in range(attempts):
            try:
                result = target(*args, **kwargs)
                return self._on_success(result)
            except BaseException as exc:  # noqa: BLE001 - re-raised unless quarantinable
                if attempt < self.config.retries and self._should_quarantine(exc):
                    if self.config.backoff > 0:
                        time.sleep(self.config.backoff)
                    continue
                return self._on_failure(target, exc, args, kwargs, prepared)
        raise AssertionError("unreachable")

    async def acall(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """``await`` *fn*, quarantining a failure instead of raising it."""
        target = unwrap_quarantined(fn)
        prepared = self._precheck(target, args, kwargs)
        if prepared is SKIPPED:
            return SKIPPED
        attempts = self.config.retries + 1
        for attempt in range(attempts):
            try:
                result = target(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return self._on_success(result)
            except BaseException as exc:  # noqa: BLE001 - re-raised unless quarantinable
                if attempt < self.config.retries and self._should_quarantine(exc):
                    if self.config.backoff > 0:
                        await asyncio.sleep(self.config.backoff)
                    continue
                return self._on_failure(target, exc, args, kwargs, prepared)
        raise AssertionError("unreachable")

    # -- inspection -----------------------------------------------------

    def records(self, function: str | None = None) -> list[Record]:
        """Everything currently in quarantine, optionally filtered by function name."""
        records = self.store.records()
        if function is not None:
            records = [r for r in records if function in (r.function, r.qualified_name)]
        return records

    def __len__(self) -> int:
        return self.store.count()

    def __iter__(self) -> Iterator[Record]:
        return iter(self.store.records())

    def clear(self) -> int:
        """Empty the folder and reset the cached bookkeeping."""
        removed = self.store.clear()
        with self._mutex:
            self._known = None
            self._count = None
            self.stats.consecutive_failures = 0
        return removed

    def summary_line(self) -> str | None:
        """The end-of-run one-liner, or ``None`` when there is nothing to report."""
        stats = self.stats
        if not (stats.quarantined or stats.skipped or stats.recovered):
            return None
        parts = [f"✓ {stats.processed:,} processed"]
        if stats.quarantined:
            parts.append(f"✗ {stats.quarantined:,} quarantined → {self.dir}/")
        if stats.skipped:
            parts.append(f"⏭ {stats.skipped:,} skipped (already quarantined)")
        if stats.recovered:
            parts.append(f"↺ {stats.recovered:,} recovered")
        line = " · ".join(parts)
        if stats.quarantined or stats.skipped:
            line += "  (run `quarantine retry` after fixing)"
        return line

    # -- retrying -------------------------------------------------------

    def retry(
        self,
        ids: Sequence[int] | None = None,
        *,
        using: Callable[..., Any] | None = None,
        function: str | None = None,
        dry_run: bool = False,
        import_from: str | Path | None = None,
    ) -> RetryResult:
        """Re-run quarantined records; drop the ones that now succeed.

        Records are replayed against the *undecorated* function, so a retry can
        never create a second record for the same item. A record that succeeds
        is deleted; one that fails again keeps its place, with an incremented
        attempt count and a fresh traceback.

        Async functions are run with :func:`asyncio.run`. Inside an already
        running event loop, use :meth:`aretry` instead.
        """
        result = RetryResult()
        plan = self._retry_plan(
            ids,
            using=using,
            function=function,
            result=result,
            dry_run=dry_run,
            import_from=import_from,
        )
        for record, target, call in plan:
            self._finish_retry(record, result, self._run_sync, target, call)
        return result

    async def aretry(
        self,
        ids: Sequence[int] | None = None,
        *,
        using: Callable[..., Any] | None = None,
        function: str | None = None,
        dry_run: bool = False,
        import_from: str | Path | None = None,
    ) -> RetryResult:
        """:meth:`retry`, awaitable - for records produced by ``async def`` functions."""
        result = RetryResult()
        plan = self._retry_plan(
            ids,
            using=using,
            function=function,
            result=result,
            dry_run=dry_run,
            import_from=import_from,
        )
        for record, target, call in plan:
            try:
                outcome = target(*call.args, **call.kwargs)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except NEVER_QUARANTINE:
                raise
            except BaseException as exc:  # noqa: BLE001 - a failing retry is normal
                self._retry_failed(record, exc, result)
            else:
                self._retry_recovered(record, result)
        return result

    def _retry_plan(
        self,
        ids: Sequence[int] | None,
        *,
        using: Callable[..., Any] | None,
        function: str | None,
        result: RetryResult,
        dry_run: bool,
        import_from: str | Path | None = None,
    ) -> list[tuple[Record, Callable[..., Any], Call]]:
        """Work out what can be retried, recording why anything cannot be."""
        wanted = set(ids) if ids is not None else None
        candidates = self.records(function)
        if wanted is not None:
            for missing in sorted(wanted - {r.id for r in candidates}):
                result.unretryable.append((missing, f"no record {missing} in {self.dir}"))
        plan = []
        for record in candidates:
            if wanted is not None and record.id not in wanted:
                continue
            try:
                target = using if using is not None else resolve_function(record, import_from)
                call = record.load_call()
            except (ResolutionError, StorageError) as exc:
                result.unretryable.append((record.id, str(exc)))
                continue
            if dry_run:
                result.recovered.append(record.id)
                continue
            plan.append((record, unwrap_quarantined(target), call))
        return plan

    def _run_sync(self, target: Callable[..., Any], call: Call) -> Any:
        outcome = target(*call.args, **call.kwargs)
        if not inspect.isawaitable(outcome):
            return outcome
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_await(outcome))
        closer = getattr(outcome, "close", None)  # avoid "never awaited" noise
        if callable(closer):
            closer()
        raise QuarantineError(
            "this record came from an async function and there is already an event "
            "loop running here; use `await q.aretry()` instead of `q.retry()`"
        )

    def _finish_retry(
        self,
        record: Record,
        result: RetryResult,
        runner: Callable[[Callable[..., Any], Call], Any],
        target: Callable[..., Any],
        call: Call,
    ) -> None:
        try:
            runner(target, call)
        except NEVER_QUARANTINE:
            raise
        except BaseException as exc:  # noqa: BLE001 - a still-failing retry is normal
            self._retry_failed(record, exc, result)
        else:
            self._retry_recovered(record, result)

    def _retry_failed(self, record: Record, exc: BaseException, result: RetryResult) -> None:
        record.attempts += 1
        record.error_type = type(exc).__name__
        record.error = str(exc)
        record.last_failed_at = utcnow()
        self.store.write_traceback(record, exc)
        self.store.update(record)
        result.still_failing.append(record.id)

    def _retry_recovered(self, record: Record, result: RetryResult) -> None:
        self.store.delete(record)
        with self._mutex:
            self.stats.recovered += 1
            if self._count is not None:
                self._count = max(0, self._count - 1)
            if self._known is not None:
                self._known.pop(record.fingerprint, None)
        result.recovered.append(record.id)

    # -- internals ------------------------------------------------------

    def _precheck(self, fn: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        """Return ``SKIPPED`` for a known-bad input, else a cached fingerprint or ``None``."""
        if not self.config.skip_known_bad:
            return None
        marker = fingerprint_source(_name_of(fn), Call(tuple(args), dict(kwargs)), self._redactor())
        with self._mutex:
            if self._known is None:
                self._known = self.store.fingerprints()
            hit = marker in self._known
        if hit:
            with self._mutex:
                self.stats.skipped += 1
            return SKIPPED
        return marker

    def _redactor(self) -> Redactor:
        return Redactor(self.config.redact)

    def _on_success(self, result: Any) -> Any:
        with self._mutex:
            self.stats.processed += 1
            self.stats.consecutive_failures = 0
        return result

    def _on_failure(
        self,
        fn: Callable[..., Any],
        exc: BaseException,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        marker: Any,
    ) -> Any:
        if not self._should_quarantine(exc):
            raise exc
        record = self._store_failure(fn, exc, Call(tuple(args), dict(kwargs)), marker)
        with self._mutex:
            self.stats.quarantined += 1
            self.stats.consecutive_failures += 1
            streak = self.stats.consecutive_failures
        self._notify(record)
        limit = self.config.halt_after
        if limit is not None and streak >= limit:
            raise SystemicFailure(streak, exc) from exc
        return QUARANTINED

    def _should_quarantine(self, exc: BaseException) -> bool:
        if isinstance(exc, NEVER_QUARANTINE):
            return False
        if self.config.exclude and isinstance(exc, self.config.exclude):
            return False
        return isinstance(exc, self.config.only)

    def _store_failure(
        self,
        fn: Callable[..., Any],
        exc: BaseException,
        call: Call,
        marker: Any,
    ) -> Record:
        self._check_capacity(exc)
        name = _name_of(fn)
        redactor = self._redactor()
        clean = redact_call(call, redactor)
        record = self.store.add(
            function=name,
            module=getattr(fn, "__module__", "") or "",
            fingerprint=marker
            if isinstance(marker, str)
            else fingerprint_source(name, call, redactor),
            source_file=_source_file_of(fn),
            exc=exc,
            serialized=serialize(clean),
            input_text=render_input_text(name, clean),
            preview=preview(clean),
            redacted=redactor.hits,
        )
        with self._mutex:
            if self._count is not None:
                self._count += 1
            if self._known is not None:
                self._known[record.fingerprint] = record.id
        return record

    def _check_capacity(self, exc: BaseException) -> None:
        limit = self.config.max_items
        if limit is None:
            return
        with self._mutex:
            if self._count is None:
                self._count = self.store.count()
            full = self._count >= limit
        if full:
            raise QuarantineFull(limit, str(self.dir)) from exc

    def _notify(self, record: Record) -> None:
        if self.config.verbose:
            warn(f"quarantined #{record.id:04d} {record.summary} → {record.path}")
        hook = self.config.on_quarantine
        if hook is None:
            return
        try:
            hook(record)
        except NEVER_QUARANTINE:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken alert must not kill the run
            warn(f"on_quarantine hook failed: {type(exc).__name__}: {exc}")


async def _await(awaitable: Any) -> Any:
    return await awaitable


def _source_file_of(fn: Callable[..., Any]) -> str:
    """Absolute path of the file *fn* was defined in, best effort.

    Recorded so that a function from a script - module name ``__main__``, which
    a later process cannot import - can still be found with
    ``quarantine retry --import``.
    """
    try:
        path = inspect.getsourcefile(inspect.unwrap(fn))
    except (TypeError, OSError):  # builtins, C extensions, exec'd code
        return ""
    if path:
        with contextlib.suppress(OSError):
            return str(Path(path).resolve())
    return ""


def _name_of(fn: Callable[..., Any]) -> str:
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None)
    if name:
        return str(name)
    return type(fn).__name__


def _finish_wrapper(wrapper: Any, fn: Callable[..., Any], owner: Quarantine) -> Any:
    wrapper.__wrapped__ = fn
    wrapper._quarantine_wrapper = True  # noqa: SLF001 - our own marker on our own wrapper
    wrapper.quarantine = owner
    return wrapper


def _reject_generator(fn: Callable[..., Any]) -> None:
    """Refuse to wrap generators, where the body does not run at call time.

    Wrapping one would produce a decorator that silently protects nothing,
    which is worse than an error message.
    """
    if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
        kind = "async generator" if inspect.isasyncgenfunction(fn) else "generator"
        raise TypeError(
            f"cannot wrap {_name_of(fn)}: it is a {kind} function, so its body runs during "
            f"iteration, not during the call - the decorator would catch nothing. "
            f"Wrap the consumer instead, or use quarantine.shield(items, using=...)."
        )

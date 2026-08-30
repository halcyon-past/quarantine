"""The public surface: the ``@quarantine`` decorator and its friends."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any, TypeVar, overload

from .core import DEFAULT_HALT_AFTER, DEFAULT_MAX_ITEMS, Config, Quarantine, RetryResult
from .record import Record
from .sentinels import QUARANTINED, SKIPPED
from .store import coerce_dir

__all__ = [
    "aretry",
    "ashield",
    "clear",
    "get_quarantine",
    "quarantine",
    "records",
    "reset",
    "retry",
    "shield",
    "summary",
]

F = TypeVar("F", bound=Callable[..., Any])

_registry: dict[Config, Quarantine] = {}
_registry_lock = threading.Lock()


def get_quarantine(
    dir: str | Path | None = None,  # noqa: A002 - matches the documented keyword
    *,
    only: Any = (Exception,),
    exclude: Any = (),
    halt_after: int | None = DEFAULT_HALT_AFTER,
    max_items: int | None = DEFAULT_MAX_ITEMS,
    retries: int = 0,
    backoff: float = 0.0,
    backoff_factor: float = 1.0,
    jitter: float = 0.0,
    dead_after: int | None = None,
    redact: Iterable[str] = (),
    on_quarantine: Callable[[Record], None] | None = None,
    on_retry_success: Callable[[Record], None] | None = None,
    on_retry_failure: Callable[[Record], None] | None = None,
    skip_known_bad: bool = True,
    report: bool = True,
    verbose: bool = False,
) -> Quarantine:
    """Get the shared :class:`~quarantine.core.Quarantine` for these settings.

    Instances are interned by configuration, so every ``@quarantine`` in a
    script shares one set of counters and prints one summary line.
    """
    config = Config(
        dir=coerce_dir(dir),
        only=only,
        exclude=exclude,
        halt_after=halt_after,
        max_items=max_items,
        retries=retries,
        backoff=backoff,
        backoff_factor=backoff_factor,
        jitter=jitter,
        dead_after=dead_after,
        redact=tuple(redact),
        on_quarantine=on_quarantine,
        on_retry_success=on_retry_success,
        on_retry_failure=on_retry_failure,
        skip_known_bad=skip_known_bad,
        report=report,
        verbose=verbose,
    )
    with _registry_lock:
        instance = _registry.get(config)
        if instance is None:
            instance = Quarantine(config=config)
            _registry[config] = instance
        return instance


@overload
def quarantine(fn: F, /) -> F: ...


@overload
def quarantine(
    fn: None = ...,
    /,
    *,
    dir: str | Path | None = ...,
    only: Any = ...,
    exclude: Any = ...,
    halt_after: int | None = ...,
    max_items: int | None = ...,
    retries: int = ...,
    backoff: float = ...,
    backoff_factor: float = ...,
    jitter: float = ...,
    dead_after: int | None = ...,
    redact: Iterable[str] = ...,
    on_quarantine: Callable[[Record], None] | None = ...,
    on_retry_success: Callable[[Record], None] | None = ...,
    on_retry_failure: Callable[[Record], None] | None = ...,
    skip_known_bad: bool = ...,
    report: bool = ...,
    verbose: bool = ...,
) -> Callable[[F], F]: ...


def quarantine(
    fn: Any = None,
    /,
    *,
    dir: str | Path | None = None,  # noqa: A002 - matches the documented keyword
    only: Any = (Exception,),
    exclude: Any = (),
    halt_after: int | None = DEFAULT_HALT_AFTER,
    max_items: int | None = DEFAULT_MAX_ITEMS,
    retries: int = 0,
    backoff: float = 0.0,
    backoff_factor: float = 1.0,
    jitter: float = 0.0,
    dead_after: int | None = None,
    redact: Iterable[str] = (),
    on_quarantine: Callable[[Record], None] | None = None,
    on_retry_success: Callable[[Record], None] | None = None,
    on_retry_failure: Callable[[Record], None] | None = None,
    skip_known_bad: bool = True,
    report: bool = True,
    verbose: bool = False,
) -> Any:
    """Keep the loop running when one item goes bad.

    Use it bare::

        @quarantine
        def process(item): ...

    or configured::

        @quarantine(dir=".bad", only=(ValueError,), halt_after=100)
        def process(item): ...

    A call that raises is written to the quarantine folder - input, traceback
    and metadata - and returns :data:`~quarantine.QUARANTINED` instead of
    propagating. Works on ``async def`` too.

    Args:
        fn: The function to wrap (supplied automatically when used bare).
        dir: Where the sick bay lives. Defaults to ``$QUARANTINE_DIR`` or
            ``./.quarantine``.
        only: Quarantine just these exception types; anything else still
            crashes. Defaults to ``(Exception,)``.
        exclude: Exception types to let through even if ``only`` matches.
        halt_after: Raise :class:`~quarantine.SystemicFailure` after this many
            *consecutive* failures. ``None`` disables the circuit breaker.
        max_items: Refuse to grow the folder past this many records.
        retries: Re-run a failing call this many times before quarantining it,
            for shrugging off transient failures.
        backoff: Base delay in seconds between transient retries.
        backoff_factor: Multiply the delay by this per retry (``2.0`` gives
            exponential backoff). ``1.0`` keeps the delay fixed.
        jitter: Add up to this many random seconds to each delay, so parallel
            workers do not hammer a struggling service in lockstep.
        dead_after: A record that has failed this many attempts is *dead*: a
            blanket retry skips it, and only retrying it by explicit id runs
            it again. ``None`` disables poison-item detection.
        redact: Field names (globs allowed) scrubbed before anything is saved.
        on_quarantine: Called with each new :class:`~quarantine.Record`; useful
            for alerting. Exceptions from the hook are reported, not raised.
        on_retry_success: Called with each record a retry recovers.
        on_retry_failure: Called with each record that fails a retry again.
        skip_known_bad: On a rerun, skip inputs already in quarantine instead
            of failing them again.
        report: Print the one-line summary when the process exits.
        verbose: Also print a line for every item as it is quarantined.

    Returns:
        The wrapped function, or a decorator when called with options.
    """
    instance = get_quarantine(
        dir,
        only=only,
        exclude=exclude,
        halt_after=halt_after,
        max_items=max_items,
        retries=retries,
        backoff=backoff,
        backoff_factor=backoff_factor,
        jitter=jitter,
        dead_after=dead_after,
        redact=redact,
        on_quarantine=on_quarantine,
        on_retry_success=on_retry_success,
        on_retry_failure=on_retry_failure,
        skip_known_bad=skip_known_bad,
        report=report,
        verbose=verbose,
    )
    if fn is None:
        return instance.wrap
    if not callable(fn):
        raise TypeError(
            f"@quarantine takes a function, got {type(fn).__name__}; "
            f"did you mean @quarantine(dir={fn!r})?"
        )
    return instance.wrap(fn)


def shield(
    items: Iterable[Any],
    using: Callable[..., Any] | None = None,
    **options: Any,
) -> Iterator[Any]:
    """Run *using* over *items*, yielding only the results that worked.

    The loop-shaped form of the decorator::

        for result in shield(items, using=process):
            ...

    Items that raise are quarantined and simply do not appear in the output,
    so the consumer never sees a failure sentinel. Accepts the same keyword
    options as :func:`quarantine`.
    """
    if using is None:
        raise TypeError(
            "shield() needs something to run: shield(items, using=process). "
            "A generator cannot protect the body of your for-loop, only the "
            "callable it hands work to."
        )
    instance = get_quarantine(**options)
    for item in items:
        outcome = instance.call(using, item)
        if outcome is QUARANTINED or outcome is SKIPPED:
            continue
        yield outcome


async def ashield(
    items: Any,
    using: Callable[..., Any] | None = None,
    **options: Any,
) -> AsyncIterator[Any]:
    """``async`` counterpart of :func:`shield`, over a sync or async iterable."""
    if using is None:
        raise TypeError("ashield() needs something to run: ashield(items, using=process)")
    instance = get_quarantine(**options)
    if hasattr(items, "__aiter__"):
        async for item in items:
            outcome = await instance.acall(using, item)
            if outcome is not QUARANTINED and outcome is not SKIPPED:
                yield outcome
    else:
        for item in items:
            outcome = await instance.acall(using, item)
            if outcome is not QUARANTINED and outcome is not SKIPPED:
                yield outcome


# -- module-level conveniences for the default folder --------------------


def default() -> Quarantine:
    """The instance used by a bare ``@quarantine``."""
    return get_quarantine()


def records(function: str | None = None, *, dir: str | Path | None = None) -> list[Record]:  # noqa: A002
    """Everything currently quarantined in *dir*."""
    return get_quarantine(dir).records(function)


def retry(
    ids: Sequence[int] | None = None,
    *,
    using: Callable[..., Any] | None = None,
    function: str | None = None,
    dir: str | Path | None = None,  # noqa: A002
    dry_run: bool = False,
    import_from: str | Path | None = None,
) -> RetryResult:
    """Re-run quarantined items; the ones that now succeed are dropped."""
    return get_quarantine(dir).retry(
        ids,
        using=using,
        function=function,
        dry_run=dry_run,
        import_from=import_from,
    )


async def aretry(
    ids: Sequence[int] | None = None,
    *,
    using: Callable[..., Any] | None = None,
    function: str | None = None,
    dir: str | Path | None = None,  # noqa: A002
    dry_run: bool = False,
) -> RetryResult:
    """Awaitable :func:`retry`, for records from ``async def`` functions."""
    return await get_quarantine(dir).aretry(ids, using=using, function=function, dry_run=dry_run)


def clear(*, dir: str | Path | None = None) -> int:  # noqa: A002
    """Empty the quarantine folder. Returns how many records were removed."""
    return get_quarantine(dir).clear()


def summary(*, dir: str | Path | None = None) -> str | None:  # noqa: A002
    """The end-of-run summary line for *dir*, or ``None`` if there is nothing to say."""
    return get_quarantine(dir).summary_line()


def reset() -> None:
    """Forget every interned instance (used by the test suite)."""
    with _registry_lock:
        _registry.clear()

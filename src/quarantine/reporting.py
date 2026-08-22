"""Terminal output: the end-of-run summary, warnings, and console-safety.

Everything user-facing goes through :func:`emit`, which degrades gracefully on
consoles that cannot encode ``✓`` - a ``UnicodeEncodeError`` from a *progress
message* must never be the thing that kills a 3-hour job.
"""

from __future__ import annotations

import atexit
import contextlib
import sys
import threading
import weakref
from collections.abc import Iterable
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .core import Quarantine

__all__ = [
    "ascii_fallback",
    "emit",
    "encodable",
    "print_summaries",
    "register_reporter",
    "warn",
]

ASCII_MAP = {
    "✓": "OK",  # check mark
    "✗": "FAIL",  # ballot X
    "→": "->",  # arrow
    "·": "|",  # middle dot
    "⏭": "SKIP",  # skip forward
    "↺": "RETRY",  # anticlockwise open circle arrow
    "✋": "STOP",  # raised hand
    "…": "...",  # ellipsis
    "─": "-",  # box drawing
}

_lock = threading.Lock()
_reporters: list[weakref.ReferenceType[Quarantine]] = []
_hook_installed = False


def ascii_fallback(text: str) -> str:
    """Replace the pretty glyphs with plain ASCII equivalents."""
    for glyph, plain in ASCII_MAP.items():
        text = text.replace(glyph, plain)
    return text.encode("ascii", "replace").decode("ascii")


def _writable(stream: IO[str] | None) -> IO[str] | None:
    if stream is None or getattr(stream, "closed", False):
        return None
    return stream


def encodable(text: str, stream: IO[str] | None) -> bool:
    """Whether *stream* can actually represent *text*.

    Checking up front matters: ``sys.stderr`` uses ``backslashreplace``, so a
    check mark on a cp1252 console does not raise - it silently prints the
    literal text ``\u2713``, which is worse than the ASCII fallback.
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def emit(text: str, stream: IO[str] | None = None) -> None:
    """Write one line to *stream* (stderr by default), never raising."""
    target = _writable(stream if stream is not None else sys.stderr)
    if target is None:
        return
    if not encodable(text, target):
        text = ascii_fallback(text)
    try:
        target.write(text + "\n")
    except UnicodeEncodeError:
        try:
            target.write(ascii_fallback(text) + "\n")
        except Exception:  # noqa: BLE001 - output is best-effort by design
            return
    except Exception:  # noqa: BLE001 - output is best-effort by design
        return
    with contextlib.suppress(Exception):
        target.flush()


def warn(message: str, stream: IO[str] | None = None) -> None:
    """Emit a quarantine warning line."""
    emit(f"quarantine: {message}", stream)


def register_reporter(instance: Quarantine) -> None:
    """Arrange for *instance* to print its summary when the process exits."""
    global _hook_installed  # noqa: PLW0603 - one process-wide atexit hook
    with _lock:
        _reporters.append(weakref.ref(instance))
        if not _hook_installed:
            atexit.register(print_summaries)
            _hook_installed = True


def live_reporters() -> list[Quarantine]:
    """Registered instances that are still alive, oldest first."""
    with _lock:
        alive = []
        surviving = []
        for ref in _reporters:
            instance = ref()
            if instance is not None:
                alive.append(instance)
                surviving.append(ref)
        _reporters[:] = surviving
    return alive


def print_summaries(stream: IO[str] | None = None) -> list[str]:
    """Print one summary line per active quarantine. Returns the lines printed."""
    lines = []
    for instance in live_reporters():
        line = instance.summary_line()
        if line:
            lines.append(line)
            emit(line, stream)
    return lines


def reset_reporters() -> None:
    """Forget every registered instance (used by the test suite)."""
    with _lock:
        _reporters.clear()


def columnize(
    rows: Iterable[Iterable[Any]],
    headers: Iterable[str],
    widths: Iterable[int | None] | None = None,
) -> list[str]:
    """Render a simple, aligned text table (no dependencies, no drama)."""
    header_list = [str(h) for h in headers]
    body = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    limits = list(widths) if widths is not None else [None] * len(header_list)
    limits += [None] * (len(header_list) - len(limits))

    sizes = []
    for index, header in enumerate(header_list):
        longest = max([len(header), *[len(r[index]) for r in body if index < len(r)]])
        cap = limits[index]
        sizes.append(min(longest, cap) if cap else longest)

    def render(cells: list[str]) -> str:
        out = []
        for index, cell in enumerate(cells):
            width = sizes[index]
            text = cell if len(cell) <= width else cell[: max(1, width - 1)] + "…"
            out.append(text.ljust(width) if index < len(cells) - 1 else text)
        return "  ".join(out).rstrip()

    return [render(header_list), *[render(row) for row in body]]

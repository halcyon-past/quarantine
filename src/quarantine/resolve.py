"""Find the function a record came from, so ``quarantine retry`` can replay it."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from .errors import QuarantineError

__all__ = [
    "ResolutionError",
    "load_module_from_path",
    "resolve_function",
    "unwrap_quarantined",
]

_SCRIPT_PREFIX = "_quarantine_target_"


class ResolutionError(QuarantineError):
    """The function that produced a record could not be imported back."""


def unwrap_quarantined(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Strip quarantine's own wrappers off *fn*, leaving other decorators alone.

    Retries must call the raw function: replaying through the wrapper would
    file a *second* record for an item that already has one.
    """
    seen = set()
    current = fn
    while getattr(current, "_quarantine_wrapper", False):
        if id(current) in seen:  # pragma: no cover - defensive against wrapper cycles
            break
        seen.add(id(current))
        inner = getattr(current, "__wrapped__", None)
        if inner is None or not callable(inner):  # pragma: no cover - defensive
            break
        current = inner
    return current


def load_module_from_path(path: str | os.PathLike[str]) -> ModuleType:
    """Import a ``.py`` file as a module under a private name.

    This is how ``quarantine retry --import job.py`` reaches a function that
    was defined in a script. Importing a file **runs its top level**, so keep
    the script's own work behind ``if __name__ == "__main__":``.
    """
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ResolutionError(f"{resolved} is not a file")

    name = f"{_SCRIPT_PREFIX}{resolved.stem}"
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ResolutionError(f"cannot import {resolved}: not an importable Python file")

    module = importlib.util.module_from_spec(spec)
    parent = str(resolved.parent)
    added = parent not in sys.path
    if added:
        sys.path.insert(0, parent)  # so the script's sibling imports still work
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise ResolutionError(f"cannot import {resolved}: {type(exc).__name__}: {exc}") from exc
    finally:
        if added and parent in sys.path:
            sys.path.remove(parent)
    return module


def resolve_function(
    record: Any,
    import_from: str | os.PathLike[str] | None = None,
) -> Callable[..., Any]:
    """Import and return the function named by *record*.

    Pass *import_from* to load a specific ``.py`` file instead of trusting the
    recorded module name.

    Raises :class:`ResolutionError` with an actionable message when the
    function cannot be reached - typically because it was defined inside
    another function, or in a script run directly (so its module name is
    ``__main__``, which means something else entirely in a new process).
    """
    qualname = getattr(record, "function", "") or ""
    module_name = getattr(record, "module", "") or ""
    source_file = getattr(record, "source_file", "") or ""

    if not qualname:
        raise ResolutionError("record does not name a function")
    if "<locals>" in qualname:
        raise ResolutionError(
            f"{qualname} is defined inside another function, so it cannot be imported; "
            f"retry it from Python with quarantine.retry(using=my_function)"
        )

    if import_from is not None:
        module = load_module_from_path(import_from)
    elif not module_name:
        raise ResolutionError(f"record for {qualname} does not name a module")
    else:
        module = _import_named(module_name, qualname, source_file)

    target: Any = module
    for part in qualname.split("."):
        try:
            target = getattr(target, part)
        except AttributeError:
            raise ResolutionError(
                f"{getattr(module, '__name__', module_name)} has no attribute path "
                f"{qualname!r} (was the function renamed or moved?)"
                + _script_hint(module_name, source_file)
            ) from None
    if not callable(target):
        raise ResolutionError(f"{module_name}.{qualname} is not callable")
    return cast("Callable[..., Any]", target)


def _import_named(module_name: str, qualname: str, source_file: str) -> ModuleType:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ResolutionError(
            f"cannot import {module_name}: {type(exc).__name__}: {exc}"
            + _script_hint(module_name, source_file)
        ) from exc

    # "__main__" means "whatever is running right now", which in a new process
    # is the quarantine CLI - not the script that wrote the record.
    if module_name == "__main__" and source_file:
        running = getattr(module, "__file__", None)
        if running is None or Path(running).resolve() != Path(source_file).resolve():
            raise ResolutionError(
                f"{qualname} was defined in {source_file}, which ran as a script, "
                f"so a separate process cannot import it as '__main__'."
                + _script_hint(module_name, source_file)
            )
    return module


def _script_hint(module_name: str, source_file: str) -> str:
    """The one piece of advice that actually unblocks a script author."""
    if module_name != "__main__" or not source_file:
        return ""
    return (
        f"\n  Retry it with: quarantine retry --import {source_file}\n"
        f"  (that imports the file, so keep the script's own work behind "
        f'if __name__ == "__main__":)'
    )

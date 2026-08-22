"""Scrub secret-looking fields out of an input *before* it reaches the disk.

Rules of the road:

* the caller's object is **never** mutated - everything is rebuilt as a copy;
* matching is case-insensitive and understands globs (``"*token*"``);
* containers, dataclasses and plain objects are all walked recursively;
* reference cycles and absurd nesting are handled, not crashed on.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any

__all__ = ["PLACEHOLDER", "Redactor", "compile_patterns", "redact"]

PLACEHOLDER = "***REDACTED***"
"""What a redacted value is replaced with on disk."""

MAX_DEPTH = 40
"""How deep to walk before giving up (cheap insurance against pathological data)."""


def compile_patterns(names: Iterable[str]) -> tuple[str, ...]:
    """Normalise user-supplied field names into match patterns."""
    patterns = []
    for name in names:
        if not isinstance(name, str):
            raise TypeError(f"redact fields must be strings, got {type(name).__name__}")
        cleaned = name.strip().lower()
        if cleaned:
            patterns.append(cleaned)
    return tuple(dict.fromkeys(patterns))


class Redactor:
    """Applies a set of field patterns to a value tree, producing a clean copy."""

    def __init__(self, patterns: Iterable[str], placeholder: str = PLACEHOLDER) -> None:
        self.patterns = compile_patterns(patterns)
        self.placeholder = placeholder
        self.hits: set[str] = set()
        self.truncated = False

    @property
    def active(self) -> bool:
        """Whether this redactor has anything to do at all."""
        return bool(self.patterns)

    def matches(self, key: object) -> bool:
        """Whether *key* names a field that must be scrubbed."""
        if not isinstance(key, str):
            return False
        lowered = key.lower()
        return any(fnmatchcase(lowered, pattern) for pattern in self.patterns)

    def apply(self, value: Any) -> Any:
        """Return a redacted copy of *value* (or *value* itself if nothing matches)."""
        if not self.active:
            return value
        return self._walk(value, 0, set())

    # -- internals ------------------------------------------------------

    def _walk(self, value: Any, depth: int, seen: set[int]) -> Any:  # noqa: PLR0911
        if depth > MAX_DEPTH:
            self.truncated = True
            return value
        if value is None or isinstance(value, (str, bytes, bytearray, int, float, bool, complex)):
            return value

        marker = id(value)
        if marker in seen:
            return value
        seen = seen | {marker}

        if isinstance(value, Mapping):
            return self._walk_mapping(value, depth, seen)
        if isinstance(value, (list, tuple)):
            return self._walk_sequence(value, depth, seen)
        if isinstance(value, (set, frozenset)):
            return type(value)(self._walk(item, depth + 1, seen) for item in value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._walk_object(value, depth, seen, dataclasses.fields(value))
        if hasattr(value, "__dict__") and not isinstance(value, type):
            return self._walk_object(value, depth, seen, None)
        return value

    def _walk_mapping(self, value: Mapping[Any, Any], depth: int, seen: set[int]) -> Any:
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if self.matches(key):
                self.hits.add(str(key))
                out[key] = self.placeholder
            else:
                out[key] = self._walk(item, depth + 1, seen)
        try:
            rebuilt = type(value)(out)  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001 - exotic mappings degrade to a plain dict
            return out
        return rebuilt

    def _walk_sequence(self, value: Sequence[Any], depth: int, seen: set[int]) -> Any:
        if isinstance(value, tuple) and hasattr(value, "_fields"):
            return self._walk_namedtuple(value, depth, seen)
        items = [self._walk(item, depth + 1, seen) for item in value]
        if isinstance(value, tuple):
            return tuple(items)
        try:
            rebuilt = type(value)(items)  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001
            return items
        return rebuilt

    def _walk_namedtuple(self, value: Any, depth: int, seen: set[int]) -> Any:
        """Namedtuples have field *names*, so they redact by name like a mapping.

        Worth the special case: ``DataFrame.itertuples()`` and ``csv`` helpers
        hand these out constantly.
        """
        names: tuple[str, ...] = value._fields
        items = []
        for name, item in zip(names, value, strict=True):
            if self.matches(name):
                self.hits.add(name)
                items.append(self.placeholder)
            else:
                items.append(self._walk(item, depth + 1, seen))
        try:
            return type(value)(*items)
        except Exception:  # noqa: BLE001 - odd tuple subclass: keep the values, lose the type
            return tuple(items)

    def _walk_object(
        self,
        value: Any,
        depth: int,
        seen: set[int],
        fields: tuple[dataclasses.Field[Any], ...] | None,
    ) -> Any:
        names = (
            [f.name for f in fields]
            if fields is not None
            else [n for n in vars(value) if not n.startswith("__")]
        )
        plan: dict[str, Any] = {}
        for name in names:
            try:
                current = getattr(value, name)
            except Exception:  # noqa: BLE001, S112 - a property blew up; nothing to redact
                continue
            if self.matches(name):
                self.hits.add(name)
                plan[name] = self.placeholder
            else:
                walked = self._walk(current, depth + 1, seen)
                if walked is not current:
                    plan[name] = walked
        if not plan:
            return value
        try:
            clone = copy.copy(value)
            for name, new in plan.items():
                setattr(clone, name, new)
        except Exception:  # noqa: BLE001 - frozen or uncopyable: fall back to a dict view
            view: dict[str, Any] = {"__class__": type(value).__name__}
            for name in names:
                view[name] = plan.get(name, getattr(value, name, None))
            return view
        return clone


def redact(
    value: Any,
    patterns: Iterable[str],
    placeholder: str = PLACEHOLDER,
) -> tuple[Any, set[str]]:
    """Redact *value*, returning ``(clean_copy, names_that_were_redacted)``."""
    redactor = Redactor(patterns, placeholder)
    return redactor.apply(value), redactor.hits

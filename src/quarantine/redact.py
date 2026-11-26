"""Scrub secret-looking fields out of an input *before* it reaches the disk.

Rules of the road:

* the caller's object is **never** mutated - everything is rebuilt as a copy;
* matching is case-insensitive and understands globs (``"*token*"``);
* containers, dataclasses and plain objects are all walked recursively;
* reference cycles and absurd nesting are handled, not crashed on.
"""

from __future__ import annotations

import collections
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
        return self._walk(value, 0, {}, set())

    # -- internals ------------------------------------------------------

    def _walk(  # noqa: PLR0911
        self,
        value: Any,
        depth: int,
        memo: dict[int, Any],
        active: set[int],
    ) -> Any:
        if depth > MAX_DEPTH:
            self.truncated = True
            return self.placeholder
        if value is None or isinstance(value, (str, bytes, bytearray, int, float, bool, complex)):
            return value

        marker = id(value)
        if marker in memo:
            return memo[marker]
        if marker in active:
            return "<circular>"

        if isinstance(value, Mapping):
            return self._walk_mapping(value, depth, memo, active)
        if isinstance(value, (list, tuple)):
            return self._walk_sequence(value, depth, memo, active)
        if isinstance(value, collections.deque):
            return self._walk_deque(value, depth, memo, active)
        if isinstance(value, (set, frozenset)):
            return self._walk_set(value, depth, memo, active)
        if not isinstance(value, type) and (
            dataclasses.is_dataclass(value)
            or hasattr(value, "__dict__")
            or hasattr(value, "__slots__")
        ):
            fields = (
                dataclasses.fields(value)
                if dataclasses.is_dataclass(value) and not isinstance(value, type)
                else None
            )
            return self._walk_object(value, depth, memo, active, fields)
        return value

    def _walk_deque(
        self,
        value: collections.deque[Any],
        depth: int,
        memo: dict[int, Any],
        active: set[int],
    ) -> collections.deque[Any]:
        marker = id(value)
        out_deque: collections.deque[Any] = collections.deque(maxlen=value.maxlen)
        memo[marker] = out_deque
        active.add(marker)
        try:
            for item in value:
                out_deque.append(self._walk(item, depth + 1, memo, active))
        finally:
            active.discard(marker)
        return out_deque

    def _walk_set(
        self,
        value: set[Any] | frozenset[Any],
        depth: int,
        memo: dict[int, Any],
        active: set[int],
    ) -> Any:
        marker = id(value)
        active.add(marker)
        try:
            walked_items = [self._walk(item, depth + 1, memo, active) for item in value]
        finally:
            active.discard(marker)
        try:
            rebuilt_set: Any = type(value)(walked_items)
        except TypeError:
            rebuilt_set = tuple(walked_items)
        memo[marker] = rebuilt_set
        return rebuilt_set

    def _walk_mapping(
        self,
        value: Mapping[Any, Any],
        depth: int,
        memo: dict[int, Any],
        active: set[int],
    ) -> Any:
        marker = id(value)
        out: dict[Any, Any] = {}
        memo[marker] = out
        active.add(marker)
        try:
            for key, item in value.items():
                if self.matches(key):
                    self.hits.add(str(key))
                    out[key] = self.placeholder
                else:
                    out[key] = self._walk(item, depth + 1, memo, active)
        finally:
            active.discard(marker)
        if type(value) is dict:
            return out
        try:
            rebuilt = type(value)(out)  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001 - exotic mappings degrade to a plain dict
            return out
        memo[marker] = rebuilt
        return rebuilt

    def _walk_sequence(
        self,
        value: Sequence[Any],
        depth: int,
        memo: dict[int, Any],
        active: set[int],
    ) -> Any:
        if isinstance(value, tuple) and hasattr(value, "_fields"):
            return self._walk_namedtuple(value, depth, memo, active)
        marker = id(value)
        if isinstance(value, list):
            items: list[Any] = []
            memo[marker] = items
            active.add(marker)
            try:
                for item in value:
                    items.append(self._walk(item, depth + 1, memo, active))
            finally:
                active.discard(marker)
            if type(value) is list:
                return items
            try:
                rebuilt = type(value)(items)
            except Exception:  # noqa: BLE001
                return items
            memo[marker] = rebuilt
            return rebuilt
        active.add(marker)
        try:
            items = [self._walk(item, depth + 1, memo, active) for item in value]
        finally:
            active.discard(marker)
        rebuilt_tuple = tuple(items)
        memo[marker] = rebuilt_tuple
        return rebuilt_tuple

    def _walk_namedtuple(
        self,
        value: Any,
        depth: int,
        memo: dict[int, Any],
        active: set[int],
    ) -> Any:
        """Namedtuples have field *names*, so they redact by name like a mapping.

        Worth the special case: ``DataFrame.itertuples()`` and ``csv`` helpers
        hand these out constantly.
        """
        marker = id(value)
        names: tuple[str, ...] = value._fields
        items = []
        active.add(marker)
        try:
            for name, item in zip(names, value, strict=True):
                if self.matches(name):
                    self.hits.add(name)
                    items.append(self.placeholder)
                else:
                    items.append(self._walk(item, depth + 1, memo, active))
        finally:
            active.discard(marker)
        try:
            rebuilt: Any = type(value)(*items)
        except Exception:  # noqa: BLE001 - odd tuple subclass: keep the values, lose the type
            rebuilt = tuple(items)
        memo[marker] = rebuilt
        return rebuilt

    def _object_attr_names(
        self,
        value: Any,
        fields: tuple[dataclasses.Field[Any], ...] | None,
    ) -> list[str]:
        if fields is not None:
            return [f.name for f in fields]
        seen_names: dict[str, None] = {}
        if hasattr(value, "__dict__"):
            for n in vars(value):
                if not n.startswith("__"):
                    seen_names[n] = None
        for cls in type(value).__mro__:
            slots = cls.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for s in slots:
                if s not in {"__dict__", "__weakref__"} and not s.startswith("__"):
                    seen_names[s] = None
        return list(seen_names)

    def _build_object_view(
        self,
        value: Any,
        names: list[str],
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        view: dict[str, Any] = {"__class__": type(value).__name__}
        for name in names:
            if name in plan:
                view[name] = plan[name]
            else:
                try:
                    view[name] = getattr(value, name)
                except Exception:  # noqa: BLE001
                    view[name] = None
        return view

    def _walk_object(
        self,
        value: Any,
        depth: int,
        memo: dict[int, Any],
        active: set[int],
        fields: tuple[dataclasses.Field[Any], ...] | None,
    ) -> Any:
        marker = id(value)
        names = self._object_attr_names(value, fields)
        plan: dict[str, Any] = {}
        active.add(marker)
        try:
            for name in names:
                try:
                    current = getattr(value, name)
                except Exception:  # noqa: BLE001, S112 - a property blew up; nothing to redact
                    continue
                if self.matches(name):
                    self.hits.add(name)
                    plan[name] = self.placeholder
                else:
                    walked = self._walk(current, depth + 1, memo, active)
                    if walked is not current:
                        plan[name] = walked
        finally:
            active.discard(marker)

        if not plan:
            memo[marker] = value
            return value
        try:
            clone = copy.copy(value)
            for name, new in plan.items():
                setattr(clone, name, new)
        except Exception:  # noqa: BLE001 - frozen or uncopyable: fall back to a dict view
            view = self._build_object_view(value, names, plan)
            memo[marker] = view
            return view
        memo[marker] = clone
        return clone


def redact(
    value: Any,
    patterns: Iterable[str],
    placeholder: str = PLACEHOLDER,
) -> tuple[Any, set[str]]:
    """Redact *value*, returning ``(clean_copy, names_that_were_redacted)``."""
    redactor = Redactor(patterns, placeholder)
    return redactor.apply(value), redactor.hits

"""The on-disk shape of a single quarantined item."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import StorageError
from .serialize import JSON, PICKLE, REPR, Call, deserialize

__all__ = ["META_NAME", "Record", "utcnow"]

META_NAME = "meta.json"
TRACEBACK_NAME = "traceback.txt"
INPUT_TEXT_NAME = "input.txt"
INPUT_PICKLE_NAME = "input.pkl"
INPUT_JSON_NAME = "input.json"

META_VERSION = 1

_PAYLOAD_FILENAMES = {PICKLE: INPUT_PICKLE_NAME, JSON: INPUT_JSON_NAME, REPR: None}


def utcnow() -> str:
    """Current time as a timezone-aware ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def payload_filename(fmt: str) -> str | None:
    """Name of the file holding the machine-readable input for *fmt*."""
    return _PAYLOAD_FILENAMES.get(fmt)


@dataclass
class Record:
    """One quarantined item: what failed, why, and with what input."""

    id: int
    function: str
    module: str
    fingerprint: str
    error_type: str
    error: str
    created_at: str
    last_failed_at: str
    attempts: int = 1
    source_file: str = ""
    payload_format: str = REPR
    payload_lossy: bool = False
    payload_reason: str | None = None
    redacted: list[str] = field(default_factory=list)
    preview: str = ""
    python: str = ""
    platform: str = ""
    quarantine_version: str = ""
    pid: int = 0
    meta_version: int = META_VERSION
    path: Path | None = field(default=None, compare=False)

    # -- serialization ---------------------------------------------------

    def to_meta(self) -> dict[str, Any]:
        """Dict written to ``meta.json`` (the on-disk path itself is not stored)."""
        data = asdict(self)
        data.pop("path", None)
        return data

    @classmethod
    def from_meta(cls, data: dict[str, Any], path: Path | None = None) -> Record:
        """Rebuild a record from ``meta.json`` content, ignoring unknown keys."""
        known = {f for f in cls.__dataclass_fields__ if f != "path"}
        missing = {"id", "function"} - data.keys()
        if missing:
            raise StorageError(f"{path or 'record'}: meta.json is missing {sorted(missing)}")
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("module", "")
        kwargs.setdefault("fingerprint", "")
        kwargs.setdefault("source_file", "")
        kwargs.setdefault("error_type", "Exception")
        kwargs.setdefault("error", "")
        kwargs.setdefault("created_at", "")
        kwargs.setdefault("last_failed_at", kwargs.get("created_at", ""))
        record = cls(**kwargs)
        record.path = path
        return record

    @classmethod
    def load(cls, path: Path) -> Record:
        """Read a record directory from disk."""
        meta_path = path / META_NAME
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"cannot read {meta_path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise StorageError(f"{meta_path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise StorageError(f"{meta_path} does not contain an object")
        return cls.from_meta(data, path)

    # -- convenience -----------------------------------------------------

    @property
    def qualified_name(self) -> str:
        """``module.qualname``, as used by ``retry`` to re-import the function."""
        return f"{self.module}.{self.function}" if self.module else self.function

    @property
    def summary(self) -> str:
        """``ErrorType: message`` on one line."""
        message = " ".join(self.error.split())
        return f"{self.error_type}: {message}" if message else self.error_type

    @property
    def when(self) -> str:
        """Wall-clock time of the last failure, ``HH:MM:SS``, for table output."""
        stamp = self.last_failed_at or self.created_at
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            return stamp[:8]
        return parsed.astimezone().strftime("%H:%M:%S")

    def _require_path(self) -> Path:
        if self.path is None:
            raise StorageError(f"record {self.id} is not attached to a directory")
        return self.path

    @property
    def input_path(self) -> Path | None:
        """Path of the machine-readable input, if one could be written."""
        name = payload_filename(self.payload_format)
        if name is None:
            return None
        candidate = self._require_path() / name
        return candidate if candidate.exists() else None

    def traceback_text(self) -> str:
        """The stored traceback, or a placeholder if it went missing."""
        path = self._require_path() / TRACEBACK_NAME
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return "(no traceback stored)\n"

    def input_text(self) -> str:
        """The human-readable input rendering."""
        path = self._require_path() / INPUT_TEXT_NAME
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return "(no input preview stored)\n"

    def load_call(self) -> Call:
        """Rebuild the original call.

        Raises :class:`~quarantine.errors.StorageError` when the input could
        only be stored as text (``payload_format == "repr"``), because a repr
        cannot be turned back into an object.
        """
        path = self.input_path
        if path is None:
            raise StorageError(
                f"record {self.id:04d} has no replayable input "
                f"(stored as {self.payload_format!r}); see input.txt"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read {path}: {exc}") from exc
        try:
            return deserialize(self.payload_format, data)
        except Exception as exc:
            raise StorageError(f"cannot rebuild the input from {path}: {exc}") from exc

"""The sick bay on disk.

Two properties matter more than anything else here:

**Atomicity.** A record is assembled in a hidden temp directory and then
*renamed* into place. A crash - or a ``kill -9`` - mid-save leaves behind a
``.tmp-*`` directory that nothing reads, never a half-written record.

**Self-description.** Every record directory stands on its own. ``index.json``
is only a cache: if it is missing, stale or corrupt it is rebuilt from the
record directories, so losing it costs a directory scan, not your data.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from ._version import __version__
from .errors import StorageError
from .record import (
    INPUT_TEXT_NAME,
    META_NAME,
    TRACEBACK_NAME,
    Record,
    payload_filename,
    utcnow,
)
from .serialize import Serialized

__all__ = ["FileLock", "Store", "default_dir"]

INDEX_NAME = "index.json"
LOCK_NAME = ".index.lock"
TMP_PREFIX = ".tmp-"
INDEX_SCHEMA = 1

COMMIT_ATTEMPTS = 6
COMMIT_DELAY = 0.01

_INDEX_FIELDS = (
    "id",
    "fingerprint",
    "function",
    "module",
    "error_type",
    "error",
    "created_at",
    "last_failed_at",
    "attempts",
    "preview",
)


class FileLock:
    """A cooperative, cross-platform lock serialising ``index.json`` writes.

    Implemented with ``O_CREAT | O_EXCL``, which is atomic on every platform we
    support. A lock older than *stale_after* is assumed to belong to a process
    that died and is broken open - the alternative is a folder that stays
    wedged forever after a single crash.
    """

    def __init__(self, path: Path, timeout: float = 10.0, stale_after: float = 60.0) -> None:
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock, waiting up to ``timeout`` seconds."""
        deadline = time.monotonic() + self.timeout
        delay = 0.001
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except (FileExistsError, PermissionError):
                if self._break_if_stale() or time.monotonic() < deadline:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.05)
                    continue
                raise StorageError(
                    f"timed out after {self.timeout}s waiting for {self.path}; "
                    f"delete it if no other process is using this folder"
                ) from None
            except OSError as exc:
                raise StorageError(f"cannot create lock {self.path}: {exc}") from exc
            else:
                with contextlib.suppress(OSError):
                    os.write(self._fd, f"{os.getpid()}\n".encode())
                return

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True  # vanished underneath us - retry immediately
        if age > self.stale_after:
            with contextlib.suppress(OSError):
                self.path.unlink()
            return True
        return False

    def release(self) -> None:
        """Release the lock, tolerating a lock that was already broken open."""
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(OSError):
            self.path.unlink()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _fsync_file(path: Path) -> None:
    with contextlib.suppress(OSError), path.open("rb") as handle:
        os.fsync(handle.fileno())


def _retry_transient(operation: Callable[[], None]) -> None:
    """Run *operation*, retrying while it fails with a transient lock.

    On Windows an antivirus scanner, a file indexer or a sync client can hold a
    brand-new file open for a few milliseconds, which surfaces as
    ``PermissionError``. That is transient, so back off and try again rather
    than losing a record over it. Anything else propagates untouched.
    """
    delay = COMMIT_DELAY
    for attempt in range(COMMIT_ATTEMPTS):
        try:
            operation()
        except PermissionError:
            if attempt == COMMIT_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2
        else:
            return


def _commit_dir(source: Path, target: Path) -> bool:
    """Move a staged record directory into place; the rename is the commit.

    Returns ``False`` if *target* already exists, which means another writer
    took that id - not that anything went wrong.
    """
    try:
        # Deliberately os.rename, not Path.rename: this must fail, rather than
        # overwrite, when another writer already claimed the id.
        _retry_transient(lambda: os.rename(source, target))  # noqa: PTH104
    except FileExistsError:
        return False
    except OSError:
        if target.exists():
            return False
        raise
    return True


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace *path* with *data*, atomically."""
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=TMP_PREFIX, suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            with contextlib.suppress(OSError):
                os.fsync(stream.fileno())
        # os.replace overwrites atomically; Path has no equivalent that does.
        _retry_transient(lambda: os.replace(tmp_name, path))  # noqa: PTH105
    except OSError as exc:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise StorageError(f"cannot write {path}: {exc}") from exc


class Store:
    """Read/write access to one ``.quarantine`` directory."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.dir = Path(directory)
        self._lock = threading.Lock()
        self._id_hint = 0
        self.problems: list[str] = []

    def __repr__(self) -> str:
        return f"Store({str(self.dir)!r})"

    # -- layout ---------------------------------------------------------

    @property
    def index_path(self) -> Path:
        """Path of the (rebuildable) index cache."""
        return self.dir / INDEX_NAME

    def exists(self) -> bool:
        """Whether the quarantine folder is present."""
        return self.dir.is_dir()

    def ensure(self) -> None:
        """Create the quarantine folder if it does not exist yet."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"cannot create {self.dir}: {exc}") from exc

    def record_dirs(self) -> list[Path]:
        """Every record directory, ordered by id."""
        if not self.exists():
            return []
        try:
            entries = list(os.scandir(self.dir))
        except OSError as exc:
            raise StorageError(f"cannot list {self.dir}: {exc}") from exc
        found = [Path(entry.path) for entry in entries if entry.is_dir() and entry.name.isdigit()]
        return sorted(found, key=lambda p: int(p.name))

    def records(self) -> list[Record]:
        """Load every readable record. Unreadable ones are reported, not fatal."""
        self.problems = []
        out = []
        for path in self.record_dirs():
            try:
                out.append(Record.load(path))
            except StorageError as exc:
                self.problems.append(str(exc))
        return out

    def get(self, record_id: int) -> Record:
        """Load a single record by id."""
        path = self.dir / self._dirname(record_id)
        if not path.is_dir():
            raise StorageError(f"no record {record_id} in {self.dir}")
        return Record.load(path)

    def count(self) -> int:
        """How many records are on disk."""
        return len(self.record_dirs())

    @staticmethod
    def _dirname(record_id: int) -> str:
        return f"{record_id:04d}"

    def _next_id(self) -> int:
        highest = self._id_hint
        for path in self.record_dirs():
            highest = max(highest, int(path.name))
        return highest + 1

    # -- writing --------------------------------------------------------

    def add(
        self,
        *,
        function: str,
        module: str,
        fingerprint: str,
        source_file: str,
        exc: BaseException,
        serialized: Serialized,
        input_text: str,
        preview: str,
        redacted: Iterable[str] = (),
    ) -> Record:
        """Write one new record, atomically, and return it."""
        self.ensure()
        now = utcnow()
        record = Record(
            id=0,
            function=function,
            module=module,
            fingerprint=fingerprint,
            source_file=source_file,
            error_type=type(exc).__name__,
            error=str(exc),
            created_at=now,
            last_failed_at=now,
            attempts=1,
            payload_format=serialized.format,
            payload_lossy=serialized.lossy,
            payload_reason=serialized.reason,
            redacted=sorted(redacted),
            preview=preview,
            python=platform.python_version(),
            platform=f"{platform.system()} {platform.release()}".strip(),
            quarantine_version=__version__,
            pid=os.getpid(),
        )
        tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        staging = Path(tempfile.mkdtemp(dir=str(self.dir), prefix=TMP_PREFIX))
        try:
            (staging / TRACEBACK_NAME).write_text(tb_text, encoding="utf-8")
            (staging / INPUT_TEXT_NAME).write_text(input_text, encoding="utf-8")
            name = payload_filename(serialized.format)
            if name is not None and serialized.data is not None:
                (staging / name).write_bytes(serialized.data)
            final = self._promote(staging, record)
        except OSError as io_error:
            shutil.rmtree(staging, ignore_errors=True)
            raise StorageError(f"cannot write a record into {self.dir}: {io_error}") from io_error
        record.path = final
        self._index_upsert(record)
        return record

    def _promote(self, staging: Path, record: Record) -> Path:
        """Name the record, finish its metadata, and rename it into place."""
        with self._lock:
            for _ in range(64):
                record_id = self._next_id()
                record.id = record_id
                _write_atomic(staging / META_NAME, _encode_meta(record))
                for child in sorted(staging.iterdir()):
                    _fsync_file(child)
                target = self.dir / self._dirname(record_id)
                committed = _commit_dir(staging, target)
                self._id_hint = record_id
                if committed:
                    return target
        raise StorageError(f"could not allocate a record id in {self.dir} after 64 attempts")

    def update(self, record: Record) -> None:
        """Rewrite ``meta.json`` for an existing record."""
        path = record.path or (self.dir / self._dirname(record.id))
        if not path.is_dir():
            raise StorageError(f"no record {record.id} in {self.dir}")
        record.path = path
        _write_atomic(path / META_NAME, _encode_meta(record))
        self._index_upsert(record)

    def write_traceback(self, record: Record, exc: BaseException) -> None:
        """Replace a record's stored traceback (used when a retry fails again)."""
        path = record.path or (self.dir / self._dirname(record.id))
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _write_atomic(path / TRACEBACK_NAME, text.encode("utf-8"))

    def delete(self, record: Record | int) -> None:
        """Remove one record from disk."""
        record_id = record if isinstance(record, int) else record.id
        path = self.dir / self._dirname(record_id)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        self._index_remove(record_id)

    def clear(self) -> int:
        """Delete every record (and the index). Returns how many were removed."""
        removed = 0
        for path in self.record_dirs():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        with contextlib.suppress(OSError):
            self.index_path.unlink()
        self._id_hint = 0
        return removed

    def purge_temp(self) -> int:
        """Delete leftover ``.tmp-*`` staging entries from crashed writes."""
        if not self.exists():
            return 0
        removed = 0
        for entry in os.scandir(self.dir):
            if not entry.name.startswith(TMP_PREFIX):
                continue
            if entry.is_dir():
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    Path(entry.path).unlink()
            removed += 1
        return removed

    # -- index ----------------------------------------------------------

    def _lockfile(self) -> FileLock:
        return FileLock(self.dir / LOCK_NAME)

    def read_index(self) -> dict[str, Any] | None:
        """Read ``index.json``, or ``None`` if it is absent or unusable."""
        try:
            raw = self.index_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            return None
        return data

    def _entry(self, record: Record) -> dict[str, Any]:
        meta = record.to_meta()
        return {key: meta.get(key) for key in _INDEX_FIELDS}

    def _index_upsert(self, record: Record) -> None:
        entry = self._entry(record)
        with self._lockfile():
            data = self.read_index() or {"schema": INDEX_SCHEMA, "records": []}
            rows = [r for r in data["records"] if isinstance(r, dict) and r.get("id") != record.id]
            rows.append(entry)
            rows.sort(key=lambda r: r.get("id") or 0)
            self._write_index(rows)

    def _index_remove(self, record_id: int) -> None:
        with self._lockfile():
            data = self.read_index()
            if data is None:
                return
            rows = [r for r in data["records"] if isinstance(r, dict) and r.get("id") != record_id]
            self._write_index(rows)

    def _write_index(self, rows: list[dict[str, Any]]) -> None:
        payload = {
            "schema": INDEX_SCHEMA,
            "updated": utcnow(),
            "count": len(rows),
            "records": rows,
        }
        _write_atomic(self.index_path, json.dumps(payload, indent=2, default=str).encode("utf-8"))

    def rebuild_index(self) -> list[dict[str, Any]]:
        """Regenerate ``index.json`` from the record directories."""
        rows = [self._entry(record) for record in self.records()]
        if self.exists():
            with self._lockfile():
                self._write_index(rows)
        return rows

    def index_rows(self) -> list[dict[str, Any]]:
        """Index rows, rebuilding first if the cache disagrees with the disk."""
        on_disk = {int(path.name) for path in self.record_dirs()}
        data = self.read_index()
        if data is not None:
            rows = [r for r in data["records"] if isinstance(r, dict)]
            if {r.get("id") for r in rows} == on_disk:
                return rows
        if not on_disk:
            return []
        return self.rebuild_index()

    def fingerprints(self) -> dict[str, int]:
        """Map of ``fingerprint -> record id`` for everything currently quarantined."""
        out: dict[str, int] = {}
        for row in self.index_rows():
            marker = row.get("fingerprint")
            record_id = row.get("id")
            if isinstance(marker, str) and marker and isinstance(record_id, int):
                out.setdefault(marker, record_id)
        return out

    def iter_records(self) -> Iterator[Record]:
        """Iterate records lazily."""
        yield from self.records()


def _encode_meta(record: Record) -> bytes:
    return json.dumps(record.to_meta(), indent=2, default=str).encode("utf-8")


def default_dir() -> Path:
    """The default quarantine folder: ``$QUARANTINE_DIR`` or ``./.quarantine``."""
    return Path(os.environ.get("QUARANTINE_DIR") or ".quarantine")


def python_banner() -> str:
    """Short interpreter description, handy in CLI output."""
    return f"Python {platform.python_version()} ({sys.platform})"

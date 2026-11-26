"""The Databricks Unity Catalog Volumes storage backend.

The record layout mirrors the local folder and the cloud object-store backends
exactly — per-record files under ``/Volumes/catalog/schema/volume/prefix/0001/...``:

**Id allocation.** An id is claimed by uploading a zero-byte ``.claim`` file
with ``overwrite=False``; on conflict another worker claimed that id first, so
this writer takes the next number.

**The commit point.** ``meta.json`` is uploaded *last*, and readers ignore any
record directory that lacks it — so a reader can never observe a half-written
record, and a crash mid-upload leaves invisible debris that ``quarantine
reindex`` sweeps.

Reads are materialised into a per-path cache directory under the system temp
folder, so :class:`~quarantine.record.Record` objects behave exactly as they
do locally — ``quarantine show``, ``debug``, ``retry`` and the dashboard all
work unchanged.

Requires ``databricks-sdk``: ``pip install "quarantine-py[databricks]"``.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import posixpath
import shutil
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import StorageError
from .record import META_NAME, TRACEBACK_NAME, Record
from .serialize import Serialized
from .store import StorageBackend, build_record

__all__ = ["DatabricksStore", "DatabricksVolumeStore"]

CLAIM_NAME = ".claim"
MAX_ID_ATTEMPTS = 64
VOLUMES_PREFIX = "/Volumes/"
_MIN_VOLUME_PARTS = 3

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


def _import_databricks() -> tuple[
    type[Any],
    tuple[type[Exception], ...],
    tuple[type[Exception], ...],
    type[Exception],
]:
    try:
        import databricks.sdk.errors as db_errors  # noqa: PLC0415
        from databricks.sdk import WorkspaceClient  # noqa: PLC0415
        from databricks.sdk.errors import (  # noqa: PLC0415
            AlreadyExists,
            NotFound,
            ResourceAlreadyExists,
            ResourceConflict,
            ResourceDoesNotExist,
        )

        databricks_error: type[Exception] = getattr(db_errors, "DatabricksError")  # noqa: B009
    except ImportError as exc:
        raise StorageError(
            "the Databricks Volumes backend needs databricks-sdk, which is an optional extra: "
            'pip install "quarantine-py[databricks]"'
        ) from exc
    conflict_errors = (ResourceAlreadyExists, AlreadyExists, ResourceConflict)
    not_found_errors = (NotFound, ResourceDoesNotExist)
    return WorkspaceClient, conflict_errors, not_found_errors, databricks_error


def _parse_volume_path(path: str | os.PathLike[str]) -> tuple[str, str, str, str]:
    """Validate and normalize ``/Volumes/<catalog>/<schema>/<volume>[/subpath]``."""
    raw = Path(path).as_posix() if isinstance(path, os.PathLike) else str(path)
    normalized = posixpath.normpath(raw.replace("\\", "/"))
    if not normalized.startswith(VOLUMES_PREFIX):
        raise StorageError(f"not a Unity Catalog volume path ({VOLUMES_PREFIX}...): {raw!r}")
    parts = [part for part in normalized[len(VOLUMES_PREFIX) :].split("/") if part]
    if len(parts) < _MIN_VOLUME_PARTS:
        raise StorageError(
            f"{raw!r} must include catalog, schema, and volume "
            "(/Volumes/catalog/schema/volume[/prefix])"
        )
    catalog, schema, volume = parts[0], parts[1], parts[2]
    return normalized, catalog, schema, volume


class DatabricksVolumeStore(StorageBackend):
    """A quarantine stored under a Databricks Unity Catalog Volume path."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        client: Any | None = None,
    ) -> None:
        normalized, catalog, schema, volume = _parse_volume_path(path)
        workspace_client, conflict_errors, not_found_errors, databricks_error = _import_databricks()
        self.dir: str = normalized
        self.catalog: str = catalog
        self.schema: str = schema
        self.volume: str = volume
        self.problems: list[str] = []
        self._conflict_errors = conflict_errors
        self._not_found_errors = not_found_errors
        self._client_error = databricks_error
        try:
            self._client = client if client is not None else workspace_client()
        except databricks_error as exc:
            raise self._wrap("connect", exc) from exc
        self._files = self._client.files
        self._mutex = threading.Lock()
        self._id_hint = 0
        digest = hashlib.sha256(self.dir.encode("utf-8")).hexdigest()[:12]
        self._cache = Path(tempfile.gettempdir()) / f"quarantine-databricks-{digest}"

    def __repr__(self) -> str:
        return f"DatabricksVolumeStore({self.dir!r})"

    # -- paths ------------------------------------------------------------

    def _record_dir(self, record_id: int) -> str:
        return f"{self.dir}/{record_id:04d}"

    def _file_path(self, record_id: int, name: str) -> str:
        return f"{self._record_dir(record_id)}/{name}"

    def _wrap(self, action: str, exc: Exception) -> StorageError:
        return StorageError(f"cannot {action} in {self.dir}: {exc}")

    # -- listing ----------------------------------------------------------

    def _list_objects(self) -> dict[int, dict[str, int]]:
        """Map of ``id -> {filename: size}`` for every record directory under ``self.dir``."""
        out: dict[int, dict[str, int]] = {}
        try:
            top_entries = list(self._files.list_directory_contents(self.dir))
        except self._not_found_errors:
            return {}
        except self._client_error as exc:
            raise self._wrap("list records", exc) from exc

        for entry in top_entries:
            name = getattr(entry, "name", None) or posixpath.basename(
                getattr(entry, "path", "") or ""
            )
            if not getattr(entry, "is_directory", False) or not name.isdigit():
                continue
            rid = int(name)
            rec_dir = self._record_dir(rid)
            try:
                children = list(self._files.list_directory_contents(rec_dir))
            except self._not_found_errors:
                continue
            except self._client_error as exc:
                raise self._wrap("list records", exc) from exc
            files: dict[str, int] = {}
            for child in children:
                if getattr(child, "is_directory", False):
                    continue
                fname = getattr(child, "name", None) or posixpath.basename(
                    getattr(child, "path", "") or ""
                )
                if fname:
                    files[fname] = int(getattr(child, "file_size", 0) or 0)
            if files:
                out[rid] = files
        return out

    def _committed_ids(self) -> list[int]:
        """Ids whose ``meta.json`` exists — the only records that officially exist."""
        return sorted(rid for rid, files in self._list_objects().items() if META_NAME in files)

    # -- reading ----------------------------------------------------------

    def exists(self) -> bool:
        """Whether anything is stored under the volume directory."""
        return bool(self._list_objects())

    def ensure(self) -> None:
        """Create the quarantine directory in the volume (idempotent)."""
        try:
            self._files.create_directory(self.dir)
        except self._client_error as exc:
            raise self._wrap("create directory", exc) from exc

    def count(self) -> int:
        """How many committed records the directory holds."""
        return len(self._committed_ids())

    def _safe_target_dir(self, record_id: int) -> Path:
        """Create and validate the cache directory for record_id, rejecting symlinks."""
        if self._cache.is_symlink():
            raise StorageError(f"cache directory is an unsafe symlink: {self._cache}")
        self._cache.mkdir(parents=True, exist_ok=True)
        if self._cache.is_symlink():
            raise StorageError(f"cache directory is an unsafe symlink: {self._cache}")

        target = self._cache / f"{record_id:04d}"
        if target.is_symlink():
            raise StorageError(f"cache record directory is an unsafe symlink: {target}")
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise StorageError(f"cache record directory is an unsafe symlink: {target}")

        target_resolved = target.resolve()
        cache_resolved = self._cache.resolve()
        if not target_resolved.is_relative_to(cache_resolved) or target_resolved == cache_resolved:
            raise StorageError(f"cache record directory escapes cache: {target}")
        return target_resolved

    def _write_cache(
        self, record_id: int, files: dict[str, bytes], *, replace: bool = False
    ) -> Path:
        with self._mutex:
            if replace:
                stale = self._cache / f"{record_id:04d}"
                if stale.is_symlink():
                    raise StorageError(f"cache record directory is an unsafe symlink: {stale}")
                shutil.rmtree(stale, ignore_errors=True)
            target = self._safe_target_dir(record_id)
            for name, data in files.items():
                if not _is_safe_filename(name):
                    continue
                dest = target / name
                if dest.is_symlink():
                    raise StorageError(f"cache destination cannot be a symlink: {dest}")
                if not dest.resolve().is_relative_to(target):
                    continue
                _safe_write_cache_file(target, name, data)
            return target

    def _download_bytes(self, file_path: str, record_id: int) -> bytes:
        try:
            response = self._files.download(file_path)
            stream = getattr(response, "contents", None)
            if stream is None:
                raise StorageError(f"empty download stream for {file_path}")
            with contextlib.closing(stream):
                data = stream.read()
        except self._client_error as exc:
            raise self._wrap(f"download record {record_id}", exc) from exc
        return data if isinstance(data, bytes) else bytes(data)

    def _materialise(self, record_id: int, filenames: Iterable[str]) -> Path:
        """Download one record's files into the local cache, returning its directory."""
        downloaded: dict[str, bytes] = {}
        for name in filenames:
            if name == CLAIM_NAME or not _is_safe_filename(name):
                continue
            downloaded[name] = self._download_bytes(self._file_path(record_id, name), record_id)
        return self._write_cache(record_id, downloaded, replace=True)

    def get(self, record_id: int) -> Record:
        """Fetch one record into the cache and load it."""
        files = self._list_objects().get(record_id)
        if not files or META_NAME not in files:
            raise StorageError(f"no record {record_id} in {self.dir}")
        return Record.load(self._materialise(record_id, files))

    def records(self) -> list[Record]:
        """Load every committed record; unreadable ones are reported, not fatal."""
        self.problems = []
        out = []
        listing = self._list_objects()
        for record_id in sorted(listing):
            if META_NAME not in listing[record_id]:
                continue
            try:
                out.append(Record.load(self._materialise(record_id, listing[record_id])))
            except StorageError as exc:
                self.problems.append(str(exc))
        return out

    # -- writing ----------------------------------------------------------

    def _put(self, file_path: str, data: bytes, *, overwrite: bool = True) -> None:
        self._files.upload(file_path, io.BytesIO(data), overwrite=overwrite)

    def _claim_id(self) -> int:
        """Claim the next free id with a conditional write; the loser moves on."""
        taken = self._list_objects()
        candidate = max([self._id_hint, *taken], default=0) + 1
        for _ in range(MAX_ID_ATTEMPTS):
            try:
                self._files.create_directory(self._record_dir(candidate))
                self._put(self._file_path(candidate, CLAIM_NAME), b"", overwrite=False)
            except self._conflict_errors:
                candidate += 1
                continue
            except self._client_error as exc:
                raise self._wrap("claim a record id", exc) from exc
            else:
                self._id_hint = candidate
                return candidate
        raise StorageError(
            f"could not allocate a record id in {self.dir} after {MAX_ID_ATTEMPTS} attempts"
        )

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
        """Write one new record; ``meta.json`` goes last and is the commit."""
        record, files = build_record(
            function=function,
            module=module,
            fingerprint=fingerprint,
            source_file=source_file,
            exc=exc,
            serialized=serialized,
            input_text=input_text,
            preview=preview,
            redacted=redacted,
        )
        self.ensure()
        with self._mutex:
            record.id = self._claim_id()
        meta_bytes = _encode_meta(record)
        try:
            for name, data in files.items():
                self._put(self._file_path(record.id, name), data, overwrite=True)
            self._put(self._file_path(record.id, META_NAME), meta_bytes, overwrite=True)
        except self._client_error as io_error:
            raise self._wrap("write a record", io_error) from io_error
        payload = {**files, META_NAME: meta_bytes}
        record.path = self._write_cache(record.id, payload, replace=True)
        return record

    def update(self, record: Record) -> None:
        """Rewrite ``meta.json`` for an existing record."""
        meta_bytes = _encode_meta(record)
        try:
            self._put(self._file_path(record.id, META_NAME), meta_bytes, overwrite=True)
        except self._client_error as exc:
            raise self._wrap(f"update record {record.id}", exc) from exc
        cached = self._write_cache(record.id, {META_NAME: meta_bytes})
        if record.path is None:
            record.path = cached

    def write_traceback(self, record: Record, exc: BaseException) -> None:
        """Replace a record's stored traceback (used when a retry fails again)."""
        import traceback as tb  # noqa: PLC0415 - keep the module namespace tidy

        text = "".join(tb.format_exception(type(exc), exc, exc.__traceback__)).encode("utf-8")
        try:
            self._put(self._file_path(record.id, TRACEBACK_NAME), text, overwrite=True)
        except self._client_error as io_error:
            raise self._wrap(f"update record {record.id}", io_error) from io_error
        self._write_cache(record.id, {TRACEBACK_NAME: text})

    # -- deleting ---------------------------------------------------------

    def _delete_record_dir(self, record_id: int, filenames: Iterable[str]) -> None:
        for name in filenames:
            try:
                self._files.delete(self._file_path(record_id, name))
            except self._not_found_errors:
                pass
            except self._client_error as exc:
                raise self._wrap(f"delete record {record_id}", exc) from exc
        try:
            self._files.delete_directory(self._record_dir(record_id))
        except self._not_found_errors:
            pass
        except self._client_error as exc:
            raise self._wrap(f"delete record {record_id}", exc) from exc

    def delete(self, record: Record | int) -> None:
        """Remove one record's files (and its cache entry)."""
        record_id = record if isinstance(record, int) else record.id
        files = self._list_objects().get(record_id, {})
        if files:
            self._delete_record_dir(record_id, files)
        with self._mutex:
            shutil.rmtree(self._cache / f"{record_id:04d}", ignore_errors=True)

    def clear(self) -> int:
        """Delete every record under the volume path. Returns how many were committed."""
        listing = self._list_objects()
        removed = sum(1 for files in listing.values() if META_NAME in files)
        for record_id, files in listing.items():
            self._delete_record_dir(record_id, files)
        with self._mutex:
            shutil.rmtree(self._cache, ignore_errors=True)
            self._id_hint = 0
        return removed

    def purge_temp(self) -> int:
        """Sweep claims and partial uploads that never got their ``meta.json``."""
        listing = self._list_objects()
        swept = 0
        for record_id, files in listing.items():
            if META_NAME in files:
                continue
            self._delete_record_dir(record_id, files)
            swept += 1
        return swept

    # -- the index --------------------------------------------------------

    def rebuild_index(self) -> list[dict[str, Any]]:
        """Unity Catalog Volumes keep no index file: the listing is live. Returns the rows."""
        rows = []
        for record in self.records():
            meta = record.to_meta()
            rows.append({key: meta.get(key) for key in _INDEX_FIELDS})
        return rows

    def fingerprints(self) -> dict[str, int]:
        """Map of ``fingerprint -> record id``, read from the live records."""
        out: dict[str, int] = {}
        for record in self.records():
            if record.fingerprint:
                out.setdefault(record.fingerprint, record.id)
        return out

    def disk_bytes(self) -> int:
        """Total size of every file under the volume path."""
        return sum(size for files in self._list_objects().values() for size in files.values())


DatabricksStore = DatabricksVolumeStore


def _encode_meta(record: Record) -> bytes:
    return json.dumps(record.to_meta(), indent=2, default=str).encode("utf-8")


def _is_safe_filename(name: str) -> bool:
    """Return whether ``name`` is a single safe basename with no path traversal."""
    return (
        bool(name)
        and "\x00" not in name
        and "/" not in name
        and "\\" not in name
        and name not in {".", ".."}
        and Path(name).name == name
    )


def _safe_write_cache_file(target: Path, name: str, data: bytes) -> None:
    """Write data to target/name safely using dir_fd/openat without following symlinks."""
    dest = target / name
    if dest.is_symlink():
        raise StorageError(f"cache destination cannot be a symlink: {dest}")

    file_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW

    if os.open in getattr(os, "supports_dir_fd", set()):
        dir_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            dir_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            dir_flags |= os.O_NOFOLLOW
        try:
            target_fd = os.open(target, dir_flags)
        except OSError as exc:
            raise StorageError(f"cannot safely open cache directory {target}: {exc}") from exc
        try:
            file_fd = os.open(name, file_flags, 0o600, dir_fd=target_fd)
        except OSError as exc:
            raise StorageError(f"cannot safely write {dest}: {exc}") from exc
        finally:
            os.close(target_fd)
    else:
        if not dest.resolve().is_relative_to(target):
            raise StorageError(f"cache destination escapes target: {dest}")
        try:
            file_fd = os.open(dest, file_flags, 0o600)
        except OSError as exc:
            raise StorageError(f"cannot safely write {dest}: {exc}") from exc

    try:
        with os.fdopen(file_fd, "wb") as f:
            f.write(data)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(file_fd)
        raise

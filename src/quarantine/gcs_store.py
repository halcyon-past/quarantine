"""The GCS storage backend: one shared quarantine for a fleet of workers.

The record layout mirrors the local folder and the S3 backend exactly -
per-record objects under ``gs://bucket/prefix/0001/...`` - but GCS's
conditional-write primitive differs slightly from S3's, so it is adapted
here (see ADR 0007, and ``s3_store.py`` for the sibling implementation):

**Id allocation.** The local store claims an id by creating a directory,
which is atomic; the S3 backend claims one with ``If-None-Match: *``. Here
an id is claimed by uploading a zero-byte ``.claim`` object with
``if_generation_match=0`` - GCS's "only write me if no live version of this
object exists yet" precondition - so two workers can never both own an id;
the loser gets a ``PreconditionFailed`` (412) and takes the next number.

**The commit point.** ``meta.json`` is uploaded *last*, and readers ignore
any record prefix that lacks it - so a reader can never observe a
half-written record, and a crash mid-upload leaves invisible debris that
``quarantine reindex`` sweeps.

Reads are materialised into a per-URL cache directory under the system temp
folder, so :class:`~quarantine.record.Record` objects behave exactly as they
do locally - ``quarantine show``, ``debug``, ``retry`` and the dashboard all
work unchanged against a bucket.

Requires ``google-cloud-storage``: ``pip install "quarantine-py[gcs]"``.
Credentials and project come from the standard Google auth chain
(``GOOGLE_APPLICATION_CREDENTIALS``, gcloud ADC, or a service account
attached to the runtime); the IAM permissions needed are documented in
``docs/remote-storage.md``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import StorageError
from .record import META_NAME, TRACEBACK_NAME, Record
from .serialize import Serialized
from .store import StorageBackend, build_record

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import ModuleType

__all__ = ["GCSStore"]

CLAIM_NAME = ".claim"
MAX_ID_ATTEMPTS = 64

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


def _import_gcs() -> tuple[ModuleType, type[Exception], type[Exception], type[Exception]]:
    try:
        from google.api_core.exceptions import (  # noqa: PLC0415
            GoogleAPICallError,
            NotFound,
            PreconditionFailed,
        )
        from google.cloud import storage  # noqa: PLC0415 - deferred, optional extra
    except ImportError as exc:
        raise StorageError(
            "the gcs:// backend needs google-cloud-storage, which is an optional extra: "
            'pip install "quarantine-py[gcs]"'
        ) from exc
    return storage, PreconditionFailed, NotFound, GoogleAPICallError


class GCSStore(StorageBackend):
    """A quarantine stored as per-record objects under a GCS prefix."""

    def __init__(self, url: str) -> None:
        if not url.startswith("gs://"):
            raise StorageError(f"not a gs:// URL: {url!r}")
        rest = url[len("gs://") :]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise StorageError(f"{url!r} is missing a bucket name (gs://bucket/prefix)")
        storage, precondition_failed, not_found, api_error = _import_gcs()
        self.dir: str = url.rstrip("/")
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self.problems: list[str] = []
        try:
            self._client = storage.Client()
        except Exception as exc:
            raise StorageError(
                f"cannot authenticate to Google Cloud Storage for {url}: {exc}. "
                "Set GOOGLE_APPLICATION_CREDENTIALS or run "
                "`gcloud auth application-default login`."
            ) from exc
        self._bucket = self._client.bucket(bucket)
        self._precondition_failed = precondition_failed
        self._not_found = not_found
        self._api_error = api_error
        self._mutex = threading.Lock()
        self._id_hint = 0
        digest = hashlib.sha256(self.dir.encode("utf-8")).hexdigest()[:12]
        self._cache = Path(tempfile.gettempdir()) / f"quarantine-gcs-{digest}"

    def __repr__(self) -> str:
        return f"GCSStore({self.dir!r})"

    # -- keys -------------------------------------------------------------

    def _key(self, record_id: int, name: str) -> str:
        base = f"{record_id:04d}/{name}"
        return f"{self.prefix}/{base}" if self.prefix else base

    def _list_prefix(self) -> str:
        return f"{self.prefix}/" if self.prefix else ""

    def _wrap(self, action: str, exc: Exception) -> StorageError:
        return StorageError(f"cannot {action} in {self.dir}: {exc}")

    # -- listing ----------------------------------------------------------

    def _list_objects(self) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = {}
        try:
            blobs = self._client.list_blobs(self._bucket, prefix=self._list_prefix())
            for blob in blobs:
                tail = blob.name[len(self._list_prefix()) :]
                dirname, _, filename = tail.partition("/")
                if dirname.isdigit() and filename:
                    out.setdefault(int(dirname), {})[filename] = blob.size or 0
        except self._api_error as exc:
            raise self._wrap("list records", exc) from exc
        return out

    def _committed_ids(self) -> list[int]:
        """Ids whose ``meta.json`` exists - the only records that officially exist."""
        return sorted(rid for rid, files in self._list_objects().items() if META_NAME in files)

    # -- reading ----------------------------------------------------------

    def exists(self) -> bool:
        """Whether anything is stored under the prefix."""
        return bool(self._list_objects())

    def ensure(self) -> None:
        """Nothing to create: the bucket must already exist, prefixes are implicit."""

    def count(self) -> int:
        """How many committed records the prefix holds."""
        return len(self._committed_ids())

    def _materialise(self, record_id: int, filenames: Iterable[str]) -> Path:
        """Download one record's files into the local cache, returning its directory."""
        target = self._cache / f"{record_id:04d}"
        target.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if name == CLAIM_NAME:
                continue
            try:
                blob = self._bucket.blob(self._key(record_id, name))
                (target / name).write_bytes(blob.download_as_bytes())
            except self._api_error as exc:
                raise self._wrap(f"download record {record_id}", exc) from exc
        return target

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
                continue  # claimed or half-uploaded: not committed, not visible
            try:
                out.append(Record.load(self._materialise(record_id, listing[record_id])))
            except StorageError as exc:
                self.problems.append(str(exc))
        return out

    # -- writing ----------------------------------------------------------

    def _put(self, key: str, data: bytes, *, if_absent: bool = False) -> None:
        blob = self._bucket.blob(key)
        kwargs: dict[str, Any] = {"if_generation_match": 0} if if_absent else {}
        blob.upload_from_string(data, **kwargs)

    def _claim_id(self) -> int:
        """Claim the next free id with a conditional write; the loser moves on."""
        taken = self._list_objects()
        candidate = max([self._id_hint, *taken], default=0) + 1
        for _ in range(MAX_ID_ATTEMPTS):
            try:
                self._put(self._key(candidate, CLAIM_NAME), b"", if_absent=True)
            except self._precondition_failed:
                candidate += 1  # another writer got there first
                continue
            except self._api_error as exc:
                raise self._wrap("claim a record id", exc) from exc
            else:
                self._id_hint = candidate
                return candidate
        raise StorageError(f"could not allocate a record id in {self.dir} after 64 attempts")

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
        with self._mutex:
            record.id = self._claim_id()
        try:
            for name, data in files.items():
                self._put(self._key(record.id, name), data)
            self._put(self._key(record.id, META_NAME), _encode_meta(record))
        except self._api_error as io_error:
            # The claim (and any partial uploads) stay behind, invisible to
            # readers; `quarantine reindex` sweeps them.
            raise self._wrap("write a record", io_error) from io_error
        files[META_NAME] = _encode_meta(record)
        record.path = self._write_cache(record.id, files)
        return record

    def _write_cache(self, record_id: int, files: dict[str, bytes]) -> Path:
        target = self._cache / f"{record_id:04d}"
        target.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (target / name).write_bytes(data)
        return target

    def update(self, record: Record) -> None:
        """Rewrite ``meta.json`` for an existing record."""
        try:
            self._put(self._key(record.id, META_NAME), _encode_meta(record))
        except self._api_error as exc:
            raise self._wrap(f"update record {record.id}", exc) from exc
        cached = self._write_cache(record.id, {META_NAME: _encode_meta(record)})
        if record.path is None:
            record.path = cached

    def write_traceback(self, record: Record, exc: BaseException) -> None:
        """Replace a record's stored traceback (used when a retry fails again)."""
        import traceback as tb  # noqa: PLC0415 - keep the module namespace tidy

        text = "".join(tb.format_exception(type(exc), exc, exc.__traceback__)).encode("utf-8")
        try:
            self._put(self._key(record.id, TRACEBACK_NAME), text)
        except self._api_error as io_error:
            raise self._wrap(f"update record {record.id}", io_error) from io_error
        self._write_cache(record.id, {TRACEBACK_NAME: text})

    # -- deleting ---------------------------------------------------------

    def _delete_keys(self, keys: list[str]) -> None:
        """Delete every key, tolerating one already gone (another worker raced us).

        Unlike S3's ``delete_objects``, GCS's ``Blob.delete`` raises ``NotFound``
        for a key that no longer exists - and since ``clear``/``purge_temp`` list
        first and delete second, a concurrent deleter can win that race. That is
        not a failure, so it is swallowed the same way S3's idempotent batch
        delete absorbs it silently.
        """
        for key in keys:
            try:
                self._bucket.blob(key).delete()
            except self._not_found:
                continue
            except self._api_error as exc:
                raise self._wrap("delete records", exc) from exc

    def delete(self, record: Record | int) -> None:
        """Remove one record's objects (and its cache entry)."""
        record_id = record if isinstance(record, int) else record.id
        files = self._list_objects().get(record_id, {})
        self._delete_keys([self._key(record_id, name) for name in files])
        shutil.rmtree(self._cache / f"{record_id:04d}", ignore_errors=True)

    def clear(self) -> int:
        """Delete every record under the prefix. Returns how many were committed."""
        listing = self._list_objects()
        removed = sum(1 for files in listing.values() if META_NAME in files)
        keys = [
            self._key(record_id, name) for record_id, files in listing.items() for name in files
        ]
        self._delete_keys(keys)
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
            self._delete_keys([self._key(record_id, name) for name in files])
            swept += 1
        return swept

    # -- the index --------------------------------------------------------

    def rebuild_index(self) -> list[dict[str, Any]]:
        """GCS keeps no index object: the listing is always live. Returns the rows."""
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
        """Total size of every object under the prefix."""
        return sum(size for files in self._list_objects().values() for size in files.values())


def _encode_meta(record: Record) -> bytes:
    return json.dumps(record.to_meta(), indent=2, default=str).encode("utf-8")

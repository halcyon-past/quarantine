"""The S3 storage backend: one shared quarantine for a fleet of workers.

The record layout mirrors the local folder exactly - per-record objects under
``s3://bucket/prefix/0001/...`` - but the two primitives the local store's
guarantees rest on do not exist on S3, so they are replaced (see ADR 0007):

**Id allocation.** The local store claims an id by creating a directory,
which is atomic. Here an id is claimed by writing a zero-byte ``.claim``
object with ``If-None-Match: *`` - S3's conditional write - so two workers
can never both own an id; the loser gets ``PreconditionFailed`` and takes
the next number.

**The commit point.** The local store renames a staged directory into place.
Here ``meta.json`` is uploaded *last*, and readers ignore any record prefix
that lacks it - so a reader can never observe a half-written record, and a
crash mid-upload leaves invisible debris that ``quarantine reindex`` sweeps.

Reads are materialised into a per-URL cache directory under the system temp
folder, so :class:`~quarantine.record.Record` objects behave exactly as they
do locally - ``quarantine show``, ``debug``, ``retry`` and the dashboard all
work unchanged against a bucket.

Requires ``boto3``: ``pip install "quarantine-py[s3]"``. Credentials and
region come from the standard boto3 chain (environment, profile, SSO,
instance role); the IAM permissions needed are documented in
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

__all__ = ["S3Store"]

CLAIM_NAME = ".claim"
MAX_ID_ATTEMPTS = 64
_DELETE_BATCH = 1000

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


def _import_boto3() -> tuple[ModuleType, type[Exception]]:
    try:
        import boto3  # noqa: PLC0415 - deferred so the core package stays zero-dependency
        from botocore.exceptions import ClientError  # noqa: PLC0415
    except ImportError as exc:
        raise StorageError(
            "the s3:// backend needs boto3, which is an optional extra: "
            'pip install "quarantine-py[s3]"'
        ) from exc
    return boto3, ClientError


class S3Store(StorageBackend):
    """A quarantine stored as per-record objects under an S3 prefix."""

    def __init__(self, url: str) -> None:
        if not url.startswith("s3://"):
            raise StorageError(f"not an s3:// URL: {url!r}")
        rest = url[len("s3://") :]
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise StorageError(f"{url!r} is missing a bucket name (s3://bucket/prefix)")
        boto3, client_error = _import_boto3()
        self.dir: str = url.rstrip("/")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.problems: list[str] = []
        self._client = boto3.client("s3")
        self._client_error = client_error
        self._mutex = threading.Lock()
        self._id_hint = 0
        digest = hashlib.sha256(self.dir.encode("utf-8")).hexdigest()[:12]
        self._cache = Path(tempfile.gettempdir()) / f"quarantine-s3-{digest}"

    def __repr__(self) -> str:
        return f"S3Store({self.dir!r})"

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
        """Map of ``id -> {filename: size}`` for every object under the prefix."""
        out: dict[int, dict[str, int]] = {}
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            pages = paginator.paginate(Bucket=self.bucket, Prefix=self._list_prefix())
            for page in pages:
                for item in page.get("Contents", []):
                    tail = item["Key"][len(self._list_prefix()) :]
                    dirname, _, filename = tail.partition("/")
                    if dirname.isdigit() and filename:
                        out.setdefault(int(dirname), {})[filename] = item["Size"]
        except self._client_error as exc:
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
                key = self._key(record_id, name)
                response = self._client.get_object(Bucket=self.bucket, Key=key)
                (target / name).write_bytes(response["Body"].read())
            except self._client_error as exc:
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

    def _put(self, key: str, data: bytes, *, if_none_match: bool = False) -> None:
        extra: dict[str, Any] = {"IfNoneMatch": "*"} if if_none_match else {}
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def _claim_id(self) -> int:
        """Claim the next free id with a conditional write; the loser moves on."""
        taken = self._list_objects()
        candidate = max([self._id_hint, *taken], default=0) + 1
        for _ in range(MAX_ID_ATTEMPTS):
            try:
                self._put(self._key(candidate, CLAIM_NAME), b"", if_none_match=True)
            except self._client_error as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                    candidate += 1  # another writer got there first
                    continue
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
        except self._client_error as io_error:
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
        except self._client_error as exc:
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
        except self._client_error as io_error:
            raise self._wrap(f"update record {record.id}", io_error) from io_error
        self._write_cache(record.id, {TRACEBACK_NAME: text})

    # -- deleting ---------------------------------------------------------

    def _delete_keys(self, keys: list[str]) -> None:
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = [{"Key": key} for key in keys[start : start + _DELETE_BATCH]]
            try:
                self._client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})
            except self._client_error as exc:
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
        """S3 keeps no index object: the listing is always live. Returns the rows."""
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

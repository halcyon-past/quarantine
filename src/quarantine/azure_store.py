"""The Azure Blob Storage backend: one shared quarantine for a fleet of workers.

The record layout mirrors the local folder and the S3/GCS backends exactly -
per-record objects under ``azure://container/prefix/0001/...`` (or ``az://...``) -
with Azure's conditional write and commit primitives (see ADR 0007):

**Id allocation.** The local store claims an id by creating a directory, which
is atomic; the S3 backend claims one with ``If-None-Match: *``; the GCS backend
uses ``if_generation_match=0``. Here an id is claimed by uploading a zero-byte
``.claim`` object with ``overwrite=False`` - Azure's conditional create via
``If-None-Match: *`` - so two workers can never both own an id; the loser gets
``ResourceExistsError`` (409/412) and takes the next number.

**The commit point.** ``meta.json`` is uploaded *last*, and readers ignore
any record prefix that lacks it - so a reader can never observe a half-written
record, and a crash mid-upload leaves invisible debris that
``quarantine reindex`` sweeps.

Reads are materialised into a per-URL cache directory under the system temp
folder, so :class:`~quarantine.record.Record` objects behave exactly as they
do locally - ``quarantine show``, ``debug``, ``retry`` and the dashboard all
work unchanged against an Azure container.

Requires ``azure-storage-blob``: ``pip install "quarantine-py[azure]"``.
Credentials and account come from the standard Azure auth chain
(``AZURE_STORAGE_CONNECTION_STRING``, ``AZURE_STORAGE_KEY`` /
``AZURE_STORAGE_ACCOUNT_KEY``, ``AZURE_STORAGE_SAS_TOKEN``, or
``DefaultAzureCredential`` via ``azure-identity``); the RBAC roles needed
are documented in ``docs/remote-storage.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
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

__all__ = ["AzureBlobStore", "AzureStore"]

CLAIM_NAME = ".claim"
MAX_ID_ATTEMPTS = 64
_CONFLICT_STATUS_CODES = {409, 412}
_NOT_FOUND_STATUS_CODE = 404

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


def _import_azure() -> tuple[
    ModuleType,
    type[Exception],
    type[Exception],
    type[Exception],
    type[Exception],
]:
    try:
        from azure.core.exceptions import (  # noqa: PLC0415
            AzureError,
            HttpResponseError,
            ResourceExistsError,
            ResourceNotFoundError,
        )
        from azure.storage import blob  # noqa: PLC0415 - deferred, optional extra
    except (ImportError, AttributeError) as exc:
        raise StorageError(
            "the azure:// backend needs azure-storage-blob, which is an optional extra: "
            'pip install "quarantine-py[azure]"'
        ) from exc
    return blob, ResourceExistsError, ResourceNotFoundError, HttpResponseError, AzureError


class AzureBlobStore(StorageBackend):
    """A quarantine stored as per-record objects under an Azure Blob Storage prefix."""

    def __init__(self, url: str, *, container_client: Any = None) -> None:
        if not (url.startswith("azure://") or url.startswith("az://")):
            raise StorageError(f"not an azure:// or az:// URL: {url!r}")

        scheme = "az" if url.startswith("az://") else "azure"
        rest = url[len(f"{scheme}://") :]

        # Support optional account in URL: azure://account@container/prefix
        account: str | None = None
        custom_endpoint: str | None = None
        if "@" in rest:
            account, _, rest = rest.partition("@")

        container, _, prefix = rest.partition("/")
        container = container.strip("/")

        # Support azure://account.blob.core.<domain>/container/prefix
        if ".blob.core." in container:
            custom_endpoint = f"https://{container}"
            account, _, _ = container.partition(".")
            container, _, prefix = prefix.partition("/")
            container = container.strip("/")

        if not container:
            raise StorageError(f"{url!r} is missing a container name (azure://container/prefix)")

        blob_mod, resource_exists, resource_not_found, http_error, azure_error = _import_azure()
        self.dir: str = url.rstrip("/")
        self.container_name = container
        self.prefix = prefix.strip("/")
        self.problems: list[str] = []
        self._resource_exists = resource_exists
        self._resource_not_found = resource_not_found
        self._http_error = http_error
        self._azure_error = azure_error

        if container_client is not None:
            self._container_client = container_client
        else:
            self._container_client = self._init_container_client(
                blob_mod, container, account, url, custom_endpoint=custom_endpoint
            )

        self._mutex = threading.Lock()
        self._id_hint = 0
        digest = hashlib.sha256(self.dir.encode("utf-8")).hexdigest()[:12]
        self._cache = Path(tempfile.gettempdir()) / f"quarantine-azure-{digest}"

    def _init_container_client(
        self,
        blob_mod: Any,
        container: str,
        account: str | None,
        url: str,
        *,
        custom_endpoint: str | None = None,
    ) -> Any:
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            try:
                return blob_mod.ContainerClient.from_connection_string(
                    conn_str.strip(), container_name=container
                )
            except Exception as exc:
                raise StorageError(
                    f"cannot connect to Azure Blob Storage via connection string for {url}: {exc}"
                ) from exc

        account_name = (
            account
            or os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
            or os.environ.get("AZURE_STORAGE_ACCOUNT")
        )
        if account_name:
            account_name = account_name.strip()
        if not account_name:
            raise StorageError(
                f"cannot determine Azure Storage account for {url}: set "
                "AZURE_STORAGE_CONNECTION_STRING, AZURE_STORAGE_ACCOUNT_NAME, "
                "or specify the account in the URL (azure://account@container/prefix)."
            )

        endpoint = (
            os.environ.get("AZURE_STORAGE_EMULATOR_HOST")
            or os.environ.get("AZURE_BLOB_ENDPOINT_URL")
            or custom_endpoint
        )
        if endpoint:
            endpoint = endpoint.strip().rstrip("/")
            if account_name and account_name not in endpoint:
                account_url = f"{endpoint}/{account_name}"
            else:
                account_url = endpoint
        else:
            account_url = f"https://{account_name}.blob.core.windows.net"

        raw_cred = (
            os.environ.get("AZURE_STORAGE_KEY")
            or os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
            or os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        )
        credential: Any = raw_cred.strip() if raw_cred else None

        if not credential:
            try:
                from azure.identity import DefaultAzureCredential  # noqa: PLC0415

                credential = DefaultAzureCredential()
            except ImportError:
                credential = None
            except self._azure_error:
                credential = None

        try:
            return blob_mod.ContainerClient(
                account_url=account_url,
                container_name=container,
                credential=credential,
            )
        except Exception as exc:
            raise StorageError(
                f"cannot authenticate to Azure Blob Storage for {url}: {exc}. "
                "Set AZURE_STORAGE_CONNECTION_STRING, AZURE_STORAGE_KEY, or install azure-identity."
            ) from exc

    def __repr__(self) -> str:
        return f"AzureBlobStore({self.dir!r})"

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
            blobs = self._container_client.list_blobs(name_starts_with=self._list_prefix())
            for blob in blobs:
                tail = blob.name[len(self._list_prefix()) :]
                dirname, _, filename = tail.partition("/")
                if dirname.isdigit() and filename:
                    out.setdefault(int(dirname), {})[filename] = blob.size or 0
        except self._azure_error as exc:
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
        """Nothing to create: the container must already exist, prefixes are implicit."""

    def count(self) -> int:
        """How many committed records the prefix holds."""
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

    def _materialise(self, record_id: int, filenames: Iterable[str]) -> Path:
        """Download one record's files into the local cache, returning its directory."""
        target = self._safe_target_dir(record_id)
        for name in filenames:
            if name == CLAIM_NAME or not _is_safe_filename(name):
                continue
            dest = target / name
            if dest.is_symlink():
                raise StorageError(f"cache destination cannot be a symlink: {dest}")
            if not dest.resolve().is_relative_to(target):
                continue
            try:
                blob_client = self._container_client.get_blob_client(self._key(record_id, name))
                data = blob_client.download_blob().readall()
            except self._azure_error as exc:
                raise self._wrap(f"download record {record_id}", exc) from exc
            _safe_write_cache_file(target, name, data)
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
        blob_client = self._container_client.get_blob_client(key)
        blob_client.upload_blob(data, overwrite=not if_absent)

    def _claim_id(self) -> int:
        """Claim the next free id with a conditional write; the loser moves on."""
        taken = self._list_objects()
        candidate = max([self._id_hint, *taken], default=0) + 1
        for _ in range(MAX_ID_ATTEMPTS):
            try:
                self._put(self._key(candidate, CLAIM_NAME), b"", if_absent=True)
            except self._resource_exists:
                candidate += 1  # another writer got there first
                continue
            except self._http_error as exc:
                if getattr(exc, "status_code", None) in _CONFLICT_STATUS_CODES:
                    candidate += 1
                    continue
                raise self._wrap("claim a record id", exc) from exc
            except self._azure_error as exc:
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
        except self._azure_error as io_error:
            # The claim (and any partial uploads) stay behind, invisible to
            # readers; `quarantine reindex` sweeps them.
            raise self._wrap("write a record", io_error) from io_error
        files[META_NAME] = _encode_meta(record)
        record.path = self._write_cache(record.id, files)
        return record

    def _write_cache(self, record_id: int, files: dict[str, bytes]) -> Path:
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

    def update(self, record: Record) -> None:
        """Rewrite ``meta.json`` for an existing record."""
        try:
            self._put(self._key(record.id, META_NAME), _encode_meta(record))
        except self._azure_error as exc:
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
        except self._azure_error as io_error:
            raise self._wrap(f"update record {record.id}", io_error) from io_error
        self._write_cache(record.id, {TRACEBACK_NAME: text})

    # -- deleting ---------------------------------------------------------

    def _delete_keys(self, keys: list[str]) -> None:
        """Delete every key, tolerating one already gone (another worker raced us)."""
        for key in keys:
            try:
                self._container_client.delete_blob(key)
            except self._resource_not_found:
                continue
            except self._http_error as exc:
                if getattr(exc, "status_code", None) == _NOT_FOUND_STATUS_CODE:
                    continue
                raise self._wrap("delete records", exc) from exc
            except self._azure_error as exc:
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
        """Azure keeps no index object: the listing is always live. Returns the rows."""
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


AzureStore = AzureBlobStore


def _encode_meta(record: Record) -> bytes:
    return json.dumps(record.to_meta(), indent=2, default=str).encode("utf-8")


def _is_safe_filename(name: str) -> bool:
    """Return whether ``name`` is a single safe basename with no path traversal."""
    return (
        bool(name)
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

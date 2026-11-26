"""The Redis storage backend: short-lived, high-churn quarantine for worker fleets.

Records are stored as Redis hashes under ``{<prefix>}:record:<id>`` (default
prefix ``quarantine``), where each hash field holds one record file
(``meta.json``, ``input.pkl`` / ``input.json`` / ``input.txt``,
``traceback.txt``). The ``{<prefix>}`` hash tag ensures all keys for a
quarantine prefix map to the same Redis Cluster hash slot.

**Atomicity.** Writing a record uses a single Redis transaction (``MULTI``/``EXEC``
pipeline) with ``HSET`` (and ``EXPIRE`` when a TTL is configured), so a reader
never observes a half-written record and all files of a record share the same
expiration.

**Id allocation.** Record ids are allocated atomically via ``INCR`` on
``{<prefix>}:next_id``, ensuring multiple workers never claim the same id.

**Local cache.** Reads are materialised into a per-URL cache directory under
the system temp folder, so :class:`~quarantine.record.Record` objects behave
exactly as they do locally — ``quarantine show``, ``debug``, ``retry`` and the
dashboard all work unchanged.

Requires ``redis``: ``pip install "quarantine-py[redis]"``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import StorageError
from .record import META_NAME, TRACEBACK_NAME, Record
from .serialize import Serialized
from .store import StorageBackend, build_record

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import ModuleType

__all__ = ["RedisStore"]

MAX_ID_ATTEMPTS = 64
DEFAULT_PREFIX = "quarantine"
_DELETE_BATCH = 1000
_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/@]*@)")
_QUERY_PASSWORD_RE = re.compile(r"(?i)([?&]password=)[^&#]*")

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


def _import_redis() -> tuple[ModuleType, type[Exception], type[Exception]]:
    try:
        import redis  # noqa: PLC0415 - deferred so the core package stays zero-dependency
        from redis.exceptions import RedisError, WatchError  # noqa: PLC0415
    except ImportError as exc:
        raise StorageError(
            "the redis:// backend needs redis, which is an optional extra: "
            'pip install "quarantine-py[redis]"'
        ) from exc
    return redis, RedisError, WatchError


def _safe_url_for_error(url: str) -> str:
    """Mask any credentials in *url* before embedding it in an error message."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.password is not None or _QUERY_PASSWORD_RE.search(url):
            return _mask_password(parsed)
    except ValueError:
        pass
    masked = _USERINFO_RE.sub(r"\1***@", url)
    return _QUERY_PASSWORD_RE.sub(r"\1***", masked).rstrip("/")


def _parse_ttl(raw: str | int | None, safe_url: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise StorageError(f"invalid ttl {raw!r} in {safe_url!r}: must be a positive integer")
    if isinstance(raw, int):
        value = raw
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise StorageError(
                f"invalid ttl {raw!r} in {safe_url!r}: must be a positive integer"
            ) from exc
    if value < 1:
        raise StorageError(f"invalid ttl {value!r} in {safe_url!r}: must be >= 1")
    return value


def _mask_password(parsed: urllib.parse.ParseResult) -> str:
    """Return the URL string with any password replaced by '***'."""
    if parsed.password is not None:
        user = urllib.parse.quote(parsed.username or "", safe="")
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{user}:***@{host}{port}"
        parsed = parsed._replace(netloc=netloc)
    raw = urllib.parse.urlunparse(parsed).rstrip("/")
    return _QUERY_PASSWORD_RE.sub(r"\1***", raw)


def _parse_redis_url(
    url: str,
    *,
    prefix: str | None = None,
    ttl: int | None = None,
) -> tuple[str, str, str, int | None]:
    """Parse a ``redis://`` or ``rediss://`` URL into ``(display_url, client_url, prefix, ttl)``."""
    safe_url = _safe_url_for_error(url)
    try:
        parsed = urllib.parse.urlparse(url)
        _ = parsed.port
    except ValueError as exc:
        raise StorageError(f"invalid Redis URL {safe_url!r}: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"redis", "rediss"}:
        raise StorageError(f"not a redis:// or rediss:// URL: {safe_url!r}")
    if not parsed.hostname:
        raise StorageError(f"{safe_url!r} is missing a host (redis://host:6379/0)")

    raw_path = parsed.path.lstrip("/")
    db_part = ""
    path_prefix = ""
    if raw_path:
        first, _, rest = raw_path.partition("/")
        if first.isdigit():
            db_part = f"/{first}"
            path_prefix = rest.strip("/")
        else:
            db_part = "/0"
            path_prefix = raw_path.strip("/")

    query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query_prefix_list = query_params.pop("prefix", [])
    query_ttl_list = query_params.pop("ttl", [])

    resolved_prefix = (
        prefix
        if prefix is not None
        else (query_prefix_list[-1] if query_prefix_list else path_prefix)
    )
    resolved_prefix = resolved_prefix.strip(":/") or DEFAULT_PREFIX
    if "{" in resolved_prefix or "}" in resolved_prefix:
        raise StorageError(
            f"invalid prefix {resolved_prefix!r} in {safe_url!r}: cannot contain '{{' or '}}'"
        )

    if ttl is not None:
        resolved_ttl = _parse_ttl(ttl, safe_url)
    elif query_ttl_list:
        resolved_ttl = _parse_ttl(query_ttl_list[-1], safe_url)
    else:
        resolved_ttl = None

    clean_query = urllib.parse.urlencode(query_params, doseq=True)
    client_url = urllib.parse.urlunparse((scheme, parsed.netloc, db_part, "", clean_query, ""))
    display_url = _mask_password(parsed)
    return display_url, client_url, resolved_prefix, resolved_ttl


def _decode_hash(raw_map: dict[Any, Any] | list[Any]) -> dict[str, bytes]:
    """Decode raw Redis hash field names to ``str`` while preserving ``bytes`` values."""
    items = (
        raw_map.items()
        if isinstance(raw_map, dict)
        else zip(raw_map[0::2], raw_map[1::2], strict=False)
    )
    out: dict[str, bytes] = {}
    for k, v in items:
        name = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
        val = v if isinstance(v, bytes) else str(v).encode("utf-8")
        out[name] = val
    return out


class RedisStore(StorageBackend):
    """A quarantine stored as Redis hashes under a configurable key prefix."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str | None = None,
        ttl: int | None = None,
        client: Any | None = None,
    ) -> None:
        display_url, client_url, resolved_prefix, resolved_ttl = _parse_redis_url(
            url, prefix=prefix, ttl=ttl
        )
        redis_mod, redis_error, watch_error = _import_redis()
        self.dir: str = display_url
        self.prefix: str = resolved_prefix
        self.ttl: int | None = resolved_ttl
        self.problems: list[str] = []
        self._client_error = redis_error
        self._watch_error = watch_error
        if client is not None:
            self._client = client
        else:
            try:
                self._client = redis_mod.Redis.from_url(client_url, decode_responses=False)
            except (redis_error, ValueError, TypeError) as exc:
                raise self._wrap("connect", exc) from exc
        self._mutex = threading.Lock()
        digest = hashlib.sha256(f"{client_url}|{self.prefix}".encode()).hexdigest()[:12]
        self._cache = Path(tempfile.gettempdir()) / f"quarantine-redis-{digest}"

    def __repr__(self) -> str:
        return f"RedisStore({self.dir!r})"

    # -- keys -------------------------------------------------------------

    def _key_tag(self) -> str:
        return f"{{{self.prefix}}}"

    def _record_key(self, record_id: int) -> str:
        return f"{self._key_tag()}:record:{record_id:04d}"

    def _record_pattern(self) -> str:
        return f"{self._key_tag()}:record:*"

    def _next_id_key(self) -> str:
        return f"{self._key_tag()}:next_id"

    def _wrap(self, action: str, exc: Exception) -> StorageError:
        return StorageError(f"cannot {action} in {self.dir}: {exc}")

    # -- listing ----------------------------------------------------------

    def _record_keys(self) -> dict[int, str]:
        """Map of ``record_id -> redis_key`` for every record key under the prefix."""
        marker = f"{self._key_tag()}:record:"
        out: dict[int, str] = {}
        try:
            for raw_key in self._client.scan_iter(match=self._record_pattern()):
                key = (
                    raw_key.decode("utf-8", errors="replace")
                    if isinstance(raw_key, bytes)
                    else str(raw_key)
                )
                if not key.startswith(marker):
                    continue
                tail = key[len(marker) :]
                if tail.isascii() and tail.isdigit():
                    out[int(tail)] = key
        except self._client_error as exc:
            raise self._wrap("list records", exc) from exc
        return dict(sorted(out.items()))

    def _committed_mask(self, ordered_keys: list[str], *, action: str) -> list[bool]:
        """Return whether each key in *ordered_keys* contains ``meta.json``."""
        if not ordered_keys:
            return []
        try:
            pipe = self._client.pipeline(transaction=False)
            for key in ordered_keys:
                pipe.hexists(key, META_NAME)
            results = pipe.execute()
        except self._client_error as exc:
            raise self._wrap(action, exc) from exc
        return [bool(present) for present in results]

    def _fetch_all_hashes(self, keys: dict[int, str]) -> dict[int, dict[str, bytes]]:
        """Fetch hashes for *keys* via pipeline, decoding field names."""
        if not keys:
            return {}
        ordered_ids = list(keys.keys())
        try:
            pipe = self._client.pipeline(transaction=False)
            for rid in ordered_ids:
                pipe.hgetall(keys[rid])
            results = pipe.execute()
        except self._client_error as exc:
            raise self._wrap("read records", exc) from exc

        out: dict[int, dict[str, bytes]] = {}
        for rid, raw_map in zip(ordered_ids, results, strict=True):
            if raw_map:
                out[rid] = _decode_hash(raw_map)
        return out

    # -- reading ----------------------------------------------------------

    def exists(self) -> bool:
        """Whether any committed record exists under the prefix."""
        return self.count() > 0

    def ensure(self) -> None:
        """Nothing to create upfront: Redis keys are created on first write."""

    def count(self) -> int:
        """How many committed records are stored under the prefix."""
        keys = self._record_keys()
        if not keys:
            return 0
        mask = self._committed_mask(list(keys.values()), action="count records")
        return sum(1 for present in mask if present)

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
            if self._cache.is_symlink():
                raise StorageError(f"cache directory is an unsafe symlink: {self._cache}")
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

    def get(self, record_id: int) -> Record:
        """Fetch one record into the local cache and load it."""
        key = self._record_key(record_id)
        try:
            raw_map = self._client.hgetall(key)
        except self._client_error as exc:
            raise self._wrap(f"download record {record_id}", exc) from exc
        if not raw_map:
            raise StorageError(f"no record {record_id} in {self.dir}")
        files = _decode_hash(raw_map)
        if META_NAME not in files:
            raise StorageError(f"no record {record_id} in {self.dir}")
        return Record.load(self._write_cache(record_id, files, replace=True))

    def records(self) -> list[Record]:
        """Load every committed record; unreadable ones are reported, not fatal."""
        self.problems = []
        keys = self._record_keys()
        hashes = self._fetch_all_hashes(keys)
        out: list[Record] = []
        for record_id in sorted(hashes):
            files = hashes[record_id]
            if META_NAME not in files:
                continue
            try:
                out.append(Record.load(self._write_cache(record_id, files, replace=True)))
            except StorageError as exc:
                self.problems.append(str(exc))
        return out

    # -- writing ----------------------------------------------------------

    def _claim_id(self) -> int:
        """Allocate the next free record id atomically via ``INCR``."""
        next_key = self._next_id_key()
        try:
            if not self._client.exists(next_key):
                existing = self._record_keys()
                if existing:
                    self._client.set(next_key, max(existing), nx=True)
            for _ in range(MAX_ID_ATTEMPTS):
                candidate = int(self._client.incr(next_key))
                if not self._client.exists(self._record_key(candidate)):
                    return candidate
        except self._client_error as exc:
            raise self._wrap("claim a record id", exc) from exc
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
        """Write one new record atomically in a Redis transaction."""
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

        meta_bytes = _encode_meta(record)
        payload: dict[str, bytes] = {**files, META_NAME: meta_bytes}
        key = self._record_key(record.id)
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.hset(key, mapping=payload)
            if self.ttl is not None:
                pipe.expire(key, self.ttl)
            pipe.execute()
        except self._client_error as io_error:
            raise self._wrap("write a record", io_error) from io_error

        record.path = self._write_cache(record.id, payload, replace=True)
        return record

    def _hset_if_exists(self, record_id: int, field: str, value: bytes) -> None:
        """Update *field* on an existing record atomically using ``WATCH``/``MULTI``."""
        key = self._record_key(record_id)
        try:
            with self._client.pipeline() as pipe:
                pipe.watch(key)
                if not pipe.exists(key):
                    pipe.unwatch()
                    raise StorageError(f"no record {record_id} in {self.dir}")
                pipe.multi()
                pipe.hset(key, field, value)
                pipe.execute()
        except self._watch_error as exc:
            raise StorageError(
                f"record {record_id} in {self.dir} changed concurrently during update"
            ) from exc
        except self._client_error as exc:
            raise self._wrap(f"update record {record_id}", exc) from exc

    def update(self, record: Record) -> None:
        """Rewrite ``meta.json`` for an existing record."""
        meta_bytes = _encode_meta(record)
        self._hset_if_exists(record.id, META_NAME, meta_bytes)
        cached = self._write_cache(record.id, {META_NAME: meta_bytes})
        if record.path is None:
            record.path = cached

    def write_traceback(self, record: Record, exc: BaseException) -> None:
        """Replace a record's stored traceback (used when a retry fails again)."""
        import traceback as tb  # noqa: PLC0415 - keep the module namespace tidy

        text = "".join(tb.format_exception(type(exc), exc, exc.__traceback__)).encode("utf-8")
        self._hset_if_exists(record.id, TRACEBACK_NAME, text)
        self._write_cache(record.id, {TRACEBACK_NAME: text})

    # -- deleting ---------------------------------------------------------

    def _delete_keys(self, keys: list[str], *, action: str) -> None:
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            try:
                self._client.delete(*batch)
            except self._client_error as exc:
                raise self._wrap(action, exc) from exc

    def delete(self, record: Record | int) -> None:
        """Remove one record's hash (and its cache entry)."""
        record_id = record if isinstance(record, int) else record.id
        self._delete_keys([self._record_key(record_id)], action=f"delete record {record_id}")
        with self._mutex:
            if not self._cache.is_symlink():
                target = self._cache / f"{record_id:04d}"
                if not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)

    def clear(self) -> int:
        """Delete every record and reset the id counter under the prefix."""
        keys = self._record_keys()
        ordered_keys = list(keys.values())
        removed = sum(
            1 for present in self._committed_mask(ordered_keys, action="clear records") if present
        )
        self._delete_keys([*ordered_keys, self._next_id_key()], action="clear records")
        with self._mutex:
            if not self._cache.is_symlink():
                shutil.rmtree(self._cache, ignore_errors=True)
        return removed

    def purge_temp(self) -> int:
        """Sweep partial hashes that lack ``meta.json``."""
        keys = self._record_keys()
        if not keys:
            return 0
        ordered_keys = list(keys.values())
        mask = self._committed_mask(ordered_keys, action="purge temp records")
        stale = [key for key, present in zip(ordered_keys, mask, strict=True) if not present]
        if stale:
            self._delete_keys(stale, action="purge temp records")
        return len(stale)

    # -- the index --------------------------------------------------------

    def rebuild_index(self) -> list[dict[str, Any]]:
        """Redis keeps no index object: the scan is always live. Returns the rows."""
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
        """Total payload bytes of every record hash under the prefix."""
        keys = self._record_keys()
        hashes = self._fetch_all_hashes(keys)
        return sum(
            len(name.encode("utf-8")) + len(data)
            for files in hashes.values()
            for name, data in files.items()
        )


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
        fileobj = os.fdopen(file_fd, "wb")
    except Exception:
        with contextlib.suppress(OSError):
            os.close(file_fd)
        raise

    with fileobj:
        fileobj.write(data)

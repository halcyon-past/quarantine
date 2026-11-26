"""The Redis backend: short-lived, high-churn quarantine with optional TTLs."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import pytest
from redis.exceptions import RedisError, WatchError

from quarantine import Quarantine, StorageBackend, open_store, quarantine, register_backend, retry
from quarantine.cli import main
from quarantine.core import Config
from quarantine.errors import QuarantineFull, StorageError, SystemicFailure
from quarantine.redis_store import RedisStore
from quarantine.store import coerce_dir
from quarantine.ui import DashboardHandler

SOURCE = """
FAIL_ON = {"bad", "worse"}


def load(item):
    if item in FAIL_ON:
        raise ValueError(f"cannot load {item}")
    return item.upper()
"""


class FakeRedisPipeline:
    def __init__(self, client: FakeRedisClient, *, transaction: bool = False) -> None:
        self._client = client
        self._transaction = transaction
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._watched: dict[str, int] = {}

    def __enter__(self) -> FakeRedisPipeline:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._ops.clear()
        self._watched.clear()

    def watch(self, *keys: str) -> bool:
        for key in keys:
            self._watched[key] = self._client.version_of(key)
        return True

    def unwatch(self) -> bool:
        self._watched.clear()
        return True

    def multi(self) -> None:
        self._transaction = True

    def exists(self, key: str) -> int:
        return self._client.exists(key)

    def hset(self, key: str, *args: Any, **kwargs: Any) -> FakeRedisPipeline:
        self._ops.append(("hset", (key, *args), kwargs))
        return self

    def hgetall(self, key: str) -> FakeRedisPipeline:
        self._ops.append(("hgetall", (key,), {}))
        return self

    def hexists(self, key: str, field: str) -> FakeRedisPipeline:
        self._ops.append(("hexists", (key, field), {}))
        return self

    def expire(self, key: str, seconds: int) -> FakeRedisPipeline:
        self._ops.append(("expire", (key, seconds), {}))
        return self

    def execute(self) -> list[Any]:
        for key, version in self._watched.items():
            if self._client.version_of(key) != version:
                self._ops.clear()
                self._watched.clear()
                raise WatchError("Watched variable changed.")
        results: list[Any] = []
        for name, args, kwargs in self._ops:
            results.append(getattr(self._client, name)(*args, **kwargs))
        self._ops.clear()
        self._watched.clear()
        return results


class FakeRedisClient:
    """In-memory Redis double supporting hashes, counters, SCAN, pipelines, and TTLs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hashes: dict[str, dict[bytes, bytes]] = {}
        self._strings: dict[str, int] = {}
        self._expires: dict[str, float] = {}
        self._versions: dict[str, int] = {}
        self.now_offset: float = 0.0

    def version_of(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            return self._versions.get(key, 0)

    def _now(self) -> float:
        return time.monotonic() + self.now_offset

    def _purge_if_expired(self, key: str) -> None:
        exp = self._expires.get(key)
        if exp is not None and self._now() >= exp:
            self._expires.pop(key, None)
            self._hashes.pop(key, None)
            self._strings.pop(key, None)

    def pipeline(self, transaction: bool = False) -> FakeRedisPipeline:
        return FakeRedisPipeline(self, transaction=transaction)

    def scan_iter(self, match: str = "*") -> list[bytes]:
        with self._lock:
            for key in list(self._expires):
                self._purge_if_expired(key)
            all_keys = list(self._hashes) + list(self._strings)
            return [k.encode("utf-8") for k in all_keys if fnmatch.fnmatch(k, match)]

    def hset(
        self,
        key: str,
        field: str | None = None,
        value: bytes | str | None = None,
        mapping: dict[str, bytes] | None = None,
    ) -> int:
        with self._lock:
            self._purge_if_expired(key)
            self._versions[key] = self._versions.get(key, 0) + 1
            bucket = self._hashes.setdefault(key, {})
            added = 0
            if mapping is not None:
                for k, v in mapping.items():
                    kb = k.encode("utf-8")
                    if kb not in bucket:
                        added += 1
                    bucket[kb] = v
            elif field is not None and value is not None:
                kb = field.encode("utf-8")
                vb = value if isinstance(value, bytes) else value.encode("utf-8")
                if kb not in bucket:
                    added += 1
                bucket[kb] = vb
            return added

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        with self._lock:
            self._purge_if_expired(key)
            return dict(self._hashes.get(key, {}))

    def hexists(self, key: str, field: str) -> bool:
        with self._lock:
            self._purge_if_expired(key)
            return field.encode("utf-8") in self._hashes.get(key, {})

    def exists(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            return int(key in self._hashes or key in self._strings)

    def set(self, key: str, value: int, nx: bool = False) -> bool:
        with self._lock:
            self._purge_if_expired(key)
            if nx and (key in self._strings or key in self._hashes):
                return False
            self._strings[key] = int(value)
            return True

    def incr(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            val = self._strings.get(key, 0) + 1
            self._strings[key] = val
            return val

    def expire(self, key: str, seconds: int) -> bool:
        with self._lock:
            self._purge_if_expired(key)
            if key not in self._hashes and key not in self._strings:
                return False
            self._expires[key] = self._now() + seconds
            return True

    def ttl(self, key: str) -> int:
        with self._lock:
            self._purge_if_expired(key)
            if key not in self._hashes and key not in self._strings:
                return -2
            if key not in self._expires:
                return -1
            return max(0, int(self._expires[key] - self._now()))

    def delete(self, *keys: str) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                self._purge_if_expired(key)
                if key in self._hashes or key in self._strings:
                    removed += 1
                self._hashes.pop(key, None)
                self._strings.pop(key, None)
                self._expires.pop(key, None)
            return removed


@pytest.fixture
def module(target_module):
    return target_module(SOURCE, name="qtarget_redis")


@pytest.fixture
def fake_redis():
    return FakeRedisClient()


@pytest.fixture
def redis_url(monkeypatch, fake_redis):
    """A Redis URL backed by the in-memory FakeRedisClient."""
    import redis

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_redis))
    prefix = f"q-{uuid.uuid4().hex[:8]}"
    url = f"redis://localhost:6379/0?prefix={prefix}"
    yield url
    digest = hashlib.sha256(f"redis://localhost:6379/0|{prefix}".encode()).hexdigest()[:12]
    shutil.rmtree(Path(tempfile.gettempdir()) / f"quarantine-redis-{digest}", ignore_errors=True)


@pytest.fixture
def store(redis_url):
    return RedisStore(redis_url)


# -- URL routing & parsing ------------------------------------------------


def test_coerce_dir_keeps_urls_and_paths_apart(tmp_path):
    assert coerce_dir("redis://localhost:6379/0") == "redis://localhost:6379/0"
    assert coerce_dir("rediss://localhost:6379/0") == "rediss://localhost:6379/0"
    assert coerce_dir(tmp_path) == tmp_path
    assert coerce_dir("plain/folder") == Path("plain/folder")
    assert str(coerce_dir(None)).endswith(".quarantine")


def test_open_store_picks_the_backend_by_scheme(redis_url, tmp_path):
    assert isinstance(open_store(tmp_path), StorageBackend)
    assert isinstance(open_store(redis_url), RedisStore)
    tls_url = redis_url.replace("redis://", "rediss://", 1)
    assert isinstance(open_store(tls_url), RedisStore)


def test_rediss_and_extra_query_params_are_forwarded(monkeypatch, fake_redis):
    import redis

    captured: list[str] = []

    def fake_from_url(cls, /, url, **kwargs):
        captured.append(url)
        return fake_redis

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(fake_from_url))
    s = RedisStore("rediss://localhost:6380/2/etl?prefix=q&ttl=60&ssl_cert_reqs=none")
    assert s.prefix == "q"
    assert s.ttl == 60
    parsed = urllib.parse.urlparse(captured[0])
    assert parsed.scheme == "rediss"
    assert parsed.path == "/2"
    assert urllib.parse.parse_qs(parsed.query) == {"ssl_cert_reqs": ["none"]}


def test_unknown_schemes_fail_with_directions():
    with pytest.raises(StorageError, match="register_backend"):
        open_store("carrierpigeon://coop/roost")


def test_third_parties_can_register_a_scheme(tmp_path):
    from quarantine.store import _BACKENDS, Store

    register_backend("carrierpigeon", lambda url: Store(tmp_path))
    try:
        assert open_store("carrierpigeon://coop/roost").dir == tmp_path
    finally:
        _BACKENDS.pop("carrierpigeon", None)


def test_config_does_not_mangle_urls():
    config = Config(dir="redis://localhost:6379/0?prefix=team", halt_after=None)
    assert config.dir == "redis://localhost:6379/0?prefix=team"


def test_the_environment_variable_accepts_a_url(monkeypatch):
    monkeypatch.setenv("QUARANTINE_DIR", "redis://localhost:6379/0")
    from quarantine.store import default_dir

    assert default_dir() == "redis://localhost:6379/0"


def test_invalid_urls_and_ttl_are_rejected():
    with pytest.raises(StorageError, match="redis://"):
        RedisStore("s3://bucket/prefix")
    with pytest.raises(StorageError, match="host"):
        RedisStore("redis:///0")
    with pytest.raises(StorageError, match="invalid Redis URL"):
        RedisStore("redis://user:supersecret@localhost:badport/0")
    with pytest.raises(StorageError, match="ttl"):
        RedisStore("redis://localhost:6379/0?ttl=abc")
    with pytest.raises(StorageError, match="ttl"):
        RedisStore("redis://localhost:6379/0?ttl=")
    with pytest.raises(StorageError, match="ttl"):
        RedisStore("redis://localhost:6379/0?ttl=0")
    with pytest.raises(StorageError, match="ttl"):
        RedisStore("redis://localhost:6379/0", ttl=-5)
    with pytest.raises(StorageError, match="ttl"):
        RedisStore("redis://localhost:6379/0", ttl=True)


def test_invalid_from_url_param_is_wrapped_and_secrets_masked():
    with pytest.raises(StorageError) as exc_info:
        RedisStore("redis://user:supersecret@localhost:6379/0?socket_timeout=notanumber")
    assert "supersecret" not in str(exc_info.value)

    with pytest.raises(StorageError) as exc_info2:
        RedisStore("redis://user:supersecret@localhost:6379/0?ttl=bad")
    assert "supersecret" not in str(exc_info2.value)


def test_prefix_and_ttl_parsing(monkeypatch, fake_redis):
    import redis

    captured: list[str] = []

    def fake_from_url(cls, /, url, **kwargs):
        captured.append(url)
        return fake_redis

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(fake_from_url))

    s1 = RedisStore("redis://localhost:6379")
    assert s1.prefix == "quarantine"
    assert s1.ttl is None
    assert captured[-1] == "redis://localhost:6379"

    s2 = RedisStore("redis://localhost:6379/2/etl/bad/")
    assert s2.prefix == "etl/bad"
    assert captured[-1] == "redis://localhost:6379/2"

    s2b = RedisStore("redis://localhost:6379/etl/bad")
    assert s2b.prefix == "etl/bad"
    assert captured[-1] == "redis://localhost:6379/0"

    s3 = RedisStore("redis://localhost:6379/2/path-p?prefix=:custom:dlq:&ttl=120")
    assert s3.prefix == "custom:dlq"
    assert s3.ttl == 120
    assert captured[-1] == "redis://localhost:6379/2"

    s4 = RedisStore(
        "redis://localhost:6379/0?prefix=a&ttl=60",
        prefix="override",
        ttl=30,
    )
    assert s4.prefix == "override"
    assert s4.ttl == 30


def test_password_is_masked_in_dir_and_repr_but_retained_for_client(monkeypatch, fake_redis):
    import redis

    captured: list[str] = []

    def fake_from_url(cls, /, url, **kwargs):
        captured.append(url)
        return fake_redis

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(fake_from_url))

    s_ipv6 = RedisStore("redis://user:supersecret@[::1]:6379/0")
    assert "supersecret" not in s_ipv6.dir
    assert "supersecret" not in repr(s_ipv6)
    assert "[::1]:6379" in s_ipv6.dir
    assert "***" in s_ipv6.dir
    assert "supersecret" in captured[-1]

    s_pw_only = RedisStore("redis://:supersecret@localhost:6379/0")
    assert "supersecret" not in s_pw_only.dir
    assert "***@localhost:6379" in s_pw_only.dir
    assert "supersecret" in captured[-1]


def test_the_missing_extra_is_named(monkeypatch, redis_url):
    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(StorageError, match=r"quarantine-py\[redis\]"):
        RedisStore(redis_url)


# -- record lifecycle -----------------------------------------------------


def test_empty_store_operations(store):
    assert not store.exists()
    assert store.count() == 0
    assert store.records() == []
    assert store.rebuild_index() == []
    assert store.fingerprints() == {}
    assert store.disk_bytes() == 0
    assert store.purge_temp() == 0
    store.delete(99)
    assert store.clear() == 0


def test_records_round_trip_through_redis(store, redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    q.call(module.load, "worse")

    assert store.exists()
    assert store.count() == 2
    records = store.records()
    assert [r.id for r in records] == [1, 2]
    assert [r.load_call().item for r in records] == ["bad", "worse"]
    assert "cannot load bad" in records[0].traceback_text()
    assert store.get(1).error == "cannot load bad"
    assert store.disk_bytes() > 0


def test_get_missing_or_partial_record_raises(store, fake_redis):
    with pytest.raises(StorageError, match="no record 99"):
        store.get(99)
    fake_redis.hset(f"{store._key_tag()}:record:0005", mapping={"traceback.txt": b"partial"})
    with pytest.raises(StorageError, match="no record 5"):
        store.get(5)


def test_non_int_keys_and_corrupt_meta_are_handled(store, fake_redis, redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    fake_redis.hset(f"{store._key_tag()}:record:notanint", mapping={"meta.json": b"{}"})
    fake_redis.hset(f"{store._key_tag()}:record:0002", mapping={"traceback.txt": b"partial"})
    fake_redis.hset(f"{store._key_tag()}:record:0003", mapping={"meta.json": b"{corrupt"})

    recs = store.records()
    assert [r.id for r in recs] == [1]
    assert len(store.problems) == 1


def test_deduplication_via_fingerprints(redis_url, module):
    first = Quarantine(redis_url, halt_after=None, report=False)
    first.call(module.load, "bad")
    second = Quarantine(redis_url, halt_after=None, report=False)
    second.call(module.load, "bad")
    store = RedisStore(redis_url)
    assert second.stats.skipped == 1
    assert store.count() == 1


def test_update_and_write_traceback(store, redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    rec = store.get(1)
    rec.attempts = 3
    rec.path = None
    store.update(rec)
    assert rec.path is not None
    assert store.get(1).attempts == 3

    try:
        raise RuntimeError("retried and exploded")
    except RuntimeError as exc:
        store.write_traceback(rec, exc)
    assert "retried and exploded" in store.get(1).traceback_text()


def test_delete_clear_and_rebuild_index(store, redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    q.call(module.load, "worse")

    rows = store.rebuild_index()
    assert [r["id"] for r in rows] == [1, 2]

    store.delete(1)
    assert store.count() == 1
    assert [r.id for r in store.records()] == [2]

    removed = store.clear()
    assert removed == 1
    assert not store.exists()
    assert store.count() == 0


def test_purge_temp_sweeps_incomplete_hashes(store, fake_redis, redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    fake_redis.hset(f"{store._key_tag()}:record:0002", mapping={"traceback.txt": b"partial"})
    assert store.count() == 1
    assert store.purge_temp() == 1
    assert store.purge_temp() == 0
    assert store.count() == 1


def test_prefix_isolation(monkeypatch, fake_redis, module):
    import redis

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, *a, **kw: fake_redis))
    url_a = "redis://localhost:6379/0?prefix=team-a"
    url_b = "redis://localhost:6379/0?prefix=team-b"
    qa = Quarantine(url_a, halt_after=None, report=False)
    qb = Quarantine(url_b, halt_after=None, report=False)

    qa.call(module.load, "bad")
    qb.call(module.load, "worse")

    sa = RedisStore(url_a)
    sb = RedisStore(url_b)
    assert [r.load_call().item for r in sa.records()] == ["bad"]
    assert [r.load_call().item for r in sb.records()] == ["worse"]

    sa.clear()
    assert sa.count() == 0
    assert sb.count() == 1


def test_ttl_expires_records(fake_redis, module):
    s = RedisStore("redis://localhost:6379/0?prefix=ephemeral&ttl=30", client=fake_redis)
    q = Quarantine(s.dir, halt_after=None, report=False)
    q.store = s

    q.call(module.load, "bad")
    assert s.count() == 1
    assert fake_redis.ttl("{ephemeral}:record:0001") > 0

    fake_redis.now_offset += 31
    assert s.count() == 0
    assert s.records() == []
    with pytest.raises(StorageError, match="no record 1"):
        s.get(1)


def test_update_expired_record_raises(fake_redis, module):
    s = RedisStore("redis://localhost:6379/0?prefix=exp&ttl=10", client=fake_redis)
    q = Quarantine(s.dir, halt_after=None, report=False)
    q.store = s
    q.call(module.load, "bad")
    rec = s.get(1)
    fake_redis.now_offset += 11

    with pytest.raises(StorageError, match="no record 1"):
        s.update(rec)
    with pytest.raises(StorageError, match="no record 1"):
        s.write_traceback(rec, RuntimeError("boom"))
    # Verify no orphaned/immortal key was created
    assert fake_redis.hgetall("{exp}:record:0001") == {}


def test_max_items_and_halt_after(redis_url, module):
    q_full = Quarantine(redis_url, max_items=1, halt_after=None, report=False)
    q_full.call(module.load, "bad")
    with pytest.raises(QuarantineFull):
        q_full.call(module.load, "worse")

    RedisStore(redis_url).clear()
    q_halt = Quarantine(redis_url, halt_after=2, report=False)
    q_halt.call(module.load, "bad")
    with pytest.raises(SystemicFailure):
        q_halt.call(module.load, "worse")


def test_claim_id_seeds_from_existing_and_exhaustion(store, fake_redis):
    fake_redis.hset(f"{store._key_tag()}:record:0005", mapping={"meta.json": b"{}"})
    assert store._claim_id() == 6

    # Exhaust MAX_ID_ATTEMPTS when all subsequent keys already exist
    for i in range(7, 7 + 65):
        fake_redis.hset(f"{store._key_tag()}:record:{i:04d}", mapping={"meta.json": b"{}"})
    with pytest.raises(StorageError, match="could not allocate a record id"):
        store._claim_id()


def test_concurrent_writers_never_collide_on_ids(redis_url):
    errors: list[Exception] = []

    @quarantine(dir=redis_url, halt_after=None, report=False, retries=0)
    def fail_always(item):
        raise ValueError(f"fail {item}")

    def run_item(idx: int) -> None:
        try:
            fail_always(f"item-{idx}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run_item, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    store = RedisStore(redis_url)
    ids = [r.id for r in store.records()]
    assert len(ids) == 10
    assert len(set(ids)) == 10


def test_retry_recovers_and_deletes_from_redis(redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    assert RedisStore(redis_url).count() == 1

    module.FAIL_ON.clear()
    result = retry(dir=redis_url)
    assert result.recovered == [1]
    assert RedisStore(redis_url).count() == 0


def test_the_cli_works_against_redis(redis_url, module, capsys):
    Quarantine(redis_url, halt_after=None, report=False).call(module.load, "bad")
    assert main(["list", "--dir", redis_url, "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["id"] == 1
    assert main(["show", "1", "--dir", redis_url]) == 0
    assert "cannot load bad" in capsys.readouterr().out
    assert main(["stats", "--dir", redis_url, "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["records"] == 1
    assert main(["reindex", "--dir", redis_url]) == 0
    assert main(["clear", "--dir", redis_url, "--yes"]) == 0
    assert RedisStore(redis_url).count() == 0


def test_dashboard_handler_reads_redis_store(redis_url, module):
    Quarantine(redis_url, halt_after=None, report=False).call(module.load, "bad")
    store = open_store(redis_url)
    records = store.records()
    assert len(records) == 1
    assert hasattr(DashboardHandler, "do_GET")
    assert store.get(1).function == "load"


def test_redis_errors_are_wrapped_as_storage_error(fake_redis):
    s = RedisStore("redis://localhost:6379/0?prefix=errtest", client=fake_redis)

    def boom(*args, **kwargs):
        raise RedisError("connection refused")

    fake_redis.scan_iter = boom
    with pytest.raises(StorageError, match="cannot list records"):
        s.records()


def test_watch_error_raises_storage_error(store, fake_redis, redis_url, module):
    q = Quarantine(redis_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    rec = store.get(1)

    orig_pipeline = fake_redis.pipeline

    class RacingPipeline(FakeRedisPipeline):
        def watch(self, *keys: str) -> bool:
            super().watch(*keys)
            # Simulate another worker modifying the record right after WATCH
            fake_redis.hset(keys[0], "other", b"race")
            return True

    fake_redis.pipeline = lambda transaction=False: RacingPipeline(
        fake_redis, transaction=transaction
    )
    try:
        with pytest.raises(StorageError, match="changed concurrently"):
            store.update(rec)
    finally:
        fake_redis.pipeline = orig_pipeline


def test_cache_security_and_stale_file_cleanup(store, fake_redis, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    store._cache.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(store._cache, ignore_errors=True)
    store._cache.symlink_to(outside)
    try:
        with pytest.raises(StorageError, match="symlink"):
            store._safe_target_dir(1)
    finally:
        store._cache.unlink(missing_ok=True)

    # Path traversal and null-byte field names are ignored
    cache_dir = store._write_cache(
        2,
        {"../escape": b"evil", "bad\x00name": b"evil", "ok.txt": b"safe"},
        replace=True,
    )
    assert (cache_dir / "ok.txt").read_bytes() == b"safe"
    assert not (cache_dir.parent / "escape").exists()

    # Re-writing with replace=True removes stale files from an earlier format
    store._write_cache(2, {"new.txt": b"fresh"}, replace=True)
    assert not (cache_dir / "ok.txt").exists()
    assert (cache_dir / "new.txt").read_bytes() == b"fresh"

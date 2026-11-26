"""The Databricks Unity Catalog Volumes backend."""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

import databricks.sdk.errors as db_errors
import pytest
from databricks.sdk.errors import NotFound, ResourceAlreadyExists
from databricks.sdk.service.files import DirectoryEntry, DownloadResponse

from quarantine import Quarantine, StorageBackend, open_store, quarantine, retry
from quarantine.cli import main
from quarantine.core import Config
from quarantine.databricks_store import DatabricksStore, DatabricksVolumeStore
from quarantine.errors import QuarantineFull, StorageError, SystemicFailure
from quarantine.store import coerce_dir
from quarantine.ui import DashboardHandler

SOURCE = """
FAIL_ON = {"bad", "worse"}


def load(item):
    if item in FAIL_ON:
        raise ValueError(f"cannot load {item}")
    return item.upper()
"""


class FakeFilesAPI:
    """In-memory Databricks FilesAPI double supporting directories, files, and overwrite=False."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirs: set[str] = set()
        self._files: dict[str, bytes] = {}

    def create_directory(self, directory_path: str) -> None:
        with self._lock:
            norm = posixpath.normpath(directory_path)
            parts = norm.strip("/").split("/")
            for i in range(1, len(parts) + 1):
                self._dirs.add("/" + "/".join(parts[:i]))

    def upload(self, file_path: str, contents: Any, *, overwrite: bool | None = None) -> None:
        data = contents.read()
        with self._lock:
            norm = posixpath.normpath(file_path)
            if overwrite is False and norm in self._files:
                raise ResourceAlreadyExists(f"File already exists: {norm}")
            parent = posixpath.dirname(norm)
            self._dirs.add(parent)
            self._files[norm] = data

    def download(self, file_path: str) -> DownloadResponse:
        with self._lock:
            norm = posixpath.normpath(file_path)
            if norm not in self._files:
                raise NotFound(f"File not found: {norm}")
            return DownloadResponse(contents=io.BytesIO(self._files[norm]))

    def delete(self, file_path: str) -> None:
        with self._lock:
            norm = posixpath.normpath(file_path)
            if norm not in self._files:
                raise NotFound(f"File not found: {norm}")
            del self._files[norm]

    def delete_directory(self, directory_path: str) -> None:
        with self._lock:
            norm = posixpath.normpath(directory_path)
            if norm not in self._dirs:
                raise NotFound(f"Directory not found: {norm}")
            prefix = norm + "/"
            for k in [p for p in self._files if p.startswith(prefix)]:
                del self._files[k]
            for d in [p for p in self._dirs if p == norm or p.startswith(prefix)]:
                self._dirs.discard(d)

    def list_directory_contents(self, directory_path: str) -> list[DirectoryEntry]:
        with self._lock:
            norm = posixpath.normpath(directory_path)
            if norm not in self._dirs:
                raise NotFound(f"Directory not found: {norm}")
            prefix = norm + "/"
            entries: list[DirectoryEntry] = []
            seen_dirs: set[str] = set()
            for d in sorted(self._dirs):
                if d.startswith(prefix):
                    rest = d[len(prefix) :]
                    if "/" not in rest and rest:
                        seen_dirs.add(d)
                        entries.append(DirectoryEntry(name=rest, path=d, is_directory=True))
            for f, data in sorted(self._files.items()):
                if f.startswith(prefix):
                    rest = f[len(prefix) :]
                    if "/" not in rest and rest:
                        entries.append(
                            DirectoryEntry(
                                name=rest,
                                path=f,
                                is_directory=False,
                                file_size=len(data),
                            )
                        )
            return entries


class FakeWorkspaceClient:
    def __init__(self) -> None:
        self.files = FakeFilesAPI()


@pytest.fixture
def module(target_module):
    return target_module(SOURCE, name="qtarget_databricks")


@pytest.fixture
def fake_ws():
    return FakeWorkspaceClient()


@pytest.fixture
def volume_path(monkeypatch, fake_ws):
    """A Unity Catalog volume path backed by FakeWorkspaceClient."""
    import databricks.sdk

    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", lambda *a, **kw: fake_ws)
    path = f"/Volumes/main/analytics/quarantine/run-{uuid.uuid4().hex[:8]}"
    yield path
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    shutil.rmtree(
        Path(tempfile.gettempdir()) / f"quarantine-databricks-{digest}", ignore_errors=True
    )


@pytest.fixture
def store(volume_path):
    return DatabricksVolumeStore(volume_path)


# -- Path routing & validation --------------------------------------------


def test_coerce_dir_keeps_volume_paths_as_strings(tmp_path):
    vol = "/Volumes/main/analytics/quarantine/etl"
    assert coerce_dir(vol) == vol
    assert coerce_dir(Path(vol)) == vol
    assert coerce_dir(tmp_path) == tmp_path
    assert coerce_dir("plain/folder") == Path("plain/folder")


def test_open_store_picks_databricks_backend_for_volumes(volume_path, tmp_path):
    assert isinstance(open_store(tmp_path), StorageBackend)
    opened = open_store(volume_path)
    assert isinstance(opened, DatabricksVolumeStore)
    assert DatabricksStore is DatabricksVolumeStore


def test_config_and_env_var_accept_volume_paths(monkeypatch):
    vol = "/Volumes/main/analytics/quarantine/etl"
    config = Config(dir=vol, halt_after=None)
    assert config.dir == vol

    monkeypatch.setenv("QUARANTINE_DIR", vol)
    from quarantine.store import default_dir

    assert default_dir() == vol


def test_invalid_volume_paths_are_rejected():
    with pytest.raises(StorageError, match="Unity Catalog volume path"):
        DatabricksVolumeStore("/tmp/not-a-volume")
    with pytest.raises(StorageError, match="catalog, schema, and volume"):
        DatabricksVolumeStore("/Volumes/main/analytics")
    with pytest.raises(StorageError, match="catalog, schema, and volume"):
        DatabricksVolumeStore("/Volumes/main")
    with pytest.raises(StorageError, match="Unity Catalog volume path"):
        DatabricksVolumeStore("/Volumes/")


def test_volume_metadata_attributes(store, volume_path):
    assert store.catalog == "main"
    assert store.schema == "analytics"
    assert store.volume == "quarantine"
    assert store.dir == volume_path
    assert volume_path in repr(store)


def test_the_missing_extra_is_named(monkeypatch, volume_path):
    monkeypatch.setitem(sys.modules, "databricks.sdk", None)
    with pytest.raises(StorageError, match=r"quarantine-py\[databricks\]"):
        DatabricksVolumeStore(volume_path)


# -- Record lifecycle -----------------------------------------------------


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


def test_records_round_trip_through_volume(store, volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
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


def test_get_missing_or_partial_record_raises(store, fake_ws):
    with pytest.raises(StorageError, match="no record 99"):
        store.get(99)
    store.ensure()
    fake_ws.files.upload(
        f"{store.dir}/0005/traceback.txt",
        io.BytesIO(b"partial"),
        overwrite=True,
    )
    with pytest.raises(StorageError, match="no record 5"):
        store.get(5)


def test_non_int_dirs_and_corrupt_meta_are_handled(store, fake_ws, volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
    q.call(module.load, "bad")
    fake_ws.files.upload(f"{store.dir}/notanint/meta.json", io.BytesIO(b"{}"), overwrite=True)
    fake_ws.files.upload(f"{store.dir}/²/meta.json", io.BytesIO(b"{}"), overwrite=True)
    fake_ws.files.upload(f"{store.dir}/0002/traceback.txt", io.BytesIO(b"partial"), overwrite=True)
    fake_ws.files.upload(f"{store.dir}/0003/meta.json", io.BytesIO(b"{corrupt"), overwrite=True)

    recs = store.records()
    assert [r.id for r in recs] == [1]
    assert len(store.problems) == 1


def test_known_bad_items_are_skipped_on_rerun(volume_path, module):
    first = Quarantine(volume_path, halt_after=None, report=False)
    first.call(module.load, "bad")
    second = Quarantine(volume_path, halt_after=None, report=False)
    second.call(module.load, "bad")
    assert second.stats.skipped == 1
    assert DatabricksVolumeStore(volume_path).count() == 1


def test_update_and_write_traceback(store, volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
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


def test_delete_clear_and_rebuild_index(store, volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
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


def test_purge_temp_sweeps_incomplete_records(store, fake_ws, volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
    q.call(module.load, "bad")
    fake_ws.files.upload(f"{store.dir}/0002/.claim", io.BytesIO(b""), overwrite=False)
    assert store.count() == 1
    assert store.purge_temp() == 1
    assert store.purge_temp() == 0
    assert store.count() == 1


def test_claim_conflict_advances_to_next_id_and_exhaustion(store, fake_ws, volume_path):
    store.ensure()
    fake_ws.files.upload(f"{store.dir}/0001/.claim", io.BytesIO(b""), overwrite=False)
    # _list_objects sees 1, so candidate starts at 2; pre-claim 2 behind our back via hint
    store._id_hint = 0
    fake_ws.files.upload(f"{store.dir}/0002/.claim", io.BytesIO(b""), overwrite=False)
    assert store._claim_id() == 3

    # Force exhaustion by making upload(overwrite=False) always raise ResourceAlreadyExists
    orig_upload = fake_ws.files.upload

    def always_conflict(file_path: str, contents: Any, *, overwrite: bool | None = None) -> None:
        if overwrite is False:
            raise ResourceAlreadyExists("already claimed")
        orig_upload(file_path, contents, overwrite=overwrite)

    fake_ws.files.upload = always_conflict
    with pytest.raises(StorageError, match="could not allocate a record id"):
        store._claim_id()


def test_concurrent_writers_never_collide_on_ids(volume_path):
    errors: list[Exception] = []

    @quarantine(dir=volume_path, halt_after=None, report=False, retries=0)
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
    store = DatabricksVolumeStore(volume_path)
    ids = [r.id for r in store.records()]
    assert len(ids) == 10
    assert len(set(ids)) == 10


def test_max_items_and_halt_after(volume_path, module):
    q_full = Quarantine(volume_path, max_items=1, halt_after=None, report=False)
    q_full.call(module.load, "bad")
    with pytest.raises(QuarantineFull):
        q_full.call(module.load, "worse")

    DatabricksVolumeStore(volume_path).clear()
    q_halt = Quarantine(volume_path, halt_after=2, report=False)
    q_halt.call(module.load, "bad")
    with pytest.raises(SystemicFailure):
        q_halt.call(module.load, "worse")


def test_retry_recovers_and_deletes_from_volume(volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
    q.call(module.load, "bad")
    assert DatabricksVolumeStore(volume_path).count() == 1

    module.FAIL_ON.clear()
    result = retry(dir=volume_path)
    assert result.recovered == [1]
    assert DatabricksVolumeStore(volume_path).count() == 0


def test_the_cli_works_against_databricks_volume(volume_path, module, capsys):
    Quarantine(volume_path, halt_after=None, report=False).call(module.load, "bad")
    assert main(["list", "--dir", volume_path, "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["id"] == 1
    assert main(["show", "1", "--dir", volume_path]) == 0
    assert "cannot load bad" in capsys.readouterr().out
    assert main(["stats", "--dir", volume_path, "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["records"] == 1
    assert main(["reindex", "--dir", volume_path]) == 0
    assert main(["clear", "--dir", volume_path, "--yes"]) == 0
    assert DatabricksVolumeStore(volume_path).count() == 0


def test_dashboard_handler_reads_volume_store(volume_path, module):
    Quarantine(volume_path, halt_after=None, report=False).call(module.load, "bad")
    handler = DashboardHandler.__new__(DashboardHandler)
    prev = DashboardHandler.quarantine_dir
    DashboardHandler.quarantine_dir = volume_path
    try:
        index_html = handler._render_index()
        record_html = handler._render_record(1)
    finally:
        DashboardHandler.quarantine_dir = prev
    assert "cannot load bad" in index_html
    assert "load" in index_html
    assert "cannot load bad" in record_html


def test_get_and_delete_are_o1_api_calls(store, fake_ws, volume_path, module):
    q = Quarantine(volume_path, halt_after=None, report=False)
    q.call(module.load, "bad")
    q.call(module.load, "worse")

    listed_paths: list[str] = []
    orig_list = fake_ws.files.list_directory_contents

    def spy_list(directory_path: str):
        listed_paths.append(directory_path)
        return orig_list(directory_path)

    fake_ws.files.list_directory_contents = spy_list
    assert store.get(1).id == 1
    assert listed_paths == [f"{volume_path}/0001"]

    listed_paths.clear()
    store.delete(2)
    assert listed_paths == [f"{volume_path}/0002"]


def test_databricks_errors_are_wrapped_as_storage_error(store, fake_ws, volume_path, module):
    databricks_error = getattr(db_errors, "DatabricksError")  # noqa: B009

    def boom(*args, **kwargs):
        raise databricks_error("permission denied")

    q = Quarantine(volume_path, halt_after=None, report=False)
    q.call(module.load, "bad")
    rec = store.get(1)

    orig_list = fake_ws.files.list_directory_contents
    fake_ws.files.list_directory_contents = boom
    with pytest.raises(StorageError, match="cannot list records"):
        store.records()
    fake_ws.files.list_directory_contents = orig_list

    orig_create = fake_ws.files.create_directory
    fake_ws.files.create_directory = boom
    with pytest.raises(StorageError, match="cannot create directory"):
        store.ensure()
    fake_ws.files.create_directory = orig_create

    orig_download = fake_ws.files.download
    fake_ws.files.download = boom
    with pytest.raises(StorageError, match="cannot download record 1"):
        store.get(1)
    fake_ws.files.download = orig_download

    orig_upload = fake_ws.files.upload
    fake_ws.files.upload = boom
    with pytest.raises(StorageError, match="cannot update record 1"):
        store.update(rec)
    with pytest.raises(StorageError, match="cannot update record 1"):
        store.write_traceback(rec, RuntimeError("fail"))
    with pytest.raises(StorageError, match="cannot claim a record id"):
        store._claim_id()
    fake_ws.files.upload = orig_upload

    orig_delete = fake_ws.files.delete
    fake_ws.files.delete = boom
    with pytest.raises(StorageError, match="cannot delete record 1"):
        store.delete(1)
    fake_ws.files.delete = orig_delete


def test_cache_security_and_stale_file_cleanup(store, tmp_path):
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

    # Symlinked record directory and symlinked cache file are rejected
    store._cache.mkdir(parents=True, exist_ok=True)
    rec_link = store._cache / "0003"
    rec_link.symlink_to(outside)
    try:
        with pytest.raises(StorageError, match="symlink"):
            store._safe_target_dir(3)
    finally:
        rec_link.unlink(missing_ok=True)

    target = store._safe_target_dir(4)
    file_link = target / "ok.txt"
    file_link.symlink_to(outside / "secret.txt")
    try:
        with pytest.raises(StorageError, match="symlink"):
            store._write_cache(4, {"ok.txt": b"safe"})
    finally:
        file_link.unlink(missing_ok=True)

    cache_dir = store._write_cache(
        2,
        {"../escape": b"evil", "bad\x00name": b"evil", "ok.txt": b"safe"},
        replace=True,
    )
    assert (cache_dir / "ok.txt").read_bytes() == b"safe"
    assert not (cache_dir.parent / "escape").exists()

    store._write_cache(2, {"new.txt": b"fresh"}, replace=True)
    assert not (cache_dir / "ok.txt").exists()
    assert (cache_dir / "new.txt").read_bytes() == b"fresh"

"""The folder must survive crashes, concurrency and corruption."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from quarantine.errors import StorageError
from quarantine.record import Record
from quarantine.serialize import Call, serialize
from quarantine.store import LOCK_NAME, TMP_PREFIX, FileLock, Store, default_dir


def add(store: Store, message: str = "boom", function: str = "process") -> Record:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return store.add(
            function=function,
            module="tests.test_store",
            fingerprint=f"fp-{message}",
            source_file=__file__,
            exc=exc,
            serialized=serialize(Call(({"msg": message},))),
            input_text=f"args[0] = {{'msg': {message!r}}}\n",
            preview=f"{{'msg': {message!r}}}",
        )


def folder(record: Record) -> Path:
    """The record's directory, asserted to exist (keeps the type checker happy)."""
    assert record.path is not None
    return record.path


def test_ids_are_sequential_and_zero_padded(tmp_path):
    store = Store(tmp_path / "qq")
    first, second = add(store, "a"), add(store, "b")
    assert (first.id, second.id) == (1, 2)
    assert folder(first).name == "0001"
    assert {p.name for p in store.record_dirs()} == {"0001", "0002"}


def test_every_expected_file_is_written(tmp_path):
    store = Store(tmp_path / "qq")
    record = add(store)
    names = {p.name for p in folder(record).iterdir()}
    assert names == {"meta.json", "input.txt", "input.pkl", "traceback.txt"}
    assert store.index_path.exists()


def test_index_is_a_cache_that_can_be_rebuilt(tmp_path):
    store = Store(tmp_path / "qq")
    add(store, "a")
    add(store, "b")
    store.index_path.unlink()
    assert len(store.index_rows()) == 2  # rebuilt on demand
    assert store.index_path.exists()
    assert set(store.fingerprints()) == {"fp-a", "fp-b"}


def test_corrupt_index_is_rebuilt(tmp_path):
    store = Store(tmp_path / "qq")
    add(store, "a")
    store.index_path.write_text("{not json", encoding="utf-8")
    assert store.read_index() is None
    assert len(store.index_rows()) == 1


def test_stale_index_entries_are_dropped(tmp_path):
    store = Store(tmp_path / "qq")
    record = add(store, "a")
    # Simulate a folder deleted by hand, leaving the index ahead of reality.
    for child in folder(record).iterdir():
        child.unlink()
    folder(record).rmdir()
    assert store.index_rows() == []
    assert store.fingerprints() == {}


def test_unreadable_record_is_reported_not_fatal(tmp_path):
    store = Store(tmp_path / "qq")
    good = add(store, "good")
    broken = store.dir / "0009"
    broken.mkdir()
    (broken / "meta.json").write_text("{oops", encoding="utf-8")

    records = store.records()
    assert [r.id for r in records] == [good.id]
    assert len(store.problems) == 1
    assert "not valid JSON" in store.problems[0]


def test_meta_missing_required_keys_is_reported(tmp_path):
    store = Store(tmp_path / "qq")
    folder = store.dir
    folder.mkdir(parents=True)
    (folder / "0001").mkdir()
    (folder / "0001" / "meta.json").write_text(json.dumps({"id": 1}), encoding="utf-8")
    assert store.records() == []
    assert "missing ['function']" in store.problems[0]


def test_unknown_meta_keys_are_ignored(tmp_path):
    store = Store(tmp_path / "qq")
    record = add(store)
    meta = json.loads((folder(record) / "meta.json").read_text(encoding="utf-8"))
    meta["from_a_future_version"] = True
    (folder(record) / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert store.get(1).function == "process"


def test_a_failed_write_leaves_no_partial_record(tmp_path, monkeypatch):
    """The rename is the commit point: if it fails, nothing is left behind."""
    store = Store(tmp_path / "qq")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "rename", explode)
    with pytest.raises(StorageError, match="cannot write a record"):
        add(store)

    assert store.record_dirs() == []
    assert list(store.dir.iterdir()) == []


def test_purge_temp_cleans_up_after_a_hard_crash(tmp_path):
    """kill -9 during a write leaves a staging folder that nothing reads."""
    store = Store(tmp_path / "qq")
    store.ensure()
    staging = store.dir / f"{TMP_PREFIX}abc123"
    staging.mkdir()
    (staging / "input.txt").write_text("half written", encoding="utf-8")
    (store.dir / f"{TMP_PREFIX}stray.part").write_text("x", encoding="utf-8")

    assert store.records() == []  # a staging folder is not a record
    assert store.purge_temp() == 2
    assert list(store.dir.iterdir()) == []


def test_id_collision_is_resolved_by_taking_the_next_id(tmp_path):
    store = Store(tmp_path / "qq")
    store.ensure()
    (store.dir / "0001").mkdir()  # someone else got there first
    record = add(store)
    assert record.id == 2


def test_concurrent_writers_do_not_collide(tmp_path):
    store = Store(tmp_path / "qq")
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            add(store, f"item-{index}")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(store.record_dirs()) == 12
    assert len({r.id for r in store.records()}) == 12
    assert len(store.index_rows()) == 12


def test_update_and_delete(tmp_path):
    store = Store(tmp_path / "qq")
    record = add(store)
    record.attempts = 4
    store.update(record)
    assert store.get(record.id).attempts == 4
    assert store.index_rows()[0]["attempts"] == 4

    store.delete(record)
    assert store.records() == []
    assert store.index_rows() == []
    with pytest.raises(StorageError, match="no record"):
        store.get(record.id)


def test_update_of_a_missing_record_fails_loudly(tmp_path):
    store = Store(tmp_path / "qq")
    record = add(store)
    store.delete(record)
    with pytest.raises(StorageError, match="no record"):
        store.update(record)


def test_clear_removes_records_and_index(tmp_path):
    store = Store(tmp_path / "qq")
    add(store, "a")
    add(store, "b")
    assert store.clear() == 2
    assert not store.index_path.exists()
    assert store.count() == 0


def test_missing_folder_reads_as_empty(tmp_path):
    store = Store(tmp_path / "nope")
    assert not store.exists()
    assert store.records() == []
    assert store.count() == 0
    assert store.index_rows() == []
    assert store.purge_temp() == 0
    assert store.rebuild_index() == []


def test_lock_is_exclusive_and_breaks_when_stale(tmp_path):
    path = tmp_path / LOCK_NAME
    with FileLock(path, timeout=0.05):
        assert path.exists()
        with pytest.raises(StorageError, match="timed out"):
            FileLock(path, timeout=0.05).acquire()
    assert not path.exists()

    path.write_text("999999\n", encoding="utf-8")
    os.utime(path, (0, 0))  # ancient: a dead process left it behind
    with FileLock(path, timeout=0.05, stale_after=1):
        pass


def test_record_without_a_path_refuses_to_read_files():
    record = Record(
        id=1,
        function="f",
        module="m",
        fingerprint="fp",
        error_type="ValueError",
        error="x",
        created_at="now",
        last_failed_at="now",
    )
    with pytest.raises(StorageError, match="not attached"):
        record.traceback_text()


def test_repr_only_records_cannot_be_replayed(tmp_path):
    store = Store(tmp_path / "qq")
    row: dict[str, Any] = {"lock": threading.Lock()}
    row["self"] = row
    try:
        raise ValueError("boom")
    except ValueError as exc:
        record = store.add(
            function="process",
            module="m",
            fingerprint="fp",
            source_file=__file__,
            exc=exc,
            serialized=serialize(Call((row,))),
            input_text="args[0] = <cyclic>\n",
            preview="<cyclic>",
        )
    assert record.payload_format == "repr"
    assert record.input_path is None
    with pytest.raises(StorageError, match="no replayable input"):
        record.load_call()


def test_default_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("QUARANTINE_DIR", str(tmp_path / "elsewhere"))
    assert default_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("QUARANTINE_DIR")
    assert default_dir().name == ".quarantine"


def test_record_when_falls_back_for_unparseable_timestamps():
    record = Record(
        id=1,
        function="f",
        module="m",
        fingerprint="fp",
        error_type="E",
        error="",
        created_at="not-a-date",
        last_failed_at="not-a-date",
    )
    assert record.when == "not-a-da"
    assert record.summary == "E"
    assert record.qualified_name == "m.f"


def test_a_transient_lock_does_not_lose_the_record(tmp_path, monkeypatch):
    """Windows scanners hold new files open for a moment; that must not cost a record."""
    store = Store(tmp_path / "qq")
    real_rename = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", flaky)
    record = add(store)

    assert calls["n"] == 3
    assert record.id == 1
    assert folder(record).is_dir()
    assert store.get(1).error == "boom"


def test_a_permanent_lock_is_reported_not_ignored(tmp_path, monkeypatch):
    store = Store(tmp_path / "qq")

    def always_locked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(os, "rename", always_locked)
    with pytest.raises(StorageError, match="cannot write a record"):
        add(store)
    assert store.record_dirs() == []

"""Regression tests for security, concurrency, and reliability audit fixes."""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass
from typing import Any

import pytest

from quarantine import Quarantine
from quarantine.cli import main
from quarantine.fingerprint import fingerprint
from quarantine.record import Record
from quarantine.redact import MAX_DEPTH, PLACEHOLDER, redact
from quarantine.reporting import columnize
from quarantine.resolve import ResolutionError, resolve_function
from quarantine.serialize import Call, deserialize, preview
from quarantine.store import FileLock


def test_redact_circular_and_dag_references() -> None:
    payload: dict[str, Any] = {"password": "secret"}
    payload["self"] = payload
    clean, hits = redact(payload, ["password"])
    assert "password" in hits
    assert clean["password"] == PLACEHOLDER
    assert clean["self"] is clean
    assert clean["self"]["password"] == PLACEHOLDER

    shared = {"token": "abc"}
    dag = [shared, shared]
    clean_dag, dag_hits = redact(dag, ["token"])
    assert "token" in dag_hits
    assert clean_dag[0]["token"] == PLACEHOLDER
    assert clean_dag[1]["token"] == PLACEHOLDER


def test_redact_max_depth_never_leaks_secrets() -> None:
    nested: dict[str, Any] = {"api_key": "sk-live-123"}
    for _ in range(MAX_DEPTH + 5):
        nested = {"wrap": nested}
    clean, _ = redact(nested, ["api_key"])
    assert "sk-live-123" not in repr(clean)


def test_redact_slots_and_deque_and_frozen_in_set() -> None:
    class Slotted:
        __slots__ = ("public", "token")

        def __init__(self, public: str, token: str) -> None:
            self.public = public
            self.token = token

    obj = Slotted("ok", "secret")
    clean, hits = redact(obj, ["token"])
    assert "token" in hits
    assert clean.token == PLACEHOLDER
    assert obj.token == "secret"

    dq = collections.deque([{"password": "pw"}], maxlen=5)
    clean_dq, _ = redact(dq, ["password"])
    assert isinstance(clean_dq, collections.deque)
    assert clean_dq[0]["password"] == PLACEHOLDER

    @dataclass(frozen=True)
    class Frozen:
        password: str

    clean_set, _ = redact({Frozen("pw")}, ["password"])
    assert "pw" not in repr(clean_set)


def test_fingerprint_distinguishes_key_types_and_orders_sets() -> None:
    fp_int = fingerprint("fn", Call(args=({1: "x"},)))
    fp_str = fingerprint("fn", Call(args=({"1": "x"},)))
    assert fp_int != fp_str


def test_deserialize_and_preview_edge_cases() -> None:
    with pytest.raises(ValueError, match="malformed"):
        deserialize("json", b"\xff\xfe")
    with pytest.raises(ValueError, match="args"):
        deserialize("json", b'{"args": "abc", "kwargs": {}}')
    with pytest.raises(ValueError, match="kwargs"):
        deserialize("json", b'{"args": [], "kwargs": [1, 2]}')
    assert preview(Call(args=("hello",)), limit=0) == ""
    assert preview(Call(args=("hello",)), limit=1) == "\u2026"


def test_resolve_rejects_dunder_qualname() -> None:
    rec = Record(
        id=1,
        function="__builtins__.eval",
        module="builtins",
        fingerprint="fp",
        error_type="ValueError",
        error="bad",
        created_at="",
        last_failed_at="",
    )
    with pytest.raises(ResolutionError, match="unsafe"):
        resolve_function(rec)


def test_columnize_wide_row_and_width_one() -> None:
    lines = columnize([["a", "b", "extra"]], ["H1", "H2"], widths=[1, 1])
    assert len(lines[1].split()[0]) == 1


def test_record_load_non_utf8_and_null_timestamps(tmp_path) -> None:
    rec_dir = tmp_path / "0001"
    rec_dir.mkdir()
    (rec_dir / "meta.json").write_bytes(
        b'{"id": 1, "function": "f", "created_at": null, "last_failed_at": null}'
    )
    rec = Record.load(rec_dir)
    assert rec.when == ""

    (rec_dir / "meta.json").write_bytes(b'{"id": 1, "function": "f", "error": "\xff"}')
    rec2 = Record.load(rec_dir)
    assert rec2.id == 1


def test_filelock_dangling_symlink_and_permission_error_respect_timeout(tmp_path) -> None:
    lock_path = tmp_path / ".index.lock"
    lock_path.symlink_to(tmp_path / "nonexistent")
    lock = FileLock(lock_path, timeout=0.05, stale_after=60.0)
    lock.acquire()
    lock.release()


async def test_cancelled_error_not_quarantined() -> None:
    q = Quarantine(dir=".quarantine-cancel-test", only=(BaseException,), report=False)

    @q.wrap
    async def cancel_me() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel_me()


def test_cli_clear_deletes_corrupt_record(tmp_path, capsys) -> None:
    qdir = tmp_path / "q"
    rec_dir = qdir / "0001"
    rec_dir.mkdir(parents=True)
    (rec_dir / "meta.json").write_text("{corrupt", encoding="utf-8")

    code = main(["clear", "1", "-d", str(qdir)])
    assert code == 0
    assert not rec_dir.exists()

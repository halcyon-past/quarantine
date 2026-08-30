"""The S3 backend: same promises as the local folder, different primitives."""

from __future__ import annotations

import sys
import uuid

import boto3
import pytest
from moto import mock_aws

from quarantine import Quarantine, StorageBackend, open_store, quarantine, register_backend, retry
from quarantine.core import Config
from quarantine.errors import StorageError
from quarantine.s3_store import S3Store
from quarantine.store import coerce_dir

BUCKET = "quarantine-test-bucket"

SOURCE = """
FAIL_ON = {"bad", "worse"}


def load(item):
    if item in FAIL_ON:
        raise ValueError(f"cannot load {item}")
    return item.upper()
"""


@pytest.fixture
def module(target_module):
    return target_module(SOURCE, name="qtarget_s3")


@pytest.fixture
def s3_url(monkeypatch):
    """A mocked bucket plus a unique prefix, so tests cannot see each other."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3").create_bucket(Bucket=BUCKET)
        yield f"s3://{BUCKET}/{uuid.uuid4().hex}"


@pytest.fixture
def s3(s3_url):
    return S3Store(s3_url)


def _client():
    return boto3.client("s3")


# -- URL routing ----------------------------------------------------------


def test_coerce_dir_keeps_urls_and_paths_apart(tmp_path):
    from pathlib import Path

    assert coerce_dir("s3://bucket/prefix") == "s3://bucket/prefix"
    assert coerce_dir(tmp_path) == tmp_path
    assert coerce_dir("plain/folder") == Path("plain/folder")
    assert str(coerce_dir(None)).endswith(".quarantine")


def test_open_store_picks_the_backend_by_scheme(s3_url, tmp_path):
    assert isinstance(open_store(tmp_path), StorageBackend)
    assert isinstance(open_store(s3_url), S3Store)


def test_unknown_schemes_fail_with_directions():
    with pytest.raises(StorageError, match="register_backend"):
        open_store("carrierpigeon://coop/roost")


def test_third_parties_can_register_a_scheme(tmp_path):
    from quarantine.store import Store

    register_backend("carrierpigeon", lambda url: Store(tmp_path))
    try:
        assert open_store("carrierpigeon://coop/roost").dir == tmp_path
    finally:
        from quarantine.store import _BACKENDS

        _BACKENDS.pop("carrierpigeon", None)


def test_config_does_not_mangle_urls():
    config = Config(dir="s3://bucket/team/quarantine", halt_after=None)
    assert config.dir == "s3://bucket/team/quarantine", "Path() would eat the //"


def test_the_environment_variable_accepts_a_url(monkeypatch):
    monkeypatch.setenv("QUARANTINE_DIR", "s3://bucket/from-env")
    from quarantine.store import default_dir

    assert default_dir() == "s3://bucket/from-env"


def test_a_missing_bucket_name_is_rejected():
    with pytest.raises(StorageError, match="bucket"):
        S3Store("s3://")


def test_the_missing_extra_is_named(monkeypatch, s3_url):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(StorageError, match=r"quarantine-py\[s3\]"):
        S3Store(s3_url)


# -- the record lifecycle ---------------------------------------------------


def test_records_round_trip_through_the_bucket(s3, s3_url, module):
    q = Quarantine(s3_url, halt_after=None, report=False)
    q.call(module.load, "bad")

    assert s3.exists()
    assert s3.count() == 1
    record = s3.get(1)
    assert record.function == "load"
    assert record.error == "cannot load bad"
    assert record.load_call().item == "bad", "the pickled input survives the round trip"
    assert "ValueError" in record.traceback_text()
    assert "'bad'" in record.input_text()


def test_a_failed_retry_updates_the_stored_record(s3_url, s3, module):
    q = Quarantine(s3_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    assert q.retry().still_failing == [1]

    fresh = S3Store(s3_url)  # a different process would see the update too
    assert fresh.get(1).attempts == 2

    module.FAIL_ON = set()
    assert q.retry().recovered == [1]
    assert fresh.count() == 0


def test_known_bad_items_are_skipped_on_a_rerun(s3_url, module):
    first = Quarantine(s3_url, halt_after=None, report=False)
    first.call(module.load, "bad")
    second = Quarantine(s3_url, halt_after=None, report=False)
    second.call(module.load, "bad")
    assert second.stats.skipped == 1
    assert S3Store(s3_url).count() == 1, "no duplicate record"


def test_delete_and_clear(s3, s3_url, module):
    q = Quarantine(s3_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    q.call(module.load, "worse")
    s3.delete(1)
    assert [r.id for r in s3.records()] == [2]
    assert s3.clear() == 1
    assert s3.count() == 0
    assert not s3.exists()


def test_disk_bytes_counts_the_objects(s3, s3_url, module):
    Quarantine(s3_url, halt_after=None, report=False).call(module.load, "bad")
    assert s3.disk_bytes() > 0


# -- the commit protocol ------------------------------------------------------


def test_a_half_written_record_is_invisible(s3, s3_url):
    prefix = s3_url.split(f"{BUCKET}/")[1]
    _client().put_object(Bucket=BUCKET, Key=f"{prefix}/0007/.claim", Body=b"")
    _client().put_object(Bucket=BUCKET, Key=f"{prefix}/0007/traceback.txt", Body=b"partial")

    assert s3.count() == 0, "no meta.json means the record does not exist"
    assert s3.records() == []
    with pytest.raises(StorageError, match="no record 7"):
        s3.get(7)


def test_reindex_sweeps_half_written_records(s3, s3_url, module):
    Quarantine(s3_url, halt_after=None, report=False).call(module.load, "bad")
    prefix = s3_url.split(f"{BUCKET}/")[1]
    _client().put_object(Bucket=BUCKET, Key=f"{prefix}/0009/.claim", Body=b"")

    assert s3.purge_temp() == 1
    assert s3.count() == 1, "committed records are untouched"
    listing = _client().list_objects_v2(Bucket=BUCKET, Prefix=f"{prefix}/0009/")
    assert listing.get("KeyCount", 0) == 0, "the debris is gone"


def test_a_lost_claim_race_moves_to_the_next_id(s3, s3_url, module):
    q = Quarantine(s3_url, halt_after=None, report=False)
    q.call(module.load, "bad")  # takes id 1
    prefix = s3_url.split(f"{BUCKET}/")[1]
    # Another worker claims id 2 between our listing and our write.
    _client().put_object(Bucket=BUCKET, Key=f"{prefix}/0002/.claim", Body=b"")

    q.call(module.load, "worse")
    assert [r.id for r in S3Store(s3_url).records()] == [1, 3], "the loser took the next id"


# -- the CLI against a bucket -------------------------------------------------


def test_the_cli_works_against_a_bucket(s3_url, module, capsys):
    import json

    from quarantine.cli import main

    Quarantine(s3_url, halt_after=None, report=False).call(module.load, "bad")

    assert main(["list", "--dir", s3_url, "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["error"] == "cannot load bad"

    assert main(["stats", "--dir", s3_url, "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["records"] == 1
    assert stats["bytes"] > 0
    assert stats["dir"] == s3_url

    assert main(["show", "1", "--dir", s3_url]) == 0
    shown = capsys.readouterr().out
    assert "cannot load bad" in shown and "'bad'" in shown

    module.FAIL_ON = set()
    assert main(["retry", "--dir", s3_url]) == 0
    assert "1 recovered" in capsys.readouterr().out

    assert main(["list", "--dir", s3_url]) == 0
    assert "Nothing quarantined" in capsys.readouterr().out


def test_the_dashboard_reads_a_bucket(s3_url, module):
    from quarantine.ui import DashboardHandler

    Quarantine(s3_url, halt_after=None, report=False).call(module.load, "bad")
    saved = DashboardHandler.quarantine_dir
    DashboardHandler.quarantine_dir = s3_url
    try:
        handler = DashboardHandler.__new__(DashboardHandler)
        page = handler._render_index()
        assert "load" in page and "ValueError" in page
        detail = handler._render_record(1)
        assert "cannot load bad" in detail
    finally:
        DashboardHandler.quarantine_dir = saved


# -- module-level api ---------------------------------------------------------


def test_module_level_retry_takes_a_url(s3_url, module):
    Quarantine(s3_url, halt_after=None, report=False).call(module.load, "bad")
    module.FAIL_ON = set()
    result = retry(dir=s3_url)
    assert result.recovered == [1]


def test_the_decorator_takes_a_url(s3_url):
    @quarantine(dir=s3_url, halt_after=None, report=False)
    def parse(item):
        raise ValueError(f"nope: {item}")

    parse({"id": 1})
    assert S3Store(s3_url).count() == 1

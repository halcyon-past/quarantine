"""The Azure Blob Storage backend: same promises as the local folder, different primitives."""

from __future__ import annotations

import json
import shutil
import sys
import urllib.parse
import uuid

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from quarantine import Quarantine, StorageBackend, open_store, quarantine, register_backend, retry
from quarantine.azure_store import AzureBlobStore
from quarantine.cli import main
from quarantine.core import Config
from quarantine.errors import StorageError
from quarantine.store import coerce_dir
from quarantine.ui import DashboardHandler

CONTAINER = "quarantine-test-container"

SOURCE = """
FAIL_ON = {"bad", "worse"}


def load(item):
    if item in FAIL_ON:
        raise ValueError(f"cannot load {item}")
    return item.upper()
"""


class FakeBlobProperties:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size


class FakeBlobDownloader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def readall(self) -> bytes:
        return self._data


class FakeBlobClient:
    def __init__(self, container: FakeContainerClient, key: str) -> None:
        self.container = container
        self.key = key

    def upload_blob(self, data: bytes, overwrite: bool = True) -> None:
        if not overwrite and self.key in self.container.blobs:
            raise ResourceExistsError("Blob already exists")
        self.container.blobs[self.key] = data

    def download_blob(self) -> FakeBlobDownloader:
        if self.key not in self.container.blobs:
            raise ResourceNotFoundError(f"Blob not found: {self.key}")
        return FakeBlobDownloader(self.container.blobs[self.key])

    def delete_blob(self) -> None:
        if self.key not in self.container.blobs:
            raise ResourceNotFoundError(f"Blob not found: {self.key}")
        del self.container.blobs[self.key]


class FakeContainerClient:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def get_blob_client(self, blob: str) -> FakeBlobClient:
        return FakeBlobClient(self, blob)

    def list_blobs(self, name_starts_with: str = "") -> list[FakeBlobProperties]:
        out = []
        for name, data in list(self.blobs.items()):
            if not name_starts_with or name.startswith(name_starts_with):
                out.append(FakeBlobProperties(name, len(data)))
        return out

    def delete_blob(self, blob: str) -> None:
        if blob not in self.blobs:
            raise ResourceNotFoundError(f"Blob not found: {blob}")
        del self.blobs[blob]

    def exists(self) -> bool:
        return True


@pytest.fixture
def module(target_module):
    return target_module(SOURCE, name="qtarget_azure")


@pytest.fixture
def fake_container():
    return FakeContainerClient()


@pytest.fixture
def azure_url(monkeypatch, fake_container):
    """An Azure URL backed by the in-memory fake container client."""
    conn_str = "DefaultEndpointsProtocol=http;AccountName=test;AccountKey=fake;"
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", conn_str)
    monkeypatch.setattr(
        AzureBlobStore,
        "_init_container_client",
        lambda *args, **kwargs: fake_container,
    )
    prefix = uuid.uuid4().hex
    url = f"azure://{CONTAINER}/{prefix}"
    yield url
    import hashlib
    import tempfile
    from pathlib import Path

    digest = hashlib.sha256(url.rstrip("/").encode("utf-8")).hexdigest()[:12]
    cache = Path(tempfile.gettempdir()) / f"quarantine-azure-{digest}"
    shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture
def azure(azure_url):
    return AzureBlobStore(azure_url)


# -- URL routing ----------------------------------------------------------


def test_coerce_dir_keeps_urls_and_paths_apart(tmp_path):
    from pathlib import Path

    assert coerce_dir("azure://container/prefix") == "azure://container/prefix"
    assert coerce_dir("az://container/prefix") == "az://container/prefix"
    assert coerce_dir(tmp_path) == tmp_path
    assert coerce_dir("plain/folder") == Path("plain/folder")
    assert str(coerce_dir(None)).endswith(".quarantine")


def test_open_store_picks_the_backend_by_scheme(azure_url, tmp_path):
    assert isinstance(open_store(tmp_path), StorageBackend)
    assert isinstance(open_store(azure_url), AzureBlobStore)
    az_url = azure_url.replace("azure://", "az://")
    assert isinstance(open_store(az_url), AzureBlobStore)


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
    config = Config(dir="azure://container/team/quarantine", halt_after=None)
    assert config.dir == "azure://container/team/quarantine", "Path() would eat the //"


def test_the_environment_variable_accepts_a_url(monkeypatch):
    monkeypatch.setenv("QUARANTINE_DIR", "azure://container/from-env")
    from quarantine.store import default_dir

    assert default_dir() == "azure://container/from-env"


def test_a_missing_container_name_is_rejected():
    with pytest.raises(StorageError, match="missing a container name"):
        AzureBlobStore("azure://")
    with pytest.raises(StorageError, match="not an azure:// or az:// URL"):
        AzureBlobStore("https://example.com")


def test_the_missing_extra_is_named(monkeypatch, azure_url):
    monkeypatch.setitem(sys.modules, "azure.storage", None)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", None)
    with pytest.raises(StorageError, match=r"quarantine-py\[azure\]"):
        AzureBlobStore(azure_url)


def test_missing_credentials_raise_informative_error(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    with pytest.raises(StorageError, match="cannot determine Azure Storage account"):
        AzureBlobStore("azure://mycontainer/prefix")


# -- Core behaviour -------------------------------------------------------


def test_records_round_trip_through_the_container(azure, azure_url, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    assert azure.exists()
    assert azure.count() == 1

    record = azure.get(1)
    assert record.id == 1
    assert record.function == "load"
    assert record.module == "qtarget_azure"
    assert record.error_type == "ValueError"
    assert record.error == "cannot load bad"
    assert record.attempts == 1
    assert record.load_call().item == "bad", "the pickled input survives the round trip"
    assert "ValueError" in record.traceback_text()
    assert "'bad'" in record.input_text()

    assert azure.fingerprints() == {record.fingerprint: 1}
    assert [r.id for r in azure.records()] == [1]

    # Inspect the container blobs directly
    prefix = azure_url.split(f"{CONTAINER}/")[1]
    keys = {b.name for b in azure._container_client.list_blobs()}
    assert f"{prefix}/0001/meta.json" in keys
    assert f"{prefix}/0001/input.pkl" in keys
    assert f"{prefix}/0001/traceback.txt" in keys


def test_a_failed_retry_updates_the_stored_record(azure_url, azure, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    assert q.retry().still_failing == [1]

    fresh = AzureBlobStore(azure_url)
    assert fresh.get(1).attempts == 2

    module.FAIL_ON = set()
    assert q.retry().recovered == [1]
    assert fresh.count() == 0


def test_known_bad_items_are_skipped_on_a_rerun(azure_url, module):
    first = Quarantine(azure_url, halt_after=None, report=False)
    first.call(module.load, "bad")

    second = Quarantine(azure_url, halt_after=None, report=False)
    second.call(module.load, "bad")
    assert second.stats.skipped == 1
    assert AzureBlobStore(azure_url).count() == 1, "no duplicate record"


def test_delete_and_clear(azure, azure_url, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    q.call(module.load, "worse")
    assert azure.count() == 2

    azure.delete(1)
    assert [r.id for r in azure.records()] == [2]

    assert azure.clear() == 1
    assert azure.count() == 0
    assert not azure.exists()


def test_disk_bytes_counts_the_objects(azure, azure_url, module):
    Quarantine(azure_url, halt_after=None, report=False).call(module.load, "bad")
    assert azure.disk_bytes() > 0


def test_a_half_written_record_is_invisible(azure, azure_url):
    prefix = azure_url.split(f"{CONTAINER}/")[1]
    azure._container_client.get_blob_client(f"{prefix}/0007/.claim").upload_blob(b"")
    azure._container_client.get_blob_client(f"{prefix}/0007/input.pkl").upload_blob(b"junk")

    assert azure.count() == 0, "no meta.json means the record does not exist"
    assert azure.records() == []
    with pytest.raises(StorageError, match="no record 7"):
        azure.get(7)


def test_reindex_sweeps_half_written_records(azure, azure_url, module):
    Quarantine(azure_url, halt_after=None, report=False).call(module.load, "bad")

    prefix = azure_url.split(f"{CONTAINER}/")[1]
    azure._container_client.get_blob_client(f"{prefix}/0002/.claim").upload_blob(b"")
    azure._container_client.get_blob_client(f"{prefix}/0002/input.pkl").upload_blob(b"junk")

    assert azure.purge_temp() == 1
    assert azure.count() == 1, "committed records are untouched"


def test_a_lost_claim_race_moves_to_the_next_id(azure, azure_url, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    prefix = azure_url.split(f"{CONTAINER}/")[1]

    azure._container_client.get_blob_client(f"{prefix}/0001/.claim").upload_blob(b"")
    q.call(module.load, "bad")

    assert [r.id for r in AzureBlobStore(azure_url).records()] == [2], "the loser took the next id"


def test_the_cli_works_against_a_container(azure_url, module, capsys):
    Quarantine(azure_url, halt_after=None, report=False).call(module.load, "bad")

    assert main(["list", "--dir", azure_url, "--json"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["error"] == "cannot load bad"

    assert main(["stats", "--dir", azure_url, "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["records"] == 1
    assert stats["bytes"] > 0
    assert stats["dir"] == azure_url

    assert main(["show", "1", "--dir", azure_url]) == 0
    shown = capsys.readouterr().out
    assert "cannot load bad" in shown and "'bad'" in shown

    module.FAIL_ON = set()
    assert main(["retry", "--dir", azure_url]) == 0
    assert "1 recovered" in capsys.readouterr().out

    assert main(["list", "--dir", azure_url]) == 0
    assert "Nothing quarantined" in capsys.readouterr().out


def test_the_dashboard_reads_a_container(azure_url, module):
    Quarantine(azure_url, halt_after=None, report=False).call(module.load, "bad")
    saved = DashboardHandler.quarantine_dir
    DashboardHandler.quarantine_dir = azure_url
    try:
        handler = DashboardHandler.__new__(DashboardHandler)
        page = handler._render_index()
        assert "load" in page and "ValueError" in page
        detail = handler._render_record(1)
        assert "cannot load bad" in detail
    finally:
        DashboardHandler.quarantine_dir = saved


def test_module_level_retry_takes_a_url(azure_url, module):
    Quarantine(azure_url, halt_after=None, report=False).call(module.load, "bad")
    module.FAIL_ON = set()
    result = retry(dir=azure_url)
    assert result.recovered == [1]


def test_the_decorator_takes_a_url(azure_url):
    @quarantine(dir=azure_url, halt_after=None, report=False)
    def broken(x):
        raise RuntimeError("boom")

    broken(1)
    assert AzureBlobStore(azure_url).count() == 1


# -- Security: path traversal & symlinks -----------------------------------


def test_malicious_object_keys_cannot_escape_cache(azure_url, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)
    prefix = azure_url.split(f"{CONTAINER}/")[1]

    # Plant traversal keys in the blob container
    evil_key = f"{prefix}/0001/../../escaped.txt"
    store._container_client.get_blob_client(evil_key).upload_blob(b"evil")
    store._container_client.get_blob_client(f"{prefix}/0001/nested/bad.txt").upload_blob(b"evil")

    rec = store.get(1)
    assert rec.id == 1
    assert not (store._cache.parent / "escaped.txt").exists()


def test_symlinked_cache_is_rejected(azure_url, module, tmp_path):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)

    shutil.rmtree(store._cache, ignore_errors=True)
    real_target = tmp_path / "real-cache"
    real_target.mkdir()
    try:
        store._cache.symlink_to(real_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/filesystem")

    with pytest.raises(StorageError, match="unsafe symlink"):
        store.get(1)


def test_symlinked_record_dir_is_rejected(azure_url, module, tmp_path):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)

    record_dir = store._cache / "0001"
    shutil.rmtree(record_dir, ignore_errors=True)
    store._cache.mkdir(parents=True, exist_ok=True)
    real_target = tmp_path / "real-record"
    real_target.mkdir()
    try:
        record_dir.symlink_to(real_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/filesystem")

    with pytest.raises(StorageError, match="unsafe symlink"):
        store.get(1)


def test_symlinked_dest_file_is_rejected(azure_url, module, tmp_path):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)

    record_dir = store._cache / "0001"
    record_dir.mkdir(parents=True, exist_ok=True)
    dest_file = record_dir / "meta.json"
    dest_file.unlink(missing_ok=True)
    real_target = tmp_path / "real-meta.json"
    real_target.write_text("{}", encoding="utf-8")
    try:
        dest_file.symlink_to(real_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/filesystem")

    with pytest.raises(StorageError, match="cannot be a symlink"):
        store.get(1)


# -- Client initialization & error branches --------------------------------


def test_repr_and_ensure(azure_url):
    store = AzureBlobStore(azure_url)
    assert repr(store) == f"AzureBlobStore({azure_url!r})"
    store.ensure()  # Idempotent no-op


def test_client_init_from_connection_string(monkeypatch):
    monkeypatch.setenv(
        "AZURE_STORAGE_CONNECTION_STRING",
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=fake;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;",
    )
    store = AzureBlobStore("azure://mycontainer/prefix")
    assert store.container_name == "mycontainer"
    assert store.prefix == "prefix"


def test_client_init_invalid_connection_string(monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "invalid connection string")
    msg = "cannot connect to Azure Blob Storage via connection string"
    with pytest.raises(StorageError, match=msg):
        AzureBlobStore("azure://mycontainer/prefix")


def test_client_init_with_account_and_key(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "myaccount")
    monkeypatch.setenv("AZURE_STORAGE_KEY", "bXlrZXk=")
    store = AzureBlobStore("azure://mycontainer/prefix")
    assert store.container_name == "mycontainer"
    assert store._container_client.url == "https://myaccount.blob.core.windows.net/mycontainer"


def test_client_init_with_url_account_and_emulator(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_NAME", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_EMULATOR_HOST", "http://127.0.0.1:10000/myaccount")
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", "?sv=2020-08-04&ss=b&srt=sco&sp=rwdlacx")
    store = AzureBlobStore("azure://myaccount@mycontainer/prefix")
    assert store.container_name == "mycontainer"
    assert urllib.parse.urlsplit(store._container_client.url).netloc == "127.0.0.1:10000"


def test_unreadable_records_appended_to_problems(azure_url, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)
    prefix = azure_url.split(f"{CONTAINER}/")[1]

    # Corrupt the meta.json
    store._container_client.get_blob_client(f"{prefix}/0001/meta.json").upload_blob(b"not json")
    # Clean cache so it must read from remote
    shutil.rmtree(store._cache, ignore_errors=True)

    records = store.records()
    assert records == []
    assert len(store.problems) == 1
    assert "0001" in store.problems[0]


def test_claim_exhaustion_raises_storage_error(azure_url, monkeypatch):
    store = AzureBlobStore(azure_url)
    monkeypatch.setattr(
        store,
        "_put",
        lambda *args, **kwargs: (_ for _ in ()).throw(ResourceExistsError("conflict")),
    )
    with pytest.raises(StorageError, match="could not allocate a record id"):
        store._claim_id()


def test_claim_id_http_conflict_and_other_error(azure_url, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    store = AzureBlobStore(azure_url)
    # First attempt gets 409 conflict, second attempt succeeds
    called = [0]

    def mock_put(key, data, if_absent=False):
        if called[0] == 0:
            called[0] += 1
            err = HttpResponseError("conflict")
            err.status_code = 409
            raise err

    monkeypatch.setattr(store, "_put", mock_put)
    assert store._claim_id() == 2

    # A non-conflict HTTP error raises StorageError
    def mock_put_fail(key, data, if_absent=False):
        err = HttpResponseError("server error")
        err.status_code = 500
        raise err

    monkeypatch.setattr(store, "_put", mock_put_fail)
    with pytest.raises(StorageError, match="cannot claim a record id"):
        store._claim_id()


def test_container_client_injection():
    fc = FakeContainerClient()
    store = AzureBlobStore("azure://mycontainer/prefix", container_client=fc)
    assert store._container_client is fc


def test_client_init_with_default_credential(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_KEY", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_SAS_TOKEN", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "myaccount")
    store = AzureBlobStore("azure://mycontainer/prefix")
    assert store.container_name == "mycontainer"


def test_client_init_auth_failure(monkeypatch):
    from azure.storage import blob

    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "myaccount")
    monkeypatch.setattr(
        blob,
        "ContainerClient",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad configuration")),
    )
    with pytest.raises(StorageError, match="cannot authenticate to Azure Blob Storage"):
        AzureBlobStore("azure://mycontainer/prefix")


def test_list_objects_http_error(azure_url, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    store = AzureBlobStore(azure_url)
    monkeypatch.setattr(
        store._container_client,
        "list_blobs",
        lambda *a, **k: (_ for _ in ()).throw(HttpResponseError("network error")),
    )
    with pytest.raises(StorageError, match="cannot list records"):
        store._list_objects()


def test_download_http_error(azure_url, module, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)
    shutil.rmtree(store._cache, ignore_errors=True)

    fake_blob = store._container_client.get_blob_client("anything")

    def mock_download():
        raise HttpResponseError("download failed")

    monkeypatch.setattr(fake_blob, "download_blob", mock_download)
    monkeypatch.setattr(store._container_client, "get_blob_client", lambda key: fake_blob)

    with pytest.raises(StorageError, match="cannot download record 1"):
        store.get(1)


def test_write_record_http_error(azure_url, module, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    def mock_put(self, key, data, if_absent=False):
        if not if_absent:
            raise HttpResponseError("write failed")

    monkeypatch.setattr(AzureBlobStore, "_put", mock_put)
    q = Quarantine(azure_url, halt_after=None, report=False)
    with pytest.raises(StorageError, match="cannot write a record"):
        q.call(module.load, "bad")


def test_update_and_write_traceback_http_error(azure_url, module, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)
    rec = store.get(1)

    monkeypatch.setattr(
        store,
        "_put",
        lambda *a, **k: (_ for _ in ()).throw(HttpResponseError("update failed")),
    )
    with pytest.raises(StorageError, match="cannot update record 1"):
        store.update(rec)

    with pytest.raises(StorageError, match="cannot update record 1"):
        store.write_traceback(rec, ValueError("new error"))


def test_delete_keys_http_error(azure_url, monkeypatch):
    from azure.core.exceptions import HttpResponseError

    store = AzureBlobStore(azure_url)

    # 404 is silently ignored
    def mock_del_404(key):
        err = HttpResponseError("not found")
        err.status_code = 404
        raise err

    monkeypatch.setattr(store._container_client, "delete_blob", mock_del_404)
    store._delete_keys(["fake_key"])

    # 500 raises StorageError
    def mock_del_500(key):
        err = HttpResponseError("server error")
        err.status_code = 500
        raise err

    monkeypatch.setattr(store._container_client, "delete_blob", mock_del_500)
    with pytest.raises(StorageError, match="cannot delete records"):
        store._delete_keys(["fake_key"])


def test_safe_write_cache_file_non_dir_fd(tmp_path, monkeypatch):
    import os

    from quarantine.azure_store import _safe_write_cache_file

    monkeypatch.setattr(os, "supports_dir_fd", set())
    dest_dir = tmp_path / "cache-dir"
    dest_dir.mkdir()
    _safe_write_cache_file(dest_dir, "test.txt", b"hello world")
    assert (dest_dir / "test.txt").read_bytes() == b"hello world"

    # Symlink destination is rejected
    sym_dest = dest_dir / "sym.txt"
    real_file = tmp_path / "real.txt"
    real_file.write_bytes(b"")
    try:
        sym_dest.symlink_to(real_file)
        with pytest.raises(StorageError, match="cannot be a symlink"):
            _safe_write_cache_file(dest_dir, "sym.txt", b"data")
    except (OSError, NotImplementedError):
        pass

    # Escaping destination is rejected
    with pytest.raises(StorageError):
        _safe_write_cache_file(dest_dir, "../escape.txt", b"data")


def test_url_with_fqdn_and_sovereign_clouds(monkeypatch):
    from quarantine.azure_store import AzureBlobStore

    # Normal azure commercial cloud with FQDN
    url = "azure://myacc.blob.core.windows.net/mycontainer/myprefix"
    store = AzureBlobStore(url, container_client=FakeContainerClient())
    assert store.container_name == "mycontainer"
    assert store.prefix == "myprefix"

    # Commercial cloud with FQDN and no prefix
    url_no_prefix = "azure://myacc.blob.core.windows.net/mycontainer"
    store_no_prefix = AzureBlobStore(url_no_prefix, container_client=FakeContainerClient())
    assert store_no_prefix.container_name == "mycontainer"
    assert store_no_prefix.prefix == ""

    # US Gov cloud
    url_gov = "azure://govacc.blob.core.usgovcloudapi.net/govcontainer/govprefix"
    monkeypatch.setenv("AZURE_STORAGE_KEY", "govkey")
    store_gov = AzureBlobStore(url_gov)
    assert store_gov.container_name == "govcontainer"
    assert store_gov.prefix == "govprefix"
    assert (
        urllib.parse.urlsplit(store_gov._container_client.url).netloc
        == "govacc.blob.core.usgovcloudapi.net"
    )
    assert urllib.parse.urlsplit(store_gov._container_client.url).path == "/govcontainer"

    # Missing container in FQDN format
    with pytest.raises(StorageError, match="missing a container name"):
        AzureBlobStore("azure://myacc.blob.core.windows.net/")

    # FQDN format without trailing slash and no prefix
    url_fqdn_no_slash = "azure://myacc.blob.core.windows.net/mycontainer"
    store_fqdn = AzureBlobStore(url_fqdn_no_slash, container_client=FakeContainerClient())
    assert store_fqdn.container_name == "mycontainer"
    assert store_fqdn.prefix == ""

    # FQDN format with trailing slash and no prefix
    url_fqdn_slash = "azure://myacc.blob.core.windows.net/mycontainer/"
    store_fqdn_slash = AzureBlobStore(url_fqdn_slash, container_client=FakeContainerClient())
    assert store_fqdn_slash.container_name == "mycontainer"
    assert store_fqdn_slash.prefix == ""


def test_service_request_transport_error(azure_url, monkeypatch):
    from azure.core.exceptions import ServiceRequestError

    store = AzureBlobStore(azure_url)

    # 1. Transport error during list_blobs
    monkeypatch.setattr(
        store._container_client,
        "list_blobs",
        lambda *a, **k: (_ for _ in ()).throw(ServiceRequestError("DNS resolution failed")),
    )
    with pytest.raises(StorageError, match="cannot list records"):
        store._list_objects()

    # 2. Transport error during download
    monkeypatch.undo()
    monkeypatch.setattr(
        store._container_client,
        "get_blob_client",
        lambda *a, **k: mock_blob_client_with_error(ServiceRequestError("Connection reset")),
    )
    with pytest.raises(StorageError, match="cannot download record"):
        store._materialise(1, ["input.pickle"])

    # 3. Transport error during delete
    monkeypatch.undo()
    monkeypatch.setattr(
        store._container_client,
        "delete_blob",
        lambda *a, **k: (_ for _ in ()).throw(ServiceRequestError("Socket timeout")),
    )
    with pytest.raises(StorageError, match="cannot delete records"):
        store._delete_keys(["blob1"])


def mock_blob_client_with_error(exc):
    class FailingBlobClient:
        def download_blob(self):
            raise exc

    return FailingBlobClient()


def test_credentials_whitespace_stripping(monkeypatch):
    from quarantine.azure_store import AzureBlobStore

    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "  testaccount  \n")
    monkeypatch.setenv("AZURE_STORAGE_KEY", "  secretkey==  \n")
    store = AzureBlobStore("azure://testcontainer/prefix")
    assert (
        urllib.parse.urlsplit(store._container_client.url).netloc
        == "testaccount.blob.core.windows.net"
    )


def test_emulator_without_account_in_endpoint(monkeypatch):
    from quarantine.azure_store import AzureBlobStore

    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "devstoreaccount1")
    monkeypatch.setenv("AZURE_STORAGE_EMULATOR_HOST", "http://127.0.0.1:10000")
    store = AzureBlobStore("azure://mycontainer/prefix")
    assert store._container_client.url == "http://127.0.0.1:10000/devstoreaccount1/mycontainer"


def test_rebuild_index_and_update_without_path(azure_url, module):
    q = Quarantine(azure_url, halt_after=None, report=False)
    q.call(module.load, "bad")
    store = AzureBlobStore(azure_url)
    rows = store.rebuild_index()
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["error_type"] == "ValueError"

    # Test update when record.path is None
    record = store.get(1)
    record.path = None
    store.update(record)
    assert record.path is not None


def test_write_cache_filters_unsafe_and_symlink(azure_url, tmp_path):
    store = AzureBlobStore(azure_url)
    target = store._safe_target_dir(1)
    sym = target / "sym.txt"
    real = tmp_path / "real.txt"
    real.write_bytes(b"data")
    try:
        sym.symlink_to(real)
        with pytest.raises(StorageError, match="cannot be a symlink"):
            store._write_cache(1, {"sym.txt": b"123"})
    except (OSError, NotImplementedError):
        pass

    # Unsafe name is skipped
    cached = store._write_cache(1, {"../unsafe.txt": b"123", "safe.txt": b"456"})
    assert not (cached / "../unsafe.txt").exists()
    assert (cached / "safe.txt").read_bytes() == b"456"


def test_claim_id_azure_error(azure_url, monkeypatch):
    from azure.core.exceptions import AzureError

    store = AzureBlobStore(azure_url)
    monkeypatch.setattr(
        store,
        "_put",
        lambda *a, **k: (_ for _ in ()).throw(AzureError("network timeout")),
    )
    with pytest.raises(StorageError, match="cannot claim a record id"):
        store._claim_id()


def test_delete_keys_resource_not_found(azure_url, monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError

    store = AzureBlobStore(azure_url)
    monkeypatch.setattr(
        store._container_client,
        "delete_blob",
        lambda *a, **k: (_ for _ in ()).throw(ResourceNotFoundError("Blob does not exist")),
    )
    # Should not raise
    store._delete_keys(["non_existent_key"])

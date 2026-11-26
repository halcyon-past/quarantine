"""End-to-end regression journeys: whole documented workflows, exercised for real.

The unit suites prove each piece works; these tests prove the pieces still work
*together*. Every test here walks a complete user journey through the public
surface only - real subprocesses, the installed CLI, real HTTP against the
dashboard - so a change that breaks a workflow (rather than a unit) fails here
first, with the user's own reproduction steps.

Run just this suite with ``pytest -m regression``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from quarantine import Quarantine, Store, __version__
from quarantine.ui import DashboardHandler

pytestmark = pytest.mark.regression


def _clean_env(**extra: str) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "PYTHONPATH", "TEMP", "TMP", "HOME", "USERPROFILE")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    return {**env, **extra}


@pytest.fixture
def run(tmp_path):
    """Run a command in a fresh process, rooted in the test's own folder."""

    def _run(*argv, extra_env=None):
        return subprocess.run(
            [sys.executable, *argv],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
            timeout=120,
            env=_clean_env(QUARANTINE_DIR=str(tmp_path / ".quarantine"), **(extra_env or {})),
        )

    return _run


def _write_script(tmp_path, name: str, source: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


# -- the configured pipeline, start to finish ------------------------------


PIPELINE = """
import os
from pathlib import Path

from quarantine import quarantine


@quarantine(retries=1, redact=["password"], halt_after=None)
def process(row):
    if row["id"] == "flaky":
        marker = Path("flaky.once")
        if not marker.exists():
            marker.touch()
            raise TimeoutError("transient blip")
        return row
    if row["id"].startswith("bad") and os.environ.get("FIXED") != "1":
        raise ValueError("cannot process " + row["id"])
    return row


if __name__ == "__main__":
    rows = [
        {"id": "ok", "password": "hunter2"},
        {"id": "flaky", "password": "hunter2"},
        {"id": "bad-1", "password": "hunter2"},
        {"id": "bad-2", "password": "hunter2"},
    ]
    for row in rows:
        process(row)
"""


def test_a_configured_pipeline_survives_recovers_and_never_leaks_secrets(tmp_path, run):
    """retries + redact + dedup + retry --import, together, in real processes."""
    _write_script(tmp_path, "job.py", PIPELINE)
    qdir = tmp_path / ".quarantine"

    # 1. the run survives: the transient item recovers via `retries`,
    #    only the genuinely bad items are quarantined.
    first = run("job.py")
    assert first.returncode == 0, first.stderr
    assert "2 processed" in first.stderr
    assert "2 quarantined" in first.stderr

    listed = run("-m", "quarantine", "list", "--json")
    records = json.loads(listed.stdout)
    assert [r["error"] for r in records] == ["cannot process bad-1", "cannot process bad-2"]

    # 2. the secret was redacted *before* anything touched the disk.
    assert all(r["redacted"] == ["password"] for r in records)
    for path in qdir.rglob("*"):
        if path.is_file():
            assert b"hunter2" not in path.read_bytes(), path

    # 3. a rerun recognises the known-bad items instead of duplicating them.
    second = run("job.py")
    assert second.returncode == 0, second.stderr
    assert "2 skipped" in second.stderr
    assert Store(qdir).count() == 2

    # 4. retrying before the fix keeps the records and counts the attempt.
    unfixed = run("-m", "quarantine", "retry", "--import", "job.py")
    assert unfixed.returncode == 1
    assert "2 still failing" in unfixed.stdout
    records = json.loads(run("-m", "quarantine", "list", "--json").stdout)
    assert [r["attempts"] for r in records] == [2, 2]

    # 5. after the fix, everything recovers and the folder is empty again.
    fixed = run("-m", "quarantine", "retry", "--import", "job.py", extra_env={"FIXED": "1"})
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "2 recovered" in fixed.stdout
    assert "Nothing quarantined" in run("-m", "quarantine", "list").stdout


# -- the circuit breaker in a real process ---------------------------------


SYSTEMIC = """
from quarantine import quarantine


@quarantine(halt_after=3)
def process(item):
    raise ValueError(f"bad {item}")


if __name__ == "__main__":
    for item in range(100):
        process(item)
"""


def test_a_systemic_failure_halts_the_process_and_clear_recovers(tmp_path, run):
    _write_script(tmp_path, "job.py", SYSTEMIC)

    crashed = run("job.py")
    assert crashed.returncode != 0
    assert "SystemicFailure" in crashed.stderr
    assert Store(tmp_path / ".quarantine").count() == 3, "halting must not lose records"

    cleared = run("-m", "quarantine", "clear", "--yes")
    assert cleared.returncode == 0, cleared.stderr
    assert "Nothing quarantined" in run("-m", "quarantine", "stats").stdout


# -- async records, replayed through the CLI -------------------------------


ASYNC_JOB = """
import asyncio
import os

from quarantine import quarantine


@quarantine
async def process(item):
    if item % 2 and os.environ.get("FIXED") != "1":
        raise ValueError(f"bad {item}")
    return item


async def main():
    await asyncio.gather(*(process(item) for item in range(6)))


if __name__ == "__main__":
    asyncio.run(main())
"""


def test_async_records_round_trip_through_the_cli(tmp_path, run):
    """Records written under asyncio.gather are replayable by the sync CLI."""
    _write_script(tmp_path, "job.py", ASYNC_JOB)

    first = run("job.py")
    assert first.returncode == 0, first.stderr
    assert "3 quarantined" in first.stderr

    fixed = run("-m", "quarantine", "retry", "--import", "job.py", extra_env={"FIXED": "1"})
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "3 recovered" in fixed.stdout
    assert "Nothing quarantined" in run("-m", "quarantine", "list").stdout


# -- the dashboard, over real HTTP ------------------------------------------


UI_TARGET = """
from pathlib import Path

FIXED = Path(__file__).with_name("fixed.flag")


def load(item):
    if not FIXED.exists():
        raise ValueError(f"cannot load {item}")
    return item
"""


@pytest.fixture
def dashboard(qdir):
    """A live dashboard server on an ephemeral port, torn down afterwards."""
    saved = DashboardHandler.quarantine_dir
    DashboardHandler.quarantine_dir = qdir
    server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        DashboardHandler.quarantine_dir = saved


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


def test_the_dashboard_serves_views_and_retries_over_real_http(
    tmp_path, qdir, q, target_module, dashboard
):
    module = target_module(UI_TARGET, name="qui_target")
    for item in ["alpha", "beta"]:
        q.call(module.load, item)
    assert q.stats.quarantined == 2

    index = _get(f"{dashboard}/")
    assert "#0001" in index and "#0002" in index
    assert "load" in index and "ValueError" in index

    detail = _get(f"{dashboard}/record?id=1")
    assert "alpha" in detail, "the record page shows the input"
    assert "cannot load alpha" in detail, "the record page shows the traceback"

    with pytest.raises(urllib.error.HTTPError) as missing:
        _get(f"{dashboard}/no-such-page")
    assert missing.value.code == 404
    missing.value.close()

    # Clicking Retry: fails while the bug is there, recovers once it is fixed.
    request = urllib.request.Request(f"{dashboard}/retry?id=1", data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        assert "#0001" in response.read().decode("utf-8"), "still failing, still listed"

    (tmp_path / "fixed.flag").touch()
    with urllib.request.urlopen(request, timeout=30) as response:
        after = response.read().decode("utf-8")
    assert "#0001" not in after, "recovered records leave the dashboard"
    assert "#0002" in after, "unretried records stay"
    assert [r.id for r in Store(qdir).records()] == [2]


# -- machine-readable output keeps its shape --------------------------------


def test_the_json_interfaces_keep_their_documented_shape(tmp_path, run):
    """`--json` output is an interface people script against - pin it."""
    _write_script(
        tmp_path,
        "job.py",
        """
        from quarantine import quarantine


        @quarantine
        def process(item):
            return 100 / item


        if __name__ == "__main__":
            for item in [1, 0]:
                process(item)
        """,
    )
    assert run("job.py").returncode == 0

    (record,) = json.loads(run("-m", "quarantine", "list", "--json").stdout)
    assert set(record) >= {
        "id",
        "function",
        "module",
        "fingerprint",
        "source_file",
        "error_type",
        "error",
        "created_at",
        "last_failed_at",
        "attempts",
        "payload_format",
        "preview",
        "meta_version",
    }
    assert record["error_type"] == "ZeroDivisionError"

    (shown,) = json.loads(run("-m", "quarantine", "show", "1", "--json").stdout)
    assert shown["id"] == record["id"]
    assert shown["fingerprint"] == record["fingerprint"]

    stats = json.loads(run("-m", "quarantine", "stats", "--json").stdout)
    assert set(stats) == {
        "dir",
        "exists",
        "records",
        "bytes",
        "by_function",
        "by_error",
        "oldest",
        "newest",
        "unreadable",
    }
    assert stats["records"] == 1
    assert stats["by_error"] == {"ZeroDivisionError": 1}

    dry = json.loads(run("-m", "quarantine", "retry", "--dry-run", "--json").stdout)
    assert set(dry) == {"recovered", "still_failing", "unretryable"}


# -- the exit codes are a contract -------------------------------------------


def test_exit_codes_hold_their_contract(run):
    version = run("-m", "quarantine", "--version")
    assert version.returncode == 0
    assert version.stdout.strip() == f"quarantine {__version__}"

    assert run("-m", "quarantine", "list").returncode == 0, "empty folder is not an error"
    assert run("-m", "quarantine", "retry").returncode == 0, "nothing to retry is not an error"
    assert run("-m", "quarantine", "show", "999").returncode == 1, "a missing record is a problem"
    assert run("-m", "quarantine", "frobnicate").returncode == 2, "bad usage is exit 2"


# -- the folder heals after a crash ------------------------------------------


def test_a_damaged_folder_heals_with_reindex(tmp_path, qdir, run):
    q = Quarantine(qdir, halt_after=None, report=False)
    for item in ["one", "two"]:
        q.call(lambda i: 1 / 0, item)
    assert Store(qdir).count() == 2

    # Simulate a crash mid-write and a lost index.
    (qdir / "index.json").unlink()
    stale = qdir / ".tmp-crashed"
    stale.mkdir()
    (stale / "meta.json").write_text("{", encoding="utf-8")

    healed = run("-m", "quarantine", "reindex")
    assert healed.returncode == 0, healed.stdout + healed.stderr
    assert "indexed 2 record(s)" in healed.stdout
    assert "cleaned up 1 leftover temp entry" in healed.stdout
    assert not stale.exists()

    records = json.loads(run("-m", "quarantine", "list", "--json").stdout)
    assert [r["id"] for r in records] == [1, 2]


# -- a fleet sharing one S3 quarantine ---------------------------------------


S3_WORKER = """
import os
import sys

from quarantine import quarantine

URL = os.environ["QUARANTINE_URL"]


@quarantine(dir=URL, halt_after=None, report=False, retries=0)
def process(item):
    if item.endswith("bad") and os.environ.get("FIXED") != "1":
        raise ValueError(f"cannot process {item}")
    return item


if __name__ == "__main__":
    tag = sys.argv[1]
    for suffix in ["ok-1", "bad", "ok-2"]:
        process(f"{tag}-{suffix}")
"""


def test_a_fleet_shares_one_s3_quarantine(tmp_path, run):
    """Issue #11's journey: ephemeral workers quarantine into one bucket,
    and the failures are inspected and replayed from a different machine."""
    import boto3
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        endpoint = f"http://{host}:{port}"
        aws_env = {
            "AWS_ENDPOINT_URL": endpoint,
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        ).create_bucket(Bucket="fleet-bucket")
        url = "s3://fleet-bucket/etl/quarantine"
        env = {**aws_env, "QUARANTINE_URL": url}

        _write_script(tmp_path, "worker.py", S3_WORKER)

        # 1. two ephemeral "pods" run the job; their local disks tell no tales
        for tag in ["w1", "w2"]:
            result = run("worker.py", tag, extra_env=env)
            assert result.returncode == 0, result.stderr

        # 2. from "the laptop": one shared store holds exactly the two failures
        listed = run("-m", "quarantine", "list", "--dir", url, "--json", extra_env=env)
        assert listed.returncode == 0, listed.stderr
        records = json.loads(listed.stdout)
        assert sorted(r["error"] for r in records) == [
            "cannot process w1-bad",
            "cannot process w2-bad",
        ]
        assert [r["id"] for r in records] == [1, 2], "claimed ids never collided"

        # 3. fix the bug, replay only the failures, the bucket empties
        fixed = run(
            "-m",
            "quarantine",
            "retry",
            "--dir",
            url,
            "--import",
            "worker.py",
            extra_env={**env, "FIXED": "1"},
        )
        assert fixed.returncode == 0, fixed.stdout + fixed.stderr
        assert "2 recovered" in fixed.stdout

        after = run("-m", "quarantine", "list", "--dir", url, extra_env=env)
        assert "Nothing quarantined" in after.stdout
    finally:
        server.stop()


def _gcs_emulator_reachable(host: str) -> bool:
    try:
        urllib.request.urlopen(f"{host}/storage/v1/b", timeout=1)
    except (urllib.error.URLError, OSError):
        return False
    return True


_GCS_EMULATOR_HOST = os.environ.get("STORAGE_EMULATOR_HOST", "http://localhost:4443")

GCS_WORKER = """
import os
import sys

from quarantine import quarantine

URL = os.environ["QUARANTINE_URL"]


@quarantine(dir=URL, halt_after=None, report=False, retries=0)
def process(item):
    if item.endswith("bad") and os.environ.get("FIXED") != "1":
        raise ValueError(f"cannot process {item}")
    return item


if __name__ == "__main__":
    tag = sys.argv[1]
    for suffix in ["ok-1", "bad", "ok-2"]:
        process(f"{tag}-{suffix}")
"""


@pytest.mark.skipif(
    not _gcs_emulator_reachable(_GCS_EMULATOR_HOST),
    reason=(
        f"fake-gcs-server not reachable at {_GCS_EMULATOR_HOST} - "
        f"run `docker run -d -p 4443:4443 fsouza/fake-gcs-server -scheme http`"
    ),
)
def test_a_fleet_shares_one_gcs_quarantine(tmp_path, run):
    """Issue #53's journey: ephemeral workers quarantine into one bucket,
    and the failures are inspected and replayed from a different machine."""
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    client = storage.Client(
        credentials=AnonymousCredentials(),
        project="test-project",
        client_options={"api_endpoint": _GCS_EMULATOR_HOST},
    )
    bucket_name = "fleet-bucket-gcs"
    if client.lookup_bucket(bucket_name) is None:
        client.create_bucket(bucket_name)

    url = f"gs://{bucket_name}/etl/{uuid.uuid4().hex}"
    gcs_env = {"STORAGE_EMULATOR_HOST": _GCS_EMULATOR_HOST, "QUARANTINE_URL": url}

    _write_script(tmp_path, "worker.py", GCS_WORKER)

    # 1. two ephemeral "pods" run the job; their local disks tell no tales
    for tag in ["w1", "w2"]:
        result = run("worker.py", tag, extra_env=gcs_env)
        assert result.returncode == 0, result.stderr

    # 2. from "the laptop": one shared store holds exactly the two failures
    listed = run("-m", "quarantine", "list", "--dir", url, "--json", extra_env=gcs_env)
    assert listed.returncode == 0, listed.stderr
    records = json.loads(listed.stdout)
    assert sorted(r["error"] for r in records) == [
        "cannot process w1-bad",
        "cannot process w2-bad",
    ]
    assert [r["id"] for r in records] == [1, 2], "claimed ids never collided"

    # 3. fix the bug, replay only the failures, the bucket empties
    fixed = run(
        "-m",
        "quarantine",
        "retry",
        "--dir",
        url,
        "--import",
        "worker.py",
        extra_env={**gcs_env, "FIXED": "1"},
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "2 recovered" in fixed.stdout

    after = run("-m", "quarantine", "list", "--dir", url, extra_env=gcs_env)
    assert "Nothing quarantined" in after.stdout


def _azurite_reachable(host: str) -> bool:
    try:
        urllib.request.urlopen(f"{host}/devstoreaccount1?comp=list", timeout=1)
    except (urllib.error.URLError, OSError):
        return False
    return True


_AZURITE_HOST = os.environ.get("AZURE_STORAGE_EMULATOR_HOST", "http://127.0.0.1:10000")

AZURE_WORKER = """
import os
import sys

from quarantine import quarantine

URL = os.environ["QUARANTINE_URL"]


@quarantine(dir=URL, halt_after=None, report=False, retries=0)
def process(item):
    if item.endswith("bad") and os.environ.get("FIXED") != "1":
        raise ValueError(f"cannot process {item}")
    return item


if __name__ == "__main__":
    tag = sys.argv[1]
    for suffix in ["ok-1", "bad", "ok-2"]:
        process(f"{tag}-{suffix}")
"""


@pytest.mark.skipif(
    not _azurite_reachable(_AZURITE_HOST),
    reason=(
        f"Azurite not reachable at {_AZURITE_HOST} - "
        f"run `docker run -d -p 10000:10000 mcr.microsoft.com/azure-storage/azurite`"
    ),
)
def test_a_fleet_shares_one_azure_quarantine(tmp_path, run):
    """Issue #54's journey: ephemeral workers quarantine into one Azure container,
    and the failures are inspected and replayed from a different machine."""
    from azure.storage.blob import ContainerClient

    conn_str = (
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
        f"BlobEndpoint={_AZURITE_HOST}/devstoreaccount1;"
    )
    container_name = "fleet-container-azure"
    client = ContainerClient.from_connection_string(conn_str, container_name=container_name)
    with contextlib.suppress(Exception):
        client.create_container()

    url = f"azure://{container_name}/etl/{uuid.uuid4().hex}"
    azure_env = {
        "AZURE_STORAGE_CONNECTION_STRING": conn_str,
        "QUARANTINE_URL": url,
    }

    _write_script(tmp_path, "worker.py", AZURE_WORKER)

    # 1. two ephemeral "pods" run the job; their local disks tell no tales
    for tag in ["w1", "w2"]:
        result = run("worker.py", tag, extra_env=azure_env)
        assert result.returncode == 0, result.stderr

    # 2. from "the laptop": one shared store holds exactly the two failures
    listed = run("-m", "quarantine", "list", "--dir", url, "--json", extra_env=azure_env)
    assert listed.returncode == 0, listed.stderr
    records = json.loads(listed.stdout)
    assert sorted(r["error"] for r in records) == [
        "cannot process w1-bad",
        "cannot process w2-bad",
    ]
    assert [r["id"] for r in records] == [1, 2], "claimed ids never collided"

    # 3. fix the bug, replay only the failures, the container empties
    fixed = run(
        "-m",
        "quarantine",
        "retry",
        "--dir",
        url,
        "--import",
        "worker.py",
        extra_env={**azure_env, "FIXED": "1"},
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "2 recovered" in fixed.stdout

    after = run("-m", "quarantine", "list", "--dir", url, extra_env=azure_env)
    assert "Nothing quarantined" in after.stdout


class _MiniRespServer:
    """Minimal in-process RESP2 TCP server for multi-process Redis regression tests."""

    def __init__(self) -> None:
        import fnmatch
        import socket
        import threading

        self._fnmatch = fnmatch
        self._lock = threading.Lock()
        self._hashes: dict[str, dict[bytes, bytes]] = {}
        self._strings: dict[str, bytes] = {}
        self._expires: dict[str, float] = {}
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        import threading

        while not self._stop:
            try:
                self._sock.settimeout(0.2)
                conn, _ = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_line(buf: bytearray, conn: Any) -> bytes | None:
        while b"\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf.extend(chunk)
        idx = buf.index(b"\r\n")
        line = bytes(buf[:idx])
        del buf[: idx + 2]
        return line

    def _read_cmd(self, buf: bytearray, conn: Any) -> list[bytes] | None:
        line = self._read_line(buf, conn)
        if line is None:
            return None
        if not line.startswith(b"*"):
            return line.split()
        count = int(line[1:])
        parts: list[bytes] = []
        for _ in range(count):
            hdr = self._read_line(buf, conn)
            if hdr is None or not hdr.startswith(b"$"):
                return None
            length = int(hdr[1:])
            while len(buf) < length + 2:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                buf.extend(chunk)
            parts.append(bytes(buf[:length]))
            del buf[: length + 2]
        return parts

    @staticmethod
    def _resp_bulk(data: bytes | None) -> bytes:
        if data is None:
            return b"$-1\r\n"
        return f"${len(data)}\r\n".encode() + data + b"\r\n"

    @classmethod
    def _resp_array(cls, items: list[bytes]) -> bytes:
        out = [f"*{len(items)}\r\n".encode()]
        out.extend(cls._resp_bulk(i) for i in items)
        return b"".join(out)

    def _purge(self, key: str) -> None:
        exp = self._expires.get(key)
        if exp is not None and time.monotonic() >= exp:
            self._expires.pop(key, None)
            self._hashes.pop(key, None)
            self._strings.pop(key, None)

    def _cmd_scan(self, args: list[bytes]) -> bytes:
        pattern = "*"
        upper_args = [a.upper() for a in args]
        if b"MATCH" in upper_args:
            pattern = args[upper_args.index(b"MATCH") + 1].decode("utf-8")
        with self._lock:
            for k in list(self._expires):
                self._purge(k)
            keys = [
                k.encode("utf-8")
                for k in list(self._hashes) + list(self._strings)
                if self._fnmatch.fnmatch(k, pattern)
            ]
        return b"*2\r\n$1\r\n0\r\n" + self._resp_array(keys)

    def _cmd_hset(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        with self._lock:
            self._purge(key)
            h = self._hashes.setdefault(key, {})
            added = 0
            for i in range(1, len(args), 2):
                if args[i] not in h:
                    added += 1
                h[args[i]] = args[i + 1]
        return f":{added}\r\n".encode()

    def _cmd_hgetall(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        with self._lock:
            self._purge(key)
            flat: list[bytes] = []
            for k, v in self._hashes.get(key, {}).items():
                flat.extend([k, v])
        return self._resp_array(flat)

    def _cmd_hexists(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        with self._lock:
            self._purge(key)
            present = int(args[1] in self._hashes.get(key, {}))
        return f":{present}\r\n".encode()

    def _cmd_exists(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        with self._lock:
            self._purge(key)
            present = int(key in self._hashes or key in self._strings)
        return f":{present}\r\n".encode()

    def _cmd_set(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        nx = any(a.upper() == b"NX" for a in args[2:])
        with self._lock:
            self._purge(key)
            if nx and (key in self._strings or key in self._hashes):
                return b"$-1\r\n"
            self._strings[key] = args[1]
        return b"+OK\r\n"

    def _cmd_incr(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        delta = int(args[1]) if len(args) > 1 else 1
        with self._lock:
            self._purge(key)
            val = int(self._strings.get(key, b"0")) + delta
            self._strings[key] = str(val).encode("utf-8")
        return f":{val}\r\n".encode()

    def _cmd_expire(self, args: list[bytes]) -> bytes:
        key = args[0].decode("utf-8")
        sec = int(args[1])
        with self._lock:
            self._purge(key)
            if key not in self._hashes and key not in self._strings:
                return b":0\r\n"
            self._expires[key] = time.monotonic() + sec
        return b":1\r\n"

    def _cmd_del(self, args: list[bytes]) -> bytes:
        with self._lock:
            removed = 0
            for raw in args:
                key = raw.decode("utf-8")
                self._purge(key)
                if key in self._hashes or key in self._strings:
                    removed += 1
                self._hashes.pop(key, None)
                self._strings.pop(key, None)
                self._expires.pop(key, None)
        return f":{removed}\r\n".encode()

    def _exec_one(self, parts: list[bytes]) -> bytes:
        cmd = parts[0].decode("utf-8").upper()
        args = parts[1:]
        if cmd == "PING":
            return b"+PONG\r\n"
        if cmd == "HELLO":
            return (
                b"*6\r\n$6\r\nserver\r\n$5\r\nredis\r\n"
                b"$7\r\nversion\r\n$5\r\n7.0.0\r\n$5\r\nproto\r\n:2\r\n"
            )
        if cmd in {"SELECT", "CLIENT", "WATCH", "UNWATCH"}:
            return b"+OK\r\n"
        handlers = {
            "SCAN": self._cmd_scan,
            "HSET": self._cmd_hset,
            "HGETALL": self._cmd_hgetall,
            "HEXISTS": self._cmd_hexists,
            "EXISTS": self._cmd_exists,
            "SET": self._cmd_set,
            "INCR": self._cmd_incr,
            "INCRBY": self._cmd_incr,
            "EXPIRE": self._cmd_expire,
            "DEL": self._cmd_del,
        }
        handler = handlers.get(cmd)
        if handler is not None:
            return handler(args)
        return f"-ERR unknown command '{cmd}'\r\n".encode()

    def _handle(self, conn: Any) -> None:
        buf = bytearray()
        in_tx = False
        tx_queue: list[list[bytes]] = []
        with conn:
            while not self._stop:
                parts = self._read_cmd(buf, conn)
                if parts is None:
                    return
                if not parts:
                    continue
                cmd = parts[0].decode("utf-8").upper()
                if cmd == "MULTI":
                    in_tx = True
                    tx_queue.clear()
                    conn.sendall(b"+OK\r\n")
                elif cmd == "EXEC":
                    in_tx = False
                    replies = [self._exec_one(q) for q in tx_queue]
                    tx_queue.clear()
                    conn.sendall(f"*{len(replies)}\r\n".encode() + b"".join(replies))
                elif in_tx:
                    tx_queue.append(parts)
                    conn.sendall(b"+QUEUED\r\n")
                else:
                    conn.sendall(self._exec_one(parts))

    def stop(self) -> None:
        self._stop = True
        with contextlib.suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=1.0)


REDIS_WORKER = """
import os
import sys

from quarantine import quarantine

URL = os.environ["QUARANTINE_URL"]


@quarantine(dir=URL, halt_after=None, report=False, retries=0)
def process(item):
    if item.endswith("bad") and os.environ.get("FIXED") != "1":
        raise ValueError(f"cannot process {item}")
    return item


if __name__ == "__main__":
    tag = sys.argv[1]
    for suffix in ["ok-1", "bad", "ok-2"]:
        process(f"{tag}-{suffix}")
"""


def test_a_fleet_shares_one_redis_quarantine(tmp_path, run):
    """Issue #55's journey: ephemeral workers quarantine into one Redis store,
    and the failures are inspected and replayed from a different machine."""
    server = _MiniRespServer()
    try:
        url = f"redis://127.0.0.1:{server.port}/0?prefix=fleet-{uuid.uuid4().hex[:8]}&ttl=3600"
        env = {"QUARANTINE_URL": url}

        _write_script(tmp_path, "worker.py", REDIS_WORKER)

        # 1. two ephemeral "pods" run the job concurrently; their local disks tell no tales
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda tag: run("worker.py", tag, extra_env=env), ["w1", "w2"]))
        for result in results:
            assert result.returncode == 0, result.stderr

        # 2. from "the laptop": one shared store holds exactly the two failures
        listed = run("-m", "quarantine", "list", "--dir", url, "--json", extra_env=env)
        assert listed.returncode == 0, listed.stderr
        records = json.loads(listed.stdout)
        assert sorted(r["error"] for r in records) == [
            "cannot process w1-bad",
            "cannot process w2-bad",
        ]
        assert [r["id"] for r in records] == [1, 2], "claimed ids never collided"

        # 3. fix the bug, replay only the failures, the Redis prefix empties
        fixed = run(
            "-m",
            "quarantine",
            "retry",
            "--dir",
            url,
            "--import",
            "worker.py",
            extra_env={**env, "FIXED": "1"},
        )
        assert fixed.returncode == 0, fixed.stdout + fixed.stderr
        assert "2 recovered" in fixed.stdout

        after = run("-m", "quarantine", "list", "--dir", url, extra_env=env)
        assert "Nothing quarantined" in after.stdout
    finally:
        server.stop()

"""End-to-end regression journeys: whole documented workflows, exercised for real.

The unit suites prove each piece works; these tests prove the pieces still work
*together*. Every test here walks a complete user journey through the public
surface only - real subprocesses, the installed CLI, real HTTP against the
dashboard - so a change that breaks a workflow (rather than a unit) fails here
first, with the user's own reproduction steps.

Run just this suite with ``pytest -m regression``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

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

"""The README promises processes are safe too, so prove it with real processes."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from quarantine import Store

WORKER = """
import os
import sys

from quarantine import Quarantine

TAG = sys.argv[1]
ITEMS = int(sys.argv[2])

q = Quarantine(os.environ["QUARANTINE_DIR"], halt_after=None, report=False)


def process(item):
    raise ValueError(f"bad {item}")


for index in range(ITEMS):
    q.call(process, f"{TAG}-{index}")
"""


def _env(target: Path) -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "PYTHONPATH", "TEMP", "TMP", "HOME", "USERPROFILE")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["QUARANTINE_DIR"] = str(target)
    return env


def test_several_processes_can_write_to_one_folder(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(WORKER), encoding="utf-8")
    target = tmp_path / "shared"
    workers = 3
    items = 6

    running = [
        subprocess.Popen(
            [sys.executable, str(worker), f"w{index}", str(items)],
            cwd=tmp_path,
            env=_env(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(workers)
    ]
    for process in running:
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr

    store = Store(target)
    records = store.records()
    assert store.problems == []
    assert len(records) == workers * items
    assert len({r.id for r in records}) == workers * items, "ids must be unique"
    assert len({r.fingerprint for r in records}) == workers * items
    assert {r.pid for r in records} != {os.getpid()}, "written by other processes"

    # No half-written records, and the index agrees with the folders.
    assert not [p for p in target.iterdir() if p.name.startswith(".tmp-")]
    assert len(store.index_rows()) == workers * items
    for record in records:
        assert record.path is not None
        assert (record.path / "meta.json").exists()
        assert (record.path / "traceback.txt").exists()
        assert record.load_call().item.startswith("w")


def test_a_second_process_skips_what_the_first_quarantined(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(WORKER), encoding="utf-8")
    target = tmp_path / "shared"

    def run() -> None:
        result = subprocess.run(
            [sys.executable, str(worker), "same", "4"],
            cwd=tmp_path,
            env=_env(target),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    run()
    assert Store(target).count() == 4
    run()  # identical inputs: recognised, not duplicated
    assert Store(target).count() == 4

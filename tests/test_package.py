"""Packaging, public surface, and the promises the README makes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import quarantine as pkg
from quarantine.core import DEFAULT_HALT_AFTER, DEFAULT_MAX_ITEMS, Config

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_public_names_are_importable_and_exported():
    for name in pkg.__all__:
        assert hasattr(pkg, name), name
    assert len(set(pkg.__all__)) == len(pkg.__all__), "no duplicates in __all__"
    for name in ("quarantine", "shield", "Quarantine", "QUARANTINED", "retry", "records"):
        assert name in pkg.__all__


def test_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+([.-].+)?", pkg.__version__)


def test_version_matches_the_changelog():
    assert f"## [{pkg.__version__}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_py_typed_marker_ships():
    assert (Path(pkg.__file__).parent / "py.typed").exists()


def test_no_runtime_dependencies():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in text


def test_defaults_match_the_documentation():
    config = Config()
    assert Path(config.dir).name == ".quarantine"
    assert DEFAULT_HALT_AFTER == 50
    assert "halt_after=50" in README
    assert DEFAULT_MAX_ITEMS == 10_000
    assert "max_items=10_000" in README
    assert config.only == (Exception,)


@pytest.mark.parametrize(
    "name",
    ["input.pkl", "input.txt", "traceback.txt", "meta.json", "index.json"],
)
def test_the_documented_files_are_the_files_we_write(name, qdir):
    from quarantine import quarantine

    @quarantine(dir=str(qdir))
    def process(row):
        raise ValueError("bad")

    process({"id": 1})
    assert name in README
    written = {p.name for p in qdir.rglob("*")}
    assert name in written


def test_the_readme_quickstart_actually_runs(tmp_path, monkeypatch):
    """Run the README's opening example in a fresh interpreter."""
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "job.py"
    script.write_text(
        "from quarantine import quarantine\n"
        "\n"
        "@quarantine\n"
        "def process(item):\n"
        "    return 100 / item\n"
        "\n"
        "items = [1, 2, 0, 4]\n"
        "for item in items:\n"
        "    process(item)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env={**_clean_env(), "QUARANTINE_DIR": str(tmp_path / ".quarantine")},
    )
    assert result.returncode == 0, result.stderr
    assert "3 processed" in result.stderr
    assert "1 quarantined" in result.stderr
    assert (tmp_path / ".quarantine" / "0001" / "traceback.txt").exists()


def test_the_cli_is_installed_as_a_script(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "quarantine", "list"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env={**_clean_env(), "QUARANTINE_DIR": str(tmp_path / ".quarantine")},
    )
    assert result.returncode == 0, result.stderr
    assert "Nothing quarantined" in result.stdout


def _clean_env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "PYTHONPATH", "TEMP", "TMP", "HOME", "USERPROFILE")
    return {key: os.environ[key] for key in keep if key in os.environ}


def test_the_script_workflow_works_end_to_end(tmp_path):
    """The documented story for a plain script: fail, inspect, fix, retry.

    This is the one path a decorator in ``__main__`` cannot take without
    ``--import``, so it is worth exercising for real, in real processes.
    """
    script = tmp_path / "job.py"
    script.write_text(
        textwrap.dedent(
            """
            import os

            from quarantine import quarantine


            @quarantine
            def process(item):
                if item == 0 and os.environ.get("FIXED") != "1":
                    raise ValueError("cannot process 0")
                return item


            if __name__ == "__main__":
                for item in [1, 0, 2]:
                    process(item)
            """
        ),
        encoding="utf-8",
    )
    env = {**_clean_env(), "QUARANTINE_DIR": str(tmp_path / ".quarantine")}

    def run(*argv, extra=None):
        return subprocess.run(
            [sys.executable, *argv],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
            env={**env, **(extra or {})},
        )

    # 1. the job survives the bad item
    first = run(str(script))
    assert first.returncode == 0, first.stderr
    assert "1 quarantined" in first.stderr

    # 2. the record is there, and knows where it came from
    listed = run("-m", "quarantine", "list", "--json")
    assert listed.returncode == 0, listed.stderr
    record = json.loads(listed.stdout)[0]
    assert record["module"] == "__main__"
    assert Path(record["source_file"]) == script
    assert record["error"] == "cannot process 0"

    # 3. a plain retry cannot import __main__, and says so usefully
    blind = run("-m", "quarantine", "retry")
    assert blind.returncode == 1
    assert "--import" in blind.stdout

    # 4. with --import, and the bug fixed, it recovers
    fixed = run("-m", "quarantine", "retry", "--import", "job.py", extra={"FIXED": "1"})
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "1 recovered" in fixed.stdout

    # 5. and the folder is empty again
    after = run("-m", "quarantine", "list")
    assert "Nothing quarantined" in after.stdout

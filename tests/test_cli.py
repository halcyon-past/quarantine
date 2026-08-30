"""The command line, end to end."""

from __future__ import annotations

import json
from typing import Any

import pytest

from quarantine import Quarantine
from quarantine.cli import EXIT_OK, EXIT_PROBLEM, EXIT_USAGE, main

SOURCE = """
FAIL_ON = {"bad", "worse"}


def load(item):
    if item in FAIL_ON:
        raise ValueError(f"cannot load {item}")
    return item.upper()
"""


@pytest.fixture
def module(target_module):
    return target_module(SOURCE)


@pytest.fixture
def populated(qdir, module):
    """Two records in the default folder, from an importable function."""
    instance = Quarantine(qdir, halt_after=None, report=False)
    instance.call(module.load, "bad")
    instance.call(module.load, "worse")
    return instance


def run(*argv):
    return main(list(argv))


# -- list ---------------------------------------------------------------


def test_list_shows_the_documented_columns(populated, capsys):
    assert run("list") == EXIT_OK
    output = capsys.readouterr().out
    header, first, second = output.splitlines()[:3]
    assert header.split() == ["#", "when", "function", "error", "input", "preview"]
    assert "load" in first
    assert "ValueError: cannot load bad" in first
    assert "'bad'" in first
    assert "worse" in second
    assert "run `quarantine retry`" in output


def test_list_is_calm_when_there_is_nothing(capsys):
    assert run("list") == EXIT_OK
    assert "Nothing quarantined" in capsys.readouterr().out


def test_list_filters_and_limits(populated, capsys):
    assert run("list", "--function", "load", "--limit", "1") == EXIT_OK
    out = capsys.readouterr().out
    assert "cannot load bad" in out
    assert "cannot load worse" not in out

    assert run("list", "--function", "nosuch") == EXIT_OK
    assert "Nothing quarantined" in capsys.readouterr().out


def test_list_json(populated, capsys):
    assert run("list", "--json") == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in payload] == [1, 2]
    assert payload[0]["error_type"] == "ValueError"


def test_ls_alias(populated, capsys):
    assert run("ls") == EXIT_OK
    assert "load" in capsys.readouterr().out


def test_explicit_dir_is_honoured(tmp_path, capsys):
    other = tmp_path / "other"
    instance = Quarantine(other, halt_after=None, report=False)
    instance.call(_broken, 1)
    assert run("list", "--dir", str(other)) == EXIT_OK
    assert "_broken" in capsys.readouterr().out


def _broken(item):
    raise RuntimeError("always")


# -- show ---------------------------------------------------------------


def test_show_prints_input_and_traceback(populated, capsys):
    assert run("show", "1") == EXIT_OK
    out = capsys.readouterr().out
    assert "record 0001" in out
    assert "qtarget.load" in out
    assert "--- input ---" in out
    assert "args[0] = 'bad'" in out
    assert "--- traceback ---" in out
    assert "ValueError: cannot load bad" in out


def test_show_reports_a_missing_id(populated, capsys):
    assert run("show", "99") == EXIT_PROBLEM
    assert "no record 99" in capsys.readouterr().err


def test_show_json(populated, capsys):
    assert run("show", "1", "2", "--json") == EXIT_OK
    assert len(json.loads(capsys.readouterr().out)) == 2


# -- retry --------------------------------------------------------------


def test_retry_recovers_and_reports(populated, module, capsys):
    module.FAIL_ON = set()
    assert run("retry") == EXIT_OK
    out = capsys.readouterr().out
    assert "2 recovered" in out
    assert populated.records() == []


def test_retry_keeps_what_still_fails(populated, module, capsys):
    module.FAIL_ON = {"worse"}
    assert run("retry") == EXIT_PROBLEM
    out = capsys.readouterr().out
    assert "1 recovered" in out
    assert "1 still failing (kept in quarantine)" in out
    assert [r.id for r in populated.records()] == [2]


def test_retry_specific_ids(populated, module, capsys):
    module.FAIL_ON = set()
    assert run("retry", "2") == EXIT_OK
    assert [r.id for r in populated.records()] == [1]


def test_retry_dead_after_skips_poison_items(populated, module, capsys):
    run("retry")  # both fail again: attempts go to 2
    capsys.readouterr()
    module.FAIL_ON = {"worse"}
    assert run("retry", "--dead-after", "2", "--json") == EXIT_PROBLEM
    payload = json.loads(capsys.readouterr().out)
    assert payload["recovered"] == []
    assert [entry["id"] for entry in payload["unretryable"]] == [1, 2]
    assert all("dead" in entry["reason"] for entry in payload["unretryable"])

    # By explicit id the same record still runs - and recovers.
    assert run("retry", "1", "--dead-after", "2") == EXIT_OK
    assert [r.id for r in populated.records()] == [2]


def test_retry_dead_after_rejects_nonsense(populated, capsys):
    assert run("retry", "--dead-after", "0") == EXIT_USAGE
    assert "dead_after" in capsys.readouterr().err


def test_retry_dry_run(populated, capsys):
    assert run("retry", "--dry-run") == EXIT_OK
    assert "would retry 2 record(s)" in capsys.readouterr().out
    assert len(populated.records()) == 2


def test_retry_json(populated, module, capsys):
    module.FAIL_ON = {"worse"}
    assert run("retry", "--json") == EXIT_PROBLEM
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"recovered": [1], "still_failing": [2], "unretryable": []}


def test_retry_with_nothing_to_do(capsys):
    assert run("retry") == EXIT_OK
    assert "Nothing quarantined" in capsys.readouterr().out


def test_retry_reports_records_it_cannot_replay(qdir, capsys):
    instance = Quarantine(qdir, halt_after=None, report=False)

    def local(item):
        raise ValueError("nope")

    instance.wrap(local)(1)
    assert run("retry") == EXIT_PROBLEM
    out = capsys.readouterr().out
    assert "0001 skipped" in out
    assert "defined inside another function" in out


# -- debug --------------------------------------------------------------


def test_debug_print_only(populated, capsys):
    assert run("debug", "1", "--print") == EXIT_OK
    out = capsys.readouterr().out
    assert "args[0] = 'bad'" in out
    assert "ValueError: cannot load bad" in out


def test_debug_drops_into_post_mortem(populated, monkeypatch, capsys):
    import pdb

    seen: dict[str, Any] = {}
    monkeypatch.setattr(pdb, "post_mortem", lambda tb: seen.setdefault("tb", tb))
    assert run("debug", "1") == EXIT_OK
    assert seen["tb"] is not None
    assert "re-running the failing call" in capsys.readouterr().out


def test_debug_says_so_when_it_no_longer_fails(populated, module, monkeypatch, capsys):
    import pdb

    module.FAIL_ON = set()
    monkeypatch.setattr(pdb, "post_mortem", lambda tb: pytest.fail("should not be reached"))
    assert run("debug", "1") == EXIT_OK
    assert "succeeded this time" in capsys.readouterr().out


def test_debug_hands_you_the_input_when_it_cannot_re_run(populated, monkeypatch, capsys):
    import pdb

    called: dict[str, Any] = {}
    monkeypatch.setattr(pdb.Pdb, "set_trace", lambda self: called.setdefault("yes", True))
    assert run("debug", "1", "--no-post-mortem") == EXIT_OK
    assert called == {"yes": True}
    assert "in scope" in capsys.readouterr().out


def test_debug_reports_a_missing_id(capsys):
    assert run("debug", "7") == EXIT_USAGE
    assert "no record 7" in capsys.readouterr().err


def test_debug_reports_an_unreplayable_input(populated, capsys):
    (populated.dir / "0001" / "input.pkl").unlink()
    assert run("debug", "1") == EXIT_PROBLEM
    assert "no replayable input" in capsys.readouterr().err


# -- clear --------------------------------------------------------------


def test_clear_needs_confirmation_when_not_a_tty(populated, capsys):
    assert run("clear") == EXIT_OK
    captured = capsys.readouterr()
    assert "refusing to delete without --yes" in captured.err
    assert len(populated.records()) == 2


def test_clear_with_yes(populated, capsys):
    assert run("clear", "--yes") == EXIT_OK
    assert "deleted 2 record(s)" in capsys.readouterr().out
    assert populated.records() == []


def test_clear_specific_ids(populated, capsys):
    assert run("clear", "1") == EXIT_OK
    assert "deleted 0001" in capsys.readouterr().out
    assert [r.id for r in populated.records()] == [2]


def test_clear_reports_missing_ids(populated, capsys):
    assert run("clear", "42") == EXIT_PROBLEM
    assert "no record 42" in capsys.readouterr().err


def test_clear_on_an_empty_folder(capsys):
    assert run("clear", "--yes") == EXIT_OK
    assert "Nothing quarantined" in capsys.readouterr().out


def test_clear_accepts_a_yes_answer_on_a_tty(populated, monkeypatch, capsys):
    class FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            return "y\n"

    monkeypatch.setattr("sys.stdin", FakeStdin())
    assert run("clear") == EXIT_OK
    assert populated.records() == []


def test_clear_declined_leaves_records(populated, monkeypatch, capsys):
    class FakeStdin:
        def isatty(self):
            return True

        def readline(self):
            return "n\n"

    monkeypatch.setattr("sys.stdin", FakeStdin())
    assert run("clear") == EXIT_OK
    assert "Left alone" in capsys.readouterr().out
    assert len(populated.records()) == 2


# -- stats / reindex ----------------------------------------------------


def test_stats(populated, capsys):
    assert run("stats") == EXIT_OK
    out = capsys.readouterr().out
    assert "2 record(s)" in out
    assert "load" in out
    assert "ValueError" in out
    assert "on disk" in out


def test_stats_json(populated, capsys):
    assert run("stats", "--json") == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["records"] == 2
    assert payload["by_error"] == {"ValueError": 2}
    assert payload["by_function"] == {"load": 2}
    assert payload["bytes"] > 0
    assert payload["unreadable"] == 0


def test_stats_on_an_empty_folder(capsys):
    assert run("stats") == EXIT_OK
    assert "Nothing quarantined" in capsys.readouterr().out


def test_reindex_rebuilds_the_index(populated, capsys):
    index = populated.store.index_path
    index.unlink()
    assert run("reindex") == EXIT_OK
    assert "indexed 2 record(s)" in capsys.readouterr().out
    assert json.loads(index.read_text(encoding="utf-8"))["count"] == 2


def test_reindex_cleans_up_stale_temp_folders(populated, capsys):
    (populated.dir / ".tmp-leftover").mkdir()
    assert run("reindex") == EXIT_OK
    assert "cleaned up 1 leftover temp entry" in capsys.readouterr().out


def test_reindex_reports_unreadable_records(populated, capsys):
    (populated.dir / "0003").mkdir()
    (populated.dir / "0003" / "meta.json").write_text("{oops", encoding="utf-8")
    assert run("reindex") == EXIT_PROBLEM
    assert "not valid JSON" in capsys.readouterr().err


def test_reindex_on_an_empty_folder(capsys):
    assert run("reindex") == EXIT_OK
    assert "does not exist yet" in capsys.readouterr().out


# -- top level ----------------------------------------------------------


def test_no_command_prints_help(capsys):
    assert main([]) == EXIT_USAGE
    assert "usage: quarantine" in capsys.readouterr().out


def test_version(capsys):
    from quarantine import __version__

    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "exit codes:" in out
    assert "quarantine retry" in out


def test_storage_errors_become_a_usage_exit(monkeypatch, capsys):
    from quarantine import cli
    from quarantine.errors import StorageError

    def explode(store, function=None):
        raise StorageError("the disk is on fire")

    monkeypatch.setattr(cli, "_load", explode)
    assert run("list") == EXIT_USAGE
    assert "the disk is on fire" in capsys.readouterr().err


def test_python_m_quarantine_is_wired_up(monkeypatch, capsys):
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["quarantine"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("quarantine", run_name="__main__")
    assert caught.value.code == EXIT_USAGE
    assert "usage: quarantine" in capsys.readouterr().out


def test_ui_command(monkeypatch, capsys):

    called_with_port = None
    called_with_dir = None

    def mock_start_server(port, d):
        nonlocal called_with_port, called_with_dir
        called_with_port = port
        called_with_dir = d
        return 0

    from quarantine import cli

    monkeypatch.setattr(cli, "start_server", mock_start_server)

    code = main(["ui", "--port", "9090", "--dir", "custom-dir"])
    assert code == 0
    assert called_with_port == 9090
    assert str(called_with_dir) == "custom-dir"

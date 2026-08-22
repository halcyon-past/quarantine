"""Finding the function again - including the awkward `__main__` case."""

from __future__ import annotations

import textwrap

import pytest

from quarantine import Quarantine
from quarantine.record import Record
from quarantine.resolve import ResolutionError, load_module_from_path, resolve_function

SCRIPT = """
FIXED = False


def load(item):
    if not FIXED:
        raise ValueError("still broken")
    return item * 2


if __name__ == "__main__":  # pragma: no cover - not executed by the tests
    load(1)
"""


def make_record(**changes: object) -> Record:
    fields: dict[str, object] = {
        "id": 1,
        "function": "load",
        "module": "__main__",
        "fingerprint": "fp",
        "error_type": "ValueError",
        "error": "still broken",
        "created_at": "",
        "last_failed_at": "",
    }
    fields.update(changes)
    return Record(**fields)  # type: ignore[arg-type]


def write_script(tmp_path, name="job.py", body=SCRIPT):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_a_script_function_is_found_via_import_from(tmp_path):
    script = write_script(tmp_path)
    found = resolve_function(make_record(source_file=str(script)), import_from=script)
    assert found.__name__ == "load"
    with pytest.raises(ValueError, match="still broken"):
        found(1)


def test_main_from_another_process_gets_actionable_advice(tmp_path):
    script = write_script(tmp_path)
    with pytest.raises(ResolutionError) as caught:
        resolve_function(make_record(source_file=str(script)))
    message = str(caught.value)
    assert "ran as a script" in message
    assert f"quarantine retry --import {script}" in message


def test_main_in_the_same_process_still_resolves(tmp_path, monkeypatch):
    """Calling retry() from inside the script itself must keep working."""
    import __main__

    monkeypatch.setattr(__main__, "__file__", str(write_script(tmp_path)), raising=False)
    monkeypatch.setattr(__main__, "resolvable", lambda item: item, raising=False)
    record = make_record(
        function="resolvable",
        source_file=str(tmp_path / "job.py"),
    )
    assert resolve_function(record)(3) == 3


def test_load_module_from_path_rejects_what_it_cannot_import(tmp_path):
    with pytest.raises(ResolutionError, match="is not a file"):
        load_module_from_path(tmp_path / "nope.py")

    broken = tmp_path / "broken.py"
    broken.write_text("def oops(:\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="SyntaxError"):
        load_module_from_path(broken)


def test_load_module_from_path_can_see_its_siblings(tmp_path):
    (tmp_path / "helper_mod.py").write_text("VALUE = 7\n", encoding="utf-8")
    main = tmp_path / "uses_helper.py"
    main.write_text("from helper_mod import VALUE\n\n\ndef get():\n    return VALUE\n", "utf-8")
    module = load_module_from_path(main)
    assert module.get() == 7


def test_retry_accepts_an_import_path(tmp_path, qdir):
    script = write_script(tmp_path, name="importable_job.py")
    module = load_module_from_path(script)

    instance = Quarantine(qdir, halt_after=None, report=False)
    instance.call(module.load, 21)
    record = instance.records()[0]
    assert record.source_file == str(script.resolve())

    # Pretend the record came from a script run directly, as it would have.
    record.module = "__main__"
    instance.store.update(record)
    assert "ran as a script" in instance.retry().unretryable[0][1]

    script.write_text(textwrap.dedent(SCRIPT).replace("FIXED = False", "FIXED = True"), "utf-8")
    assert instance.retry(import_from=script).recovered == [1]
    assert instance.records() == []


def test_source_file_is_recorded_for_ordinary_functions(q):
    q.call(_broken, 1)
    record = q.records()[0]
    assert record.source_file.endswith("test_resolve.py")


def _broken(item):
    raise RuntimeError("always")

"""The summary line, and output that cannot crash the job it is reporting on."""

from __future__ import annotations

import io
from typing import Any, cast

import pytest

from quarantine import Quarantine, quarantine
from quarantine.reporting import (
    ascii_fallback,
    columnize,
    emit,
    encodable,
    live_reporters,
    print_summaries,
    reset_reporters,
)


def test_summary_line_matches_the_documented_shape(qdir):
    instance = Quarantine(qdir, halt_after=None, report=False)
    safe = instance.wrap(_maybe_fail)
    for index in range(10):
        safe(index)

    line = instance.summary_line()
    assert line is not None
    assert line.startswith("✓ 8 processed")
    assert f"✗ 2 quarantined → {qdir}/" in line
    assert "run `quarantine retry` after fixing" in line


def _maybe_fail(number):
    if number % 5 == 0:
        raise ValueError(number)
    return number


def test_thousands_are_grouped(qdir):
    instance = Quarantine(qdir, report=False)
    instance.stats.processed = 9996
    instance.stats.quarantined = 4
    line = instance.summary_line()
    assert line is not None
    assert "✓ 9,996 processed" in line


def test_skipped_and_recovered_appear_when_relevant(qdir):
    instance = Quarantine(qdir, report=False)
    instance.stats.skipped = 3
    instance.stats.recovered = 2
    line = instance.summary_line()
    assert line is not None
    assert "⏭ 3 skipped (already quarantined)" in line
    assert "↺ 2 recovered" in line


def test_a_clean_run_says_nothing(qdir):
    instance = Quarantine(qdir, report=False)
    instance.wrap(lambda value: value)(1)
    assert instance.summary_line() is None
    assert print_summaries() == []


def test_registered_instances_report_at_exit(qdir, capsys):
    @quarantine(dir=str(qdir))
    def process(item):
        raise ValueError("bad")

    process(1)
    printed = print_summaries()
    assert len(printed) == 1
    assert "1 quarantined" in capsys.readouterr().err


def test_report_can_be_turned_off(qdir):
    reset_reporters()
    Quarantine(qdir, report=False)
    assert live_reporters() == []


def test_emit_falls_back_to_ascii_on_a_limited_console():
    class Cp1252Stream(io.StringIO):
        def write(self, text):
            text.encode("cp1252")  # raises for ✓ and →
            return super().write(text)

    stream = Cp1252Stream()
    emit("✓ 1 processed · ✗ 1 quarantined → .quarantine/", stream)
    written = stream.getvalue()
    assert "OK 1 processed" in written
    assert "FAIL 1 quarantined ->" in written


def test_emit_never_raises():
    class Hostile:
        closed = False

        def write(self, text):
            raise OSError("broken pipe")

        def flush(self):
            raise OSError("broken pipe")

    emit("anything", cast("Any", Hostile()))  # must not raise
    emit("anything", None)


def test_emit_ignores_a_closed_stream():
    stream = io.StringIO()
    stream.close()
    emit("anything", stream)


def test_ascii_fallback_covers_every_glyph_we_use():
    assert ascii_fallback("✓✗→·⏭↺✋…─") == "OK FAIL -> | SKIP RETRY STOP ... -".replace(" ", "")


def test_columnize_aligns_and_truncates():
    lines = columnize(
        [[1, "short"], [2, "a very long value indeed"]],
        ["#", "value"],
        widths=[3, 10],
    )
    assert lines[0].split() == ["#", "value"]
    assert lines[1].startswith("1  ")
    assert lines[2].endswith("…")
    assert all(len(line) <= 16 for line in lines)


def test_columnize_handles_missing_and_none_cells():
    lines = columnize([[None, "x"]], ["a", "b"])
    assert "x" in lines[1]


def test_dead_instances_are_forgotten(qdir):
    import gc

    reset_reporters()
    Quarantine(qdir, report=True)
    gc.collect()
    assert live_reporters() == []


@pytest.mark.parametrize("stats", [{"quarantined": 1}, {"skipped": 1}, {"recovered": 1}])
def test_any_interesting_counter_produces_a_line(qdir, stats):
    instance = Quarantine(qdir, report=False)
    for key, value in stats.items():
        setattr(instance.stats, key, value)
    assert instance.summary_line()


def test_ascii_fallback_is_chosen_before_writing_to_a_narrow_console():
    """stderr uses backslashreplace, so we must check the encoding up front."""

    class Cp1252Console(io.StringIO):
        encoding = "cp1252"

    stream = Cp1252Console()
    emit("✓ 4 processed · ✗ 1 quarantined → .quarantine/", stream)
    written = stream.getvalue()
    assert written.startswith("OK 4 processed | FAIL 1 quarantined ->")
    assert "\u2713" not in written


def test_encodable_reports_what_a_stream_can_take():
    class Utf8(io.StringIO):
        encoding = "utf-8"

    class Ascii(io.StringIO):
        encoding = "ascii"

    class Nonsense(io.StringIO):
        encoding = "not-a-codec"

    assert encodable("✓", Utf8())
    assert not encodable("✓", Ascii())
    assert encodable("plain", Ascii())
    assert not encodable("✓", Nonsense())
    assert encodable("✓", io.StringIO())  # no encoding attribute: assume it copes

"""Reruns must not pile up duplicates or re-run known-bad items."""

from __future__ import annotations

from typing import Any

from quarantine import Quarantine, is_quarantined, is_skipped
from quarantine.fingerprint import fingerprint, fingerprint_source
from quarantine.redact import Redactor
from quarantine.serialize import Call


def test_a_known_bad_item_is_skipped_on_the_next_run(qdir):
    calls = []

    def process(row):
        calls.append(row)
        raise ValueError("bad row")

    first = Quarantine(qdir, halt_after=None, report=False)
    assert is_quarantined(first.wrap(process)({"id": 1}))
    assert len(calls) == 1

    # A second process, same folder: the item is recognised and not re-run.
    second = Quarantine(qdir, halt_after=None, report=False)
    assert is_skipped(second.wrap(process)({"id": 1}))
    assert len(calls) == 1
    assert second.stats.skipped == 1
    assert len(list(qdir.glob("0*"))) == 1


def test_skipping_can_be_turned_off(qdir):
    calls = []

    def process(row):
        calls.append(row)
        raise ValueError("bad row")

    first = Quarantine(qdir, halt_after=None, report=False)
    first.wrap(process)({"id": 1})

    second = Quarantine(qdir, halt_after=None, report=False, skip_known_bad=False)
    assert is_quarantined(second.wrap(process)({"id": 1}))
    assert len(calls) == 2
    assert len(list(qdir.glob("0*"))) == 2  # deliberately a second record


def test_only_the_matching_item_is_skipped(qdir):
    def process(row):
        raise ValueError("bad row")

    first = Quarantine(qdir, halt_after=None, report=False)
    first.wrap(process)({"id": 1})

    second = Quarantine(qdir, halt_after=None, report=False)
    safe = second.wrap(process)
    assert is_skipped(safe({"id": 1}))
    assert is_quarantined(safe({"id": 2}))
    assert second.stats.skipped == 1
    assert second.stats.quarantined == 1


def test_recovered_items_are_processed_again(qdir, target_module):
    module = target_module(
        """
        FAIL = True


        def load(item):
            if FAIL:
                raise ValueError("broken")
            return item * 2
        """
    )
    instance = Quarantine(qdir, halt_after=None, report=False)
    instance.call(module.load, 21)
    assert is_skipped(instance.call(module.load, 21))

    module.FAIL = False
    instance.retry()
    assert instance.call(module.load, 21) == 42


def test_fingerprint_ignores_dict_ordering():
    first = Call(({"a": 1, "b": 2},))
    second = Call(({"b": 2, "a": 1},))
    assert fingerprint("process", first) == fingerprint("process", second)


def test_fingerprint_separates_functions_and_inputs():
    call = Call(({"a": 1},))
    assert fingerprint("one", call) != fingerprint("two", call)
    assert fingerprint("one", call) != fingerprint("one", Call(({"a": 2},)))


def test_fingerprint_survives_exotic_inputs():
    class Weird:
        def __repr__(self):
            raise RuntimeError("nope")

    row: dict[str, Any] = {"self": None}
    row["self"] = row
    assert fingerprint("f", Call((Weird(),)))
    assert fingerprint("f", Call((row,)))


def test_fingerprint_is_computed_after_redaction():
    redactor = Redactor(["password"])
    one = Call(({"id": 1, "password": "a"},))
    two = Call(({"id": 1, "password": "b"},))
    assert fingerprint_source("f", one, redactor) == fingerprint_source("f", two, redactor)
    assert fingerprint_source("f", one, redactor) != fingerprint("f", one)

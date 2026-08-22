"""Secrets must not reach the disk - and the caller's data must not be mutated."""

from __future__ import annotations

from collections import OrderedDict, namedtuple
from dataclasses import dataclass
from typing import Any

import pytest

from quarantine import quarantine
from quarantine.redact import MAX_DEPTH, PLACEHOLDER, Redactor, compile_patterns, redact


def test_top_level_and_nested_fields_are_scrubbed():
    data = {
        "id": 7,
        "api_key": "sk-live-123",
        "nested": {"password": "hunter2", "keep": "yes"},
        "rows": [{"password": "a"}, {"password": "b"}],
    }
    clean, hits = redact(data, ["api_key", "password"])

    assert clean["id"] == 7
    assert clean["api_key"] == PLACEHOLDER
    assert clean["nested"] == {"password": PLACEHOLDER, "keep": "yes"}
    assert [row["password"] for row in clean["rows"]] == [PLACEHOLDER, PLACEHOLDER]
    assert hits == {"api_key", "password"}


def test_the_original_object_is_never_mutated():
    data: dict[str, Any] = {"password": "hunter2", "nested": {"password": "x"}}
    clean, _ = redact(data, ["password"])
    assert data["password"] == "hunter2"
    assert data["nested"]["password"] == "x"
    assert clean is not data


def test_matching_is_case_insensitive_and_glob_aware():
    data = {"API_KEY": 1, "AccessToken": 2, "refresh_token": 3, "id": 4}
    clean, hits = redact(data, ["api_key", "*token*"])
    assert clean == {
        "API_KEY": PLACEHOLDER,
        "AccessToken": PLACEHOLDER,
        "refresh_token": PLACEHOLDER,
        "id": 4,
    }
    assert hits == {"API_KEY", "AccessToken", "refresh_token"}


def test_containers_keep_their_shape():
    Point = namedtuple("Point", "x secret")
    data = {
        "tuple": (1, {"secret": "s"}),
        "list": [{"secret": "s"}],
        "set": frozenset({1, 2}),
        "ordered": OrderedDict(secret="s", keep=1),
        "named": Point(1, "s"),
    }
    clean, _ = redact(data, ["secret"])
    assert isinstance(clean["tuple"], tuple)
    assert clean["tuple"][1]["secret"] == PLACEHOLDER
    assert isinstance(clean["list"], list)
    assert isinstance(clean["set"], frozenset)
    assert isinstance(clean["ordered"], OrderedDict)
    assert clean["ordered"]["secret"] == PLACEHOLDER
    assert isinstance(clean["named"], Point)
    assert clean["named"].secret == PLACEHOLDER


def test_dataclasses_and_plain_objects_are_walked():
    @dataclass
    class Creds:
        user: str
        password: str

    class Config:
        def __init__(self):
            self.name = "prod"
            self.api_key = "sk-live"

    creds = Creds("ann", "hunter2")
    config = Config()
    clean_creds, _ = redact(creds, ["password"])
    clean_config, _ = redact(config, ["api_key"])

    assert clean_creds.password == PLACEHOLDER
    assert clean_creds.user == "ann"
    assert creds.password == "hunter2"
    assert clean_config.api_key == PLACEHOLDER
    assert clean_config.name == "prod"
    assert config.api_key == "sk-live"


def test_frozen_dataclass_degrades_to_a_dict_view():
    @dataclass(frozen=True)
    class Frozen:
        password: str

    clean, hits = redact(Frozen("hunter2"), ["password"])
    assert clean == {"__class__": "Frozen", "password": PLACEHOLDER}
    assert hits == {"password"}


def test_reference_cycles_do_not_hang():
    row: dict[str, Any] = {"password": "x"}
    row["self"] = row
    clean, _ = redact(row, ["password"])
    assert clean["password"] == PLACEHOLDER


def test_absurd_nesting_is_truncated_not_crashed():
    deep: dict[str, Any] = {"password": "x"}
    for _ in range(MAX_DEPTH + 5):
        deep = {"next": deep}
    redactor = Redactor(["password"])
    redactor.apply(deep)
    assert redactor.truncated


def test_broken_property_is_skipped():
    class Fragile:
        secret = "s"

        @property
        def boom(self):
            raise RuntimeError("do not touch")

    clean, hits = redact(Fragile(), ["boom", "secret"])
    assert hits == set()  # nothing in __dict__ to redact
    assert clean is not None


def test_compile_patterns_normalises_and_validates():
    assert compile_patterns([" API_KEY ", "api_key", ""]) == ("api_key",)
    with pytest.raises(TypeError, match="must be strings"):
        compile_patterns([b"api_key"])  # type: ignore[list-item]


def test_redaction_happens_before_anything_touches_the_disk(qdir):
    @quarantine(dir=str(qdir), redact=["api_key", "password"])
    def charge(payload, *, api_key):
        raise ValueError("card declined")

    charge({"user": "ann", "password": "hunter2"}, api_key="sk-live-secret")

    written = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in qdir.rglob("*")
        if path.is_file()
    )
    assert "hunter2" not in written
    assert "sk-live-secret" not in written
    assert PLACEHOLDER in written
    assert "ann" in written


def test_redacted_fields_are_listed_in_meta(qdir):
    @quarantine(dir=str(qdir), redact=["password"])
    def process(row):
        raise ValueError("bad")

    process({"password": "hunter2"})
    from quarantine import records

    assert records(dir=str(qdir))[0].redacted == ["password"]

"""Shared fixtures. Every test runs in its own directory with its own sick bay."""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

from quarantine import api, reporting
from quarantine.core import Quarantine


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point quarantine at a throwaway folder and reset all global state."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QUARANTINE_DIR", str(tmp_path / ".quarantine"))
    api.reset()
    reporting.reset_reporters()
    yield tmp_path
    api.reset()
    reporting.reset_reporters()


@pytest.fixture
def qdir(tmp_path: Path) -> Path:
    """Path of the quarantine folder used by the default instance."""
    return tmp_path / ".quarantine"


@pytest.fixture
def q(qdir: Path) -> Quarantine:
    """A quarantine with the noisy bits (exit summary, circuit breaker) turned off."""
    return Quarantine(qdir, halt_after=None, report=False)


@pytest.fixture
def target_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An importable module on disk, so `retry` can re-import its functions.

    Returns a factory: ``target_module(source) -> module``.
    """
    created: list[str] = []

    def make(source: str, name: str = "qtarget") -> object:
        path = tmp_path / f"{name}.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        sys.modules.pop(name, None)
        module = importlib.import_module(name)
        created.append(name)
        return module

    yield make

    for name in created:
        sys.modules.pop(name, None)

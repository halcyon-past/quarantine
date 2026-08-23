"""Documentation has to keep up with the code, so the tests check that it does."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import quarantine as pkg
from quarantine.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
README = (ROOT / "README.md").read_text(encoding="utf-8")
SPEC = ROOT / "quarentine.md"

DOC_FILES = [
    "index.md",
    "installation.md",
    "usage.md",
    "cli.md",
    "api.md",
    "on-disk-format.md",
    "troubleshooting.md",
    "faq.md",
]


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", DOC_FILES)
def test_doc_pages_exist_and_are_substantial(name):
    text = read(name)
    assert text.startswith("# "), "every page starts with a title"
    assert len(text.splitlines()) > 30, "a stub is not documentation"


@pytest.mark.parametrize("name", DOC_FILES)
def test_readme_links_to_every_doc_page(name):
    if name == "index.md":
        return  # linked from the docs pages themselves
    assert f"docs/{name}" in README


def _relative_links(text: str) -> list[str]:
    return [
        target
        for target in re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", text)
        if not target.startswith(("http://", "https://", "mailto:"))
    ]


def test_every_relative_link_in_the_readme_resolves():
    for target in _relative_links(README):
        assert (ROOT / target).exists(), target


@pytest.mark.parametrize("name", DOC_FILES)
def test_every_relative_link_in_the_docs_resolves(name):
    for target in _relative_links(read(name)):
        assert (DOCS / target).resolve().exists(), f"{name} -> {target}"


def _subparser_actions() -> list[Any]:
    """The argparse plumbing that holds the subcommands (private, but stable)."""
    parser: Any = build_parser()
    return [a for a in parser._subparsers._group_actions if getattr(a, "choices", None)]


def _subcommands() -> list[str]:
    return sorted({name for action in _subparser_actions() for name in action.choices})


def test_every_cli_command_is_documented():
    """Each command has a section of its own, or is named as an alias of one."""
    cli_doc = read("cli.md")
    for name in _subcommands():
        documented = f"`quarantine {name}" in cli_doc or f"Alias: `{name}`" in cli_doc
        assert documented, f"{name} is missing from docs/cli.md"
        mentioned = f"quarantine {name}" in README or f"`{name}` works too" in README
        assert mentioned, f"{name} is missing from the README"


def test_every_cli_flag_is_documented():
    cli_doc = read("cli.md")
    for action in _subparser_actions():
        for command, sub in action.choices.items():
            for option in sub._actions:
                flags = [f for f in option.option_strings if f.startswith("--")]
                for flag in flags:
                    if flag in {"--help", "--dir"}:
                        continue  # documented once, globally
                    assert flag in cli_doc, f"{command} {flag} is undocumented"


def test_every_public_name_is_documented():
    api_doc = read("api.md")
    for name in pkg.__all__:
        if name.startswith("__"):
            continue
        assert name in api_doc, f"{name} is missing from docs/api.md"


def test_the_documented_defaults_are_the_real_defaults():
    from quarantine.core import DEFAULT_HALT_AFTER, DEFAULT_MAX_ITEMS, Config

    api_doc = read("api.md")
    config = Config()
    assert f"| `halt_after` | `{DEFAULT_HALT_AFTER}` |" in api_doc
    assert f"| `max_items` | `{DEFAULT_MAX_ITEMS:_}` |" in api_doc
    assert config.halt_after == DEFAULT_HALT_AFTER
    assert config.max_items == DEFAULT_MAX_ITEMS
    assert "3.10" in read("installation.md")
    assert '">=3.10"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_readme_still_covers_every_section_of_the_original_spec():
    """The README may grow, but it must not drop what the spec promised."""
    if not SPEC.exists():
        pytest.skip("spec file not present")
    spec = SPEC.read_text(encoding="utf-8")
    for heading in re.findall(r"^#{2,3} .+$", spec, flags=re.MULTILINE):
        assert heading in README, f"README lost: {heading}"
    for promise in [
        "pip install quarantine-py",
        "@quarantine",
        "quarantine retry",
        "quarantine debug 2",
        ".quarantine/",
        "halt_after=50",
        "max_items=10_000",
        "redact=",
        "on_quarantine=",
        "shield(items, using=process)",
    ]:
        assert promise in README, f"README lost: {promise}"


def test_changelog_and_contributing_are_real():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{pkg.__version__}]" in changelog
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "pip install -e" in contributing
    assert "pytest" in contributing

"""The reference document's catalog tables must match the registry.

The tables in `docs/reference/mcp-server.md` are written by hand, so they drift
the moment a verb is registered or withdrawn without someone remembering to
edit them. A reader who trusts a stale table is told a working verb cannot be
called, or is never told a verb exists. These tests fail on that drift and name
the verbs that differ, so the fix does not start with a hand diff.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lionagi.mcp.verbs import ABSENT, VERBS

DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "mcp-server.md"

# Verb names as the document writes them: backticked, in a table's first cell.
_ROW_NAME = re.compile(r"^\|\s*`([a-z][a-z0-9.\-]*)`\s*\|")

_NUMBER_WORDS = {
    "One": 1,
    "Two": 2,
    "Three": 3,
    "Four": 4,
    "Five": 5,
    "Six": 6,
    "Seven": 7,
    "Eight": 8,
    "Nine": 9,
    "Ten": 10,
    "Eleven": 11,
    "Twelve": 12,
    "Thirteen": 13,
    "Fourteen": 14,
    "Fifteen": 15,
    "Sixteen": 16,
    "Seventeen": 17,
    "Eighteen": 18,
    "Nineteen": 19,
    "Twenty": 20,
    "Twenty-one": 21,
    "Twenty-two": 22,
    "Twenty-three": 23,
    "Twenty-four": 24,
    "Twenty-five": 25,
    "Twenty-six": 26,
    "Twenty-seven": 27,
    "Twenty-eight": 28,
    "Twenty-nine": 29,
    "Thirty": 30,
}


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _marked_region(text: str, key: str) -> str:
    start = f"<!-- mcp-catalog:{key}:start -->"
    end = f"<!-- mcp-catalog:{key}:end -->"
    assert start in text and end in text, (
        f"{DOC.name} no longer carries the {key!r} table markers ({start} / {end}). "
        "The catalog tables are checked against the registry by name, so removing "
        "the markers removes the check — restore them, or delete this test "
        "deliberately along with the tables it guards."
    )
    return text.split(start, 1)[1].split(end, 1)[0]


def _documented(text: str, key: str) -> set[str]:
    return {
        m.group(1)
        for line in _marked_region(text, key).splitlines()
        if (m := _ROW_NAME.match(line.strip()))
    }


def _explain(kind: str, documented: set[str], registry: set[str]) -> str:
    missing = sorted(registry - documented)
    extra = sorted(documented - registry)
    parts = [f"{DOC.name} disagrees with the registry about which verbs are {kind}."]
    if missing:
        parts.append(
            f"  {kind.capitalize()} in the registry but absent from the {kind} table "
            f"({len(missing)}): {', '.join(missing)}"
        )
    if extra:
        parts.append(
            f"  Listed in the {kind} table but not {kind} in the registry "
            f"({len(extra)}): {', '.join(extra)}"
        )
    parts.append(
        "  Regenerate the table from the registry rather than editing single rows: "
        "`help=true` (or lionagi.mcp.verbs.VERBS / ABSENT) is the authority."
    )
    return "\n".join(parts)


def test_available_table_matches_registry(doc_text):
    registry = set(VERBS)
    documented = _documented(doc_text, "available")
    assert documented == registry, _explain("available", documented, registry)


def test_unavailable_table_matches_registry(doc_text):
    registry = {absent.name for absent in ABSENT}
    documented = _documented(doc_text, "unavailable")
    assert documented == registry, _explain("unavailable", documented, registry)


def test_stated_available_count_matches_registry(doc_text):
    match = re.search(r"^(\d+) verbs are reachable\.", doc_text, re.MULTILINE)
    assert match, (
        "The catalog section no longer opens with 'N verbs are reachable.'; "
        f"the registry currently registers {len(VERBS)}."
    )
    stated = int(match.group(1))
    assert stated == len(VERBS), (
        f"{DOC.name} says {stated} verbs are reachable; the registry registers "
        f"{len(VERBS)}. A reader sizing the surface from the prose is told the "
        "wrong number even when the table below it is right."
    )


def test_stated_unavailable_count_matches_registry(doc_text):
    match = re.search(
        r"^([A-Z][a-z]+(?:-[a-z]+)?) further names are catalogued", doc_text, re.MULTILINE
    )
    assert match, (
        "The unavailable section no longer opens with 'N further names are "
        f"catalogued'; the registry currently catalogues {len(ABSENT)} of them."
    )
    word = match.group(1)
    assert word in _NUMBER_WORDS, (
        f"{DOC.name} states the unavailable count as {word!r}, which is not a "
        f"number word this test knows. The registry catalogues {len(ABSENT)}."
    )
    assert _NUMBER_WORDS[word] == len(ABSENT), (
        f"{DOC.name} says {word} ({_NUMBER_WORDS[word]}) names are catalogued as "
        f"unavailable; the registry catalogues {len(ABSENT)}."
    )

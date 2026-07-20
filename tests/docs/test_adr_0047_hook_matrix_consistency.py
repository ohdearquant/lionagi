"""ADR-0047's Consequences section must not drift from its own production matrix.

Regression: after delta 2 wired API_PRE_CALL/API_POST_CALL/API_STREAM_CHUNK, the
Consequences section still claimed "Three values currently describe reserved
vocabulary only" -- false at that head, since only ARTIFACT_CREATED remained
unwired, and directly contradicted the production matrix ~200 lines above it.
"""

from pathlib import Path

ADR_PATH = (
    Path(__file__).parents[2]
    / "docs"
    / "adr"
    / "ADR-0047-hook-mechanism-scopes-and-canonical-ownership.md"
)


def _text() -> str:
    return ADR_PATH.read_text(encoding="utf-8")


def test_consequences_section_names_only_artifact_created_as_unwired():
    text = _text()
    assert "Three\n  values currently describe reserved vocabulary only" not in text
    assert "Three values currently describe reserved vocabulary only" not in text

    consequences_start = text.index("## Consequences")
    delta_table_start = text.index("## Current-vs-ideal delta")
    consequences = text[consequences_start:delta_table_start]

    assert "`ARTIFACT_CREATED` alone currently describes reserved" in consequences


def test_production_matrix_and_consequences_agree_on_unwired_count():
    text = _text()
    matrix_start = text.index(
        "| Point | Production source | Payload supplied at that source | State |"
    )
    matrix_end = text.index("\n\n", matrix_start)
    matrix = text[matrix_start:matrix_end]

    # Every catalog row except ARTIFACT_CREATED must show a real production
    # emit site (i.e. not "none" in both the HookBus/SessionObserver columns).
    unwired_rows = [
        line for line in matrix.splitlines() if line.startswith("| `") and "| none | none |" in line
    ]
    assert len(unwired_rows) == 1
    assert "ARTIFACT_CREATED" in unwired_rows[0]

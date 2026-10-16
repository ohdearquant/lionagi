"""Pin the collection records' split between shipped state and target decisions.

ADR-0002 records what the collection model does today; its two remaining
questions are decided in ADR-0129. The failure these checks catch is the
records drifting back into one document, or a claim about the code being
restated without the code that backs it:

- a target decision landing in the retrospective record again;
- the holder sites being described as waiting on a lock they do not wait on;
- a named view going unclassified in the multiplicity table;
- the index losing the record that carries the decisions.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = ROOT / "docs" / "adr"
RETROSPECTIVE = ADR_DIR / "ADR-0002-uuid-keyed-ordered-collection-model.md"
TARGET = ADR_DIR / "ADR-0129-ordering-multiplicity-and-holder-coordination.md"
INDEX = ADR_DIR / "README.md"

# Every view the collection records name, with the multiplicity its type fixes.
EXPECTED_VIEW_CLASSES = {
    "A collection's default order (`Pile.progression`)": "unique",
    "`Flow` views appended by `add_item`": "repeated",
    "`Executor.pending`": "repeated",
    "`MessageManager` typed views (`assistant_responses`, `actions`, `action_requests`, `action_responses`, `instructions`)": "unique",
    "`Executor.pile`": "unique",
    "`MessageManager.messages`": "unique",
    '`Branch.progression` when `metadata["current_progression"]` is set': "repeated",
    "`Exchange` mailbox views `outbox` and `inbox_<sender>`": "repeated",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _lock_block(path: str, header_line_1based: int) -> str:
    """Return the source lines under one `async with <collection>` header.

    The block ends at the first line that is non-blank and no deeper than the
    header, which is the same rule the reader applies when deciding what a
    lock is held across.
    """
    lines = (ROOT / path).read_text(encoding="utf-8").split("\n")
    header = lines[header_line_1based - 1]
    indent = len(header) - len(header.lstrip())
    body: list[str] = []
    for line in lines[header_line_1based:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def _view_classes(text: str) -> dict[str, str]:
    """Map each classified view label to the leading word of its Class cell."""
    rows: dict[str, str] = {}
    for line in text.split("\n"):
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] in {"View", ""}:
            continue
        head = cells[2].split(":", 1)[0].split(",", 1)[0].split(" ", 1)[0].strip()
        if head in {"unique", "repeated"}:
            rows[cells[0]] = head
    return rows


def test_target_decisions_live_in_an_aspirational_record():
    header = _text(TARGET)
    assert header.startswith("# ADR-0129: ")
    assert "- **Status**: Proposed" in header
    assert "- **Kind**: Aspirational" in header
    assert "- **Implementation-status**: not-started" in header
    assert "- **Area**: core-data-model" in header
    for section in ("## Context", "## Decision", "## Consequences", "## Alternatives considered"):
        assert section in header, f"ADR-0129 is missing {section}"


def test_retrospective_record_keeps_no_target_decisions():
    text = _text(RETROSPECTIVE)
    assert "- **Kind**: Retrospective" in text
    # No amendment heading carrying decisions, only the pointer and the delta rows.
    assert "## Amendment" not in text
    assert "ADR-0129" in text
    # The two superseded summary rows are annotated where they stand.
    assert "Superseded 2026-10-16 by ADR-0129 D1" in text
    assert "Superseded 2026-10-16 by ADR-0129 D2" in text
    # The one-line pointer sits under the delta table's introduction.
    assert "decided in **ADR-0129**" in text
    # The status line names the per-clause supersession rather than claiming an amendment.
    assert "superseded 2026-10-16 by ADR-0129" in text


def test_delta_rows_state_a_testable_acceptance_clause():
    text = _text(RETROSPECTIVE)
    rows = {m.group(1): m.group(2) for m in re.finditer(r"^\| (\d) \| (.*) \|$", text, re.M)}
    for number in ("3", "4"):
        assert "Acceptance requires" in rows[number], f"delta row {number} has no acceptance clause"
    # Row 4 names the snapshot value's type and each holder.
    assert "`Pile.snapshot()`" in rows["4"]
    assert "tuple of ids" in rows["4"]
    for holder in (
        "`Session`",
        "`MessageManager`",
        "`Exchange`",
        "`DataLogger`",
        "`Executor`",
        "`Flow`",
    ):
        assert holder in rows["4"], f"delta row 4 does not name {holder}"
    # Row 3 carries the aliasing target.
    assert "`Pile(order=<order instance>)`" in rows["3"]


def test_the_four_named_callers_do_not_await_under_the_lock():
    """Each cited site is a synchronous holder, so its block contains no await."""
    for path, header in (
        ("lionagi/session/session.py", 75),
        ("lionagi/protocols/messages/manager.py", 74),
        ("lionagi/protocols/messages/manager.py", 80),
        ("lionagi/session/exchange.py", 87),
        ("lionagi/protocols/generic/log.py", 246),
    ):
        body = _lock_block(path, header)
        assert not re.search(r"\bawait\b", body), (
            f"{path}:{header} now awaits inside the lock; the records call it a synchronous holder"
        )


def test_the_holder_that_does_wait_under_the_lock_is_named():
    """The two sites that genuinely await under a collection lock stay cited."""
    session = _lock_block("lionagi/session/session.py", 279)
    assert "await resolved_branch.aclone" in session
    adapted = _lock_block("lionagi/protocols/generic/pile.py", 902)
    assert "await super().adapt_to_async" in adapted


def test_every_named_view_carries_its_multiplicity_class():
    table = _view_classes(_text(TARGET))
    for view, expected in EXPECTED_VIEW_CLASSES.items():
        assert view in table, f"the multiplicity table does not classify {view!r}"
        assert table[view] == expected, (
            f"{view!r} is classified {table[view]!r}, expected {expected!r}"
        )
    # The accepted records whose presumptions change are named.
    text = _text(TARGET)
    assert "ADR-0006 D4" in text
    assert "ADR-0004 D5" in text


def test_index_lists_the_new_record():
    index = _text(INDEX)
    assert "ADR-0129-ordering-multiplicity-and-holder-coordination.md" in index
    assert "(sequential number; see Numbering)" in index

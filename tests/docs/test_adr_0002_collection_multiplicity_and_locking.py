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
    "`Branch.progression`, both forms": "unique",
    "`Exchange` mailbox views `outbox` and `inbox_<sender>`": "repeated",
}

# Holder names the records must state, and the two sites that await under a lock.
HOLDER_NAMES = (
    "`Session`",
    "`MessageManager`",
    "`Exchange`",
    "`DataLogger`",
    "`Executor`",
    "`InMemoryStore`",
    "`Flow`",
)
AWAITING_SITES = ("`Session.asplit`", "`Pile.adapt_to_async`")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block_at(lines: list[str], header_index: int) -> str:
    """Return the source lines under one header, ending at the first line that
    is non-blank and no deeper than the header.

    This is the same rule a reader applies when deciding what a lock is held
    across, stated as text rather than as a line number so a lock moving inside
    its method does not silently retarget the check.
    """
    header = lines[header_index]
    indent = len(header) - len(header.lstrip())
    body: list[str] = []
    for line in lines[header_index + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return "\n".join(body)


def _method_body(path: str, method_pattern: str, header_pattern: str) -> str:
    """Body under ``header_pattern`` inside the method ``method_pattern`` names.

    Both patterns are regexes matched against the source, so a moved method or
    header is found by name; only a rename or a deleted header fails, which is
    the case the record must be updated for anyway.
    """
    lines = (ROOT / path).read_text(encoding="utf-8").split("\n")
    method_re = re.compile(method_pattern)
    header_re = re.compile(header_pattern)
    method_line = next((i for i, line in enumerate(lines) if method_re.search(line)), None)
    assert method_line is not None, f"{path} no longer defines {method_pattern!r}"
    header_line = next(
        (i for i in range(method_line, len(lines)) if header_re.search(lines[i])),
        None,
    )
    assert header_line is not None, (
        f"{path} no longer opens {header_pattern!r} inside {method_pattern!r}"
    )
    return _block_at(lines, header_line)


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
    # Row 3 names construction and the aliasing target.
    assert "`Pile(order=<order instance>)`" in rows["3"]
    assert "construction" in rows["3"]
    assert "ordering and Pile/Flow round trip" in rows["3"]
    # Row 4 names the snapshot value and each holder.
    assert "`Pile.snapshot()`" in rows["4"]
    assert "tuple of ids" in rows["4"]
    assert "expose no lock" in rows["4"]
    for holder in HOLDER_NAMES:
        assert holder in rows["4"], f"delta row 4 does not name {holder}"
    for site in AWAITING_SITES:
        assert site in rows["4"], f"delta row 4 does not name the awaiting site {site}"


def _d2_holder_list(target: str) -> str:
    """The holder-rule bullet list in ADR-0129 D2, up to the accepted-records block."""
    start = target.index("The named holders are:")
    end = target.index("Accepted records this changes")
    return target[start:end]


def test_the_records_name_every_holder_and_the_awaiting_sites():
    """The records, not just the code, must name who coordinates a collection."""
    target = _text(TARGET)
    holders = _d2_holder_list(target)
    for holder in HOLDER_NAMES:
        assert holder in holders, f"ADR-0129's holder rule does not name {holder}"
    for site in AWAITING_SITES:
        assert site in target, f"ADR-0129 does not name the awaiting site {site}"
    # The accepted record whose presumption changes is named with its clause.
    assert "ADR-0092 D3" in target


def test_the_four_named_callers_do_not_await_under_the_lock():
    """Each cited site is a synchronous holder, so its block contains no await."""
    for path, method, header in (
        ("lionagi/session/session.py", r"def ainclude_branches", r"async with self\.branches:"),
        (
            "lionagi/protocols/messages/manager.py",
            r"def aclear_messages",
            r"async with self\.messages:",
        ),
        (
            "lionagi/protocols/messages/manager.py",
            r"def a_add_message",
            r"async with self\.messages:",
        ),
        ("lionagi/session/exchange.py", r"def collect\b", r"async with self\.flows:"),
        ("lionagi/protocols/generic/log.py", r"def alog\b", r"async with self\.logs:"),
    ):
        body = _method_body(path, method, header)
        assert not re.search(r"\bawait\b", body), (
            f"{path} {method} now awaits inside the lock; the records call it a synchronous holder"
        )


def test_the_holders_that_wait_under_the_lock_are_named():
    """The two sites that genuinely await under a collection lock stay cited."""
    session = _method_body(
        "lionagi/session/session.py", r"def asplit\b", r"async with self\.branches:"
    )
    assert "await resolved_branch.aclone" in session
    adapted = _method_body(
        "lionagi/protocols/generic/pile.py",
        r"def adapt_to_async\b",
        r"async with self\._both_locks\(\):",
    )
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
    assert "ADR-0092 D3" in text


def test_the_target_states_construction_rejects_a_duplicate_id():
    """D1's unique order rejects a duplicate at construction, not only on mutators."""
    target = _text(TARGET)
    decision = target[target.index("### D1 —") : target.index("### D2 —")]
    assert "construct([uids]) -> rejects" in decision
    assert "Both construction and every mutator" in decision
    # The repeated order keeps the supplied duplicate.
    assert "construct([uids]) -> keeps" in decision


def test_the_two_locks_are_described_as_excluding_each_other():
    """The mechanism in ADR-0002 must match the shipped dual-lock protocol."""
    text = _text(RETROSPECTIVE)
    d6 = text[text.index("### D6 —") :]
    assert "exclude each other" in d6
    assert "do not exclude each other" not in d6, (
        "ADR-0002 D6 still claims the two locks can overlap"
    )
    assert "spin" in d6
    # The retired false claim must not survive in the target either.
    target = _text(TARGET)
    assert "do not exclude each other" not in target


def test_index_lists_the_new_record():
    index = _text(INDEX)
    assert "ADR-0129-ordering-multiplicity-and-holder-coordination.md" in index
    assert "(sequential number; see Numbering)" in index

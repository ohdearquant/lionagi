# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin ADR-0007 Amendment 1's contracts and its citations of the current code.

The amendment states two things a reader has to be able to trust: the target
contract (authority on a block and on a section, two bound records per section,
a required ``bounds`` field, a refusal that carries the records) and what the
code does today (the two refusals on the active path, the eviction and
compaction paths, and the omission markers one layer down). Both sides are read
here, at the cited line, so drift in either the record or the source fails in
this file rather than in a reader's head.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_PATH = REPO_ROOT / "docs/adr/ADR-0007-canonical-turn-request-compilation-boundary.md"
AMENDMENT_HEADING = "## Amendment 1 (2026-10-16)"


def _adr() -> str:
    return ADR_PATH.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _amendment_body() -> str:
    text = _adr()
    parts = text.split(AMENDMENT_HEADING, 1)
    assert len(parts) == 2, (
        f"ADR-0007 no longer contains {AMENDMENT_HEADING!r}. The assertions below "
        "pin the amendment's contract, so a renamed heading needs updating here."
    )
    return parts[1]


def _source_line(relative: str, number: int) -> str:
    lines = (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
    assert number <= len(lines), f"{relative} has no line {number}"
    return lines[number - 1]


def _code_block_body(text: str, start: str) -> str:
    """Body of the fenced block whose first line is ``start``."""
    parts = text.split(start, 1)
    assert len(parts) == 2, f"fence opening {start!r} not found"
    body = parts[1].split("```", 1)[0]
    assert body.strip(), f"fence {start!r} is empty"
    return body


# --- the contract the record fixes -----------------------------------------


def test_contract_block_carries_authority_and_a_required_bounds_field() -> None:
    body = _code_block_body(_adr(), "class SectionBound:")
    assert "authority: SectionAuthority" in body

    request = _code_block_body(_adr(), "class CompiledChatRequest:")
    assert "bounds: tuple[SectionBound, ...]" in request
    # ``= ()`` is what let a compiled request exist with no bound records, which
    # is the state the amendment says is inexpressible.
    assert "bounds: tuple[SectionBound, ...] = " not in request
    assert "no default" in request


def test_context_block_and_section_authority_are_in_d1s_contract() -> None:
    text = _adr()
    block = _code_block_body(text, "class ContextBlock:")
    assert "authority: SectionAuthority" in block
    assert "Amended by D8" in block
    assert "class SectionAuthority(str, Enum)" in text


def test_d3_example_carries_the_authority_key() -> None:
    text = _adr()
    example = _code_block_body(text, "LIONAGI_CONTEXT_BLOCKS_JSON\n[")
    assert example.count('"authority":"material"') == 2, (
        "both entries in D3's example must carry the authority key D8 adds"
    )
    payload = _code_block_body(text, "payload = [")
    assert '"authority": block.authority.value' in payload
    # D1's ContextBlock and D3's example each carry the marker; added in one
    # place only, a reader of the other would not know it was amended.
    assert text.count("Amended by D8") >= 2


def test_refusal_error_carries_the_bound_records() -> None:
    body = _code_block_body(_adr(), "class SectionBudgetRefused(ValueError):")
    assert "bounds" in body
    assert "self.bounds = bounds" in body
    assert "bounds: tuple[SectionBound, ...]" in body


def test_reason_vocabulary_has_the_five_values() -> None:
    body = _code_block_body(_adr(), "LeftOutReason = Literal[")
    for reason in (
        "per_provider_limit",
        "section_budget",
        "truncated",
        "evicted",
        "compacted",
    ):
        assert f'"{reason}"' in body, f"{reason} missing from LeftOutReason"


def test_history_left_out_records_evictions_and_compactions() -> None:
    """The two removals the active view performs have to appear as left-out entries."""
    normalized = _normalized(_amendment_body())
    assert (
        "The history section's `left_out` therefore records every message that the active "
        "view excludes but the record keeps" in normalized
    )
    assert "an eviction is a message with reason `evicted`" in normalized
    assert (
        "a compaction is each message the summary replaced, with reason `compacted`" in normalized
    )


def test_every_posture_includes_skip_for_context() -> None:
    text = _adr()
    assert 'posture: Literal["truncate", "refuse", "skip"]' in text
    normalized = _normalized(_amendment_body())
    assert "**skip**: the whole unit is left out and never cut" in normalized
    assert "which is the no-partial-truncation rule ADR-0008 D3 already fixes" in normalized


def test_marker_text_is_the_records_own_not_a_provider_shape() -> None:
    normalized = _normalized(_amendment_body())
    assert "The marker is D7's own text, not any provider's" in normalized
    assert "The Codex provider's character-counting" in normalized
    assert "cut marker is not its shape" in normalized


def test_kept_stores_references_not_raw_text() -> None:
    body = _code_block_body(_adr(), "class SectionBound:")
    assert "never raw text" in body


# --- what the record says the code does today ------------------------------


def test_the_two_refusals_on_the_active_path_are_at_the_cited_lines() -> None:
    first = _source_line("lionagi/operations/chat/_prepare.py", 295)
    assert "First message in progression must be an Instruction or System" in first
    assert "raise ValueError" in first

    error_line = _source_line("lionagi/operations/chat/_prepare.py", 347)
    assert "EmptyOutgoingContentError(" in error_line


def test_the_empty_content_guard_tests_the_current_turn() -> None:
    prepare = "lionagi/operations/chat/_prepare.py"
    assert "_current_turn_rendered = None" in _source_line(prepare, 311)
    assert "if i == _last_index:" in _source_line(prepare, 330)
    assert "_current_turn_rendered = rendered" in _source_line(prepare, 331)
    assert _source_line(prepare, 341).lstrip().startswith("if _contents")


def test_eviction_path_and_its_metadata_key_are_where_cited() -> None:
    context = "lionagi/tools/context/context.py"
    assert "current_progression" in _source_line(context, 144)
    assert 'branch.metadata["current_progression"] = cp' in _source_line(context, 148)
    assert _source_line(context, 290).strip() == 'if action == "evict":'
    assert "cp.exclude(uids)" in _source_line(context, 298)
    assert _source_line(context, 306).strip() == 'if action == "evict_action_results":'
    assert "cp.exclude(to_evict)" in _source_line(context, 317)

    branch = "lionagi/session/branch.py"
    assert "agent-managed subset" in _source_line(branch, 249)
    assert 'cp = self.metadata.get("current_progression")' in _source_line(branch, 251)


def test_compaction_path_replaces_a_span_with_one_summary() -> None:
    context = "lionagi/tools/context/context.py"
    assert _source_line(context, 353).strip() == 'if action == "compact":'
    assert "CONTEXT COMPACTION" in _source_line(context, 418)
    assert "cp.insert(summary_at, note.id)" in _source_line(context, 422)
    assert "cp.exclude(collapse)" in _source_line(context, 423)


def test_codex_omission_markers_are_the_three_cited_shapes() -> None:
    codex = "lionagi/providers/openai/codex.py"
    assert "[output truncated:" in _source_line(codex, 996)
    assert "characters omitted here]" in _source_line(codex, 997)
    assert "items not shown]" in _source_line(codex, 1119)
    assert "keys not shown" in _source_line(codex, 1154)


def test_amendment_counts_three_codex_marker_shapes_and_cites_each() -> None:
    normalized = _normalized(_amendment_body())
    assert "in three shapes" in normalized
    for cited in (":995-998", ":1117-1120", ":1149-1154"):
        assert cited in normalized, f"{cited} is not cited for its marker shape"
    # The mapping note is applied a second time per file-change entry; dropping
    # that citation loses the count's fourth site.
    assert ":1209-1212" in normalized


def test_d7_marker_text_names_section_count_and_reason() -> None:
    normalized = _normalized(_amendment_body())
    assert (
        "[LIONAGI_SECTION_TRUNCATED section=<section> left_out=<n> reason=<reason>]" in normalized
    )


def test_react_context_removal_is_listed_as_a_left_out() -> None:
    normalized = _normalized(_amendment_body())
    assert "a second, narrower left-out exists inside one ReAct turn's later calls" in normalized
    assert "(`lionagi/operations/chat/_prepare.py:191-232`)" in normalized


def test_amendment_cites_the_value_error_guard_by_line() -> None:
    normalized = _normalized(_amendment_body())
    assert "raises a `ValueError` when the" in normalized
    assert "(`lionagi/operations/chat/_prepare.py:295`, cited by D2 §3 above)" in normalized


def test_operations_types_module_holds_only_parameter_dataclasses() -> None:
    types_path = "lionagi/operations/types.py"
    assert "class ChatParam(MorphParam)" in _source_line(types_path, 102)
    assert "class RunParam(ChatParam)" in _source_line(types_path, 141)
    source = (REPO_ROOT / types_path).read_text(encoding="utf-8")
    for absent in ("CompiledChatRequest", "TurnRequest", "SectionAuthority", "SectionBound"):
        assert absent not in source, f"{absent} now exists; the amendment's 'today' list is stale"


# --- the sentences the amendment had to stop saying ------------------------


def test_the_removed_false_claims_are_gone() -> None:
    text = _adr()
    for removed in (
        "no constructor admits a section without a bound record",
        "refusal exists for exactly one case",
        "one in-context cut marker does exist",
        "amendments are per-record",
        "the module does not exist yet",
        "| Delta (ADR-0008) | How this amendment narrows it |",
        "bounds: tuple[SectionBound, ...] = ()",
    ):
        assert removed not in text, f"{removed!r} is still in the record"


def test_changed_decisions_are_named_where_they_are_defined() -> None:
    text = _adr()
    assert "Amended by D7" in text
    assert "Amended by D8" in text
    normalized = _normalized(text)
    assert (
        "D1's `CompiledChatRequest` and `ContextBlock` contracts and D3's example are amended"
        in (normalized)
    )
    assert "D2, D4, D5 and D6 are unchanged" in normalized


def test_no_delta_table_remains_in_the_amendment() -> None:
    body = _amendment_body()
    assert "ADR-0008 delta 1" in _normalized(body)
    assert "ADR-0008 delta 4" in _normalized(body)
    assert "D7 does not decide this row" in _normalized(body)
    # A delta table is reserved for retrospective records (docs/adr/TEMPLATE.md);
    # this ADR is Aspirational.
    assert "\n|---|" not in body


def test_header_carries_the_required_implementation_status() -> None:
    header = _adr().split("## Context", 1)[0]
    assert "- **Implementation-status**:" in header
    assert "not-started" in header

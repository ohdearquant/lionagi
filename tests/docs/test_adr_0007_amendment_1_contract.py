# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin ADR-0007 Amendment 1's contracts and its citations of the current code.

The amendment states two things a reader has to be able to trust: the target
contract (authority on a block and on a section, a bound record per section, a
required ``bounds`` field, a refusal that carries the records, skipped entries
with their reasons) and what the code does today (the two refusals on the active
path, the eviction and compaction paths, and the omission markers one layer
down).

Record statements are pinned as sentences; code citations are pinned as symbol
or statement text found inside the cited line range, never as an exact line, so
an edit elsewhere in the cited file that only shifts the lines does not fail
this module. Removing the cited symbol does fail it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_PATH = REPO_ROOT / "docs/adr/ADR-0007-canonical-turn-request-compilation-boundary.md"
AMENDMENT_HEADING = "## Amendment 1 (2026-10-16)"

D1_HEADING = "### D1 — Separate record-ready instruction data from transient call data"
D2_HEADING = "### D2 — One deterministic compiler owns selection, folding, and rendering"
D3_HEADING = "### D3 — Context blocks use a canonical source-attributed JSON section"
D7_HEADING = "### D7 — Every bound has two visible states"
D8_HEADING = "### D8 — Sections carry authority; material cannot wear the system's"


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


def _decision_block(heading: str) -> str:
    """The record from ``heading`` to the next section heading of the same level.

    Slicing by heading is what lets a test assert that a marker sits inside the
    decision it belongs to rather than anywhere in the record.
    """
    text = _adr()
    parts = text.split(heading, 1)
    assert len(parts) == 2, f"ADR-0007 no longer contains the heading {heading!r}"
    body = parts[1]
    for terminator in ("\n### ", "\n## "):
        if terminator in body:
            body = body.split(terminator, 1)[0]
    return body


def _source_span(relative: str, start: int, end: int) -> str:
    """The text of lines ``start``..``end`` of a cited file, so the symbol is what
    is pinned and a shift of the surrounding lines is not a failure."""
    lines = (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
    assert 1 <= start <= end <= len(lines), f"{relative} has no lines {start}-{end}"
    return "\n".join(lines[start - 1 : end])


def _in_span(relative: str, start: int, end: int, *needles: str) -> None:
    span = _source_span(relative, start, end)
    for needle in needles:
        assert needle in span, f"{needle!r} not found in {relative}:{start}-{end}"


def _count_phrase(text: str, phrase: str) -> int:
    return text.count(phrase)


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


def test_refusal_error_carries_the_bound_records() -> None:
    body = _code_block_body(_adr(), "class SectionBudgetRefused(ValueError):")
    assert "bounds" in body
    assert "self.bounds = bounds" in body
    assert "bounds: tuple[SectionBound, ...]" in body


def test_reason_vocabulary_is_exactly_the_five_values() -> None:
    body = _code_block_body(_adr(), "LeftOutReason = Literal[")
    values = re.findall(r'^\s*"([a-z_]+)",', body, flags=re.MULTILINE)
    assert values == [
        "per_provider_limit",
        "section_budget",
        "truncated",
        "evicted",
        "compacted",
    ], f"LeftOutReason must enumerate exactly these five values, found {values!r}"
    normalized = _normalized(_amendment_body())
    assert "of exactly five values" in normalized, "a sixth value must fail the count too"


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


# --- every "Amended by" marker sits in the decision it belongs to -----------


def test_d1_block_carries_the_bounds_marker() -> None:
    block = _decision_block(D1_HEADING)
    assert "bounds: tuple[SectionBound, ...]  # Amended by D7; required, no default" in block


def test_d1_block_carries_the_skipped_marker() -> None:
    block = _decision_block(D1_HEADING)
    assert "skipped: tuple[LeftOut, ...] = ()  # Amended by D7" in block


def test_d1_block_carries_the_authority_marker() -> None:
    block = _decision_block(D1_HEADING)
    assert "authority: SectionAuthority = field(  # Amended by D8" in block


def test_d2_block_carries_its_amendment_marker() -> None:
    block = _decision_block(D2_HEADING)
    assert "#### 6. Apply section bounds and record both states (Amended by D7)" in block


def test_d3_block_carries_both_authority_markers() -> None:
    block = _decision_block(D3_HEADING)
    # D3 has two markers: the key D8 adds in the payload sketch, and the prose
    # note under the example. Removing either one used to leave the suite green.
    assert '"authority": block.authority.value,  # Amended by D8' in block
    assert "Amended by D8: each canonical JSON entry gains an `authority` key" in block


def test_the_blocks_with_marks_are_the_only_ones_claimed_amended() -> None:
    normalized = _normalized(_adr())
    assert (
        "D1's `CompiledChatRequest` and `TurnRequest` and `ContextBlock` contracts, D2's "
        "algorithm and D3's example are amended in place" in normalized
    )
    assert "D4, D5 and D6 are unchanged" in normalized
    assert "D2, D4, D5 and D6 are unchanged" not in normalized


# --- D2 applies the bounds D1 requires --------------------------------------


def test_d2_carries_the_bounds_step() -> None:
    block = _decision_block(D2_HEADING)
    assert "Apply section bounds and record both states (Amended by D7)" in block
    assert "applies each section's declared bound" in _normalized(block)
    assert "raises `SectionBudgetRefused` before the" in _normalized(block)


def test_d2_renders_tool_output_as_its_own_section() -> None:
    block = _decision_block(D2_HEADING)
    assert "Folded action results render in the request's `tool_results` section" in _normalized(
        block
    )
    assert "so D7's bound applies to them independently of the instruction" in _normalized(block)
    assert "`tool_results` section in action-response order" in _normalized(block)
    assert "Pending action responses are carried into the request's `tool_results`" in _normalized(
        block
    )


def test_section_literal_names_tool_results_and_instruction() -> None:
    body = _code_block_body(_adr(), "class SectionBound:")
    assert 'section: Literal["system", "instruction", "history", "context", "tool_results"]' in body


# --- skipped entries and their reasons --------------------------------------


def test_turn_request_carries_skipped_entries_with_reasons() -> None:
    request = _code_block_body(_adr(), "class TurnRequest:")
    assert "skipped: tuple[LeftOut, ...] = ()" in request
    normalized = _normalized(_amendment_body())
    assert "`TurnRequest.skipped`, a `tuple[LeftOut, ...]`" in normalized
    assert "and the reason for each" in normalized


def test_the_lifetime_table_lists_skipped_entries() -> None:
    table = _decision_block(D1_HEADING)
    assert "| skipped context entries | no | `TurnRequest.skipped` |" in table


def test_the_skip_reason_source_is_adr_0008_delta_row_2() -> None:
    block = _decision_block(D7_HEADING)
    normalized = _normalized(block)
    assert "ADR-0008 delta row 2 is the source of those reasons" in normalized
    assert "one typed outcome per provider" in normalized
    assert "carrying a stable reason code" in normalized


def test_the_amendment_delta_list_names_row_2() -> None:
    deltas = _decision_block("### Deltas against ADR-0008")
    assert "ADR-0008 delta 2" in _normalized(deltas)
    assert "it depends on the row" in _normalized(deltas).lower()


# --- D8 names the constructor and the rule ----------------------------------


def test_d8_names_the_per_origin_constructors() -> None:
    block = _decision_block(D8_HEADING)
    contract = _code_block_body(_adr(), "class ContextBlock:")
    for origin in ("from_provider", "from_operator", "from_run_instruction"):
        assert f"{origin}()" in block, f"{origin} missing from D8's semantics"
        assert f"def {origin}(" in contract, f"{origin} missing from D1's contract"
    assert "each sets `authority` from the origin it names" in _normalized(block)


def test_d8_authority_is_not_settable_by_an_ordinary_builder() -> None:
    block = _decision_block(D8_HEADING)
    contract = _code_block_body(_adr(), "class ContextBlock:")
    assert "init=False" in contract
    assert "`authority` is declared `init=False`" in _normalized(block)
    assert "`TypeError`" in block


def test_d8_binds_the_provider_path_to_the_material_builder() -> None:
    normalized = _normalized(_decision_block(D8_HEADING))
    assert "A provider's result becomes a block through" in normalized
    assert "which is material by construction" in normalized
    assert "no provider-supplied value can reach them" in normalized
    assert "This per-origin construction is what the amendment" in normalized
    assert "requires of the implementation" in normalized


# --- the ReAct removal is given a posture -----------------------------------


def test_react_removal_takes_the_skip_posture() -> None:
    block = _decision_block(D7_HEADING)
    normalized = _normalized(block)
    assert "The ReAct context removal, which today drops the caller's context" in normalized
    assert "takes **skip** on the history section" in normalized
    assert "so the removal is a stated fact rather than an unrecorded one" in normalized
    # The gap list walks the same removal and points at the posture that covers it.
    context = _decision_block("### Context — what the code does today")
    assert "D7 gives it the **skip** posture above" in _normalized(context)


# --- the gap list lives in the amendment's Context, not the Decision --------


def test_the_gap_list_is_in_the_amendment_context() -> None:
    context = _decision_block("### Context — what the code does today")
    assert "the compiler does not exist" in context
    assert "an eviction path exists outside the compiler" in context
    assert "What the code does today" not in _decision_block(D7_HEADING)
    assert "What the code does today" not in _decision_block(D8_HEADING)


def test_the_decisions_carry_no_delta_table_or_gap_list() -> None:
    body = _amendment_body()
    assert "\n|---|" not in body
    # Both headings exist, so the decision text is separated from the starting state.
    assert "### Context — what the code does today" in body
    assert "### Deltas against ADR-0008" in body


# --- the sentences the amendment had to stop saying -------------------------


def test_the_removed_false_claims_are_gone() -> None:
    text = _adr()
    for removed in (
        "no constructor admits a section without a bound record",
        "refusal exists for exactly one case",
        "one in-context cut marker does exist",
        "amendments are per-record",
        "the module does not exist yet",
        "dataclasses only",
        "| Delta (ADR-0008) | How this amendment narrows it |",
        "bounds: tuple[SectionBound, ...] = ()",
        "assertable without a live model",
    ):
        assert removed not in text, f"{removed!r} is still in the record"


def test_the_testability_claim_appears_once() -> None:
    text = _adr()
    assert _count_phrase(text, "highly testable without a live model") == 1


def test_header_carries_the_required_implementation_status() -> None:
    header = _adr().split("## Context", 1)[0]
    assert "- **Implementation-status**:" in header
    assert "not-started" in header


# --- what the record says the code does today ------------------------------

_PREPARE = "lionagi/operations/chat/_prepare.py"
_CONTEXT = "lionagi/tools/context/context.py"
_BRANCH = "lionagi/session/branch.py"
_PROVIDERS = "lionagi/protocols/context_providers.py"
_CODEX = "lionagi/providers/openai/codex.py"
_TYPES = "lionagi/operations/types.py"
_INSTRUCTION = "lionagi/protocols/messages/instruction.py"
_PREPARE_MSGS = "lionagi/protocols/messages/prepare.py"


def test_the_two_refusals_on_the_active_path_are_where_cited() -> None:
    _in_span(
        _PREPARE,
        295,
        295,
        'raise ValueError("First message in progression must be an Instruction or System")',
    )
    _in_span(_PREPARE, 341, 354, "raise EmptyOutgoingContentError(")


def test_the_empty_content_guard_tests_the_current_turn() -> None:
    _in_span(_PREPARE, 310, 311, "_current_turn_rendered = None")
    _in_span(_PREPARE, 330, 331, "if i == _last_index:", "_current_turn_rendered = rendered")
    _in_span(
        _PREPARE,
        341,
        341,
        "if _contents and _has_real_instruction_text(ins.content) and not _current_turn_rendered",
    )
    _in_span(
        _PREPARE,
        395,
        408,
        "def _has_real_instruction_text(content) -> bool:",
        "Deliberately ignores ``guidance``/``prompt_context``",
    )


def test_eviction_path_and_its_metadata_key_are_where_cited() -> None:
    _in_span(
        _CONTEXT,
        144,
        149,
        'if "current_progression" not in branch.metadata:',
        'branch.metadata["current_progression"] = cp',
    )
    _in_span(_CONTEXT, 290, 304, 'if action == "evict":', "cp.exclude(uids)")
    _in_span(
        _CONTEXT,
        306,
        323,
        'if action == "evict_action_results":',
        "cp.exclude(to_evict)",
    )
    _in_span(
        _BRANCH,
        248,
        254,
        "agent-managed subset",
        'cp = self.metadata.get("current_progression")',
    )


def test_compaction_path_replaces_a_span_with_one_summary() -> None:
    _in_span(_CONTEXT, 353, 430, 'if action == "compact":')
    _in_span(
        _CONTEXT,
        417,
        423,
        "[CONTEXT COMPACTION]",
        "cp.insert(summary_at, note.id)",
        "cp.exclude(collapse)",
    )


def test_codex_omission_markers_are_the_three_cited_shapes() -> None:
    _in_span(_CODEX, 995, 998, "[output truncated:", "characters omitted here]")
    _in_span(_CODEX, 1117, 1120, "items not shown]")
    _in_span(_CODEX, 1149, 1154, 'note = "[omitted]"', "keys not shown")
    # The file-change echo repeats the mapping's shape; its payload is the
    # ``{"omitted": ...}`` entry the amendment now names as its own shape.
    _in_span(_CODEX, 1209, 1212, '{"omitted":', "changes not shown")


def test_amendment_counts_three_codex_marker_shapes_and_cites_each() -> None:
    normalized = _normalized(_amendment_body())
    assert "in three shapes" in normalized
    for cited in (":995-998", ":1117-1120", ":1149-1154"):
        assert cited in normalized, f"{cited} is not cited for its marker shape"
    assert ":1209-1212" in normalized
    # The fourth site is the same mapping shape, said so, not a fourth shape.
    assert "A fourth site repeats the mapping's shape rather than adding a new one" in normalized
    # The ``{"omitted": ...}`` entry the file-change echo appends is described as its
    # own shape, so a reader is not left with the name-only "[omitted]" mapping entry.
    assert '`{"omitted": "<n> of <m> changes not shown"}`' in normalized
    assert "same mapping shape appears twice" in normalized


def test_the_refusal_prose_names_where_the_records_live() -> None:
    normalized = _normalized(_decision_block(D7_HEADING))
    assert (
        "the refusal arm raises before any provider call, and the bound records travel on the "
        "raised error's `bounds` attribute, so a refused turn remains countable without a "
        "compiled request" in normalized
    )


def test_d7_marker_text_names_section_count_and_reason() -> None:
    normalized = _normalized(_amendment_body())
    assert (
        "[LIONAGI_SECTION_TRUNCATED section=<section> left_out=<n> reason=<reason>]" in normalized
    )


def test_the_skipped_block_renders_the_same_marker() -> None:
    block = _decision_block(D7_HEADING)
    normalized = _normalized(block)
    assert "named in the same `[LIONAGI_SECTION_TRUNCATED …]` marker" in normalized
    assert "section=context" in normalized


def test_react_context_removal_is_listed_as_a_left_out() -> None:
    normalized = _normalized(_amendment_body())
    assert "a second, narrower left-out exists inside one ReAct turn's later calls" in normalized
    assert "(`lionagi/operations/chat/_prepare.py:191-232`)" in normalized
    # The removal the sentence cites is where the cited range shows it: the first
    # carrier keeps the context and the later carriers drop it.
    _in_span(
        _PREPARE,
        191,
        232,
        "_leave_out = set(carriers[1:])",
        "_without_context(ins, shown)",
        "if i in _leave_out:",
    )


def test_the_file_change_echo_call_site_is_where_cited() -> None:
    _in_span(_CODEX, 1384, 1384, "_bounded_changes(changes)")


def test_amendment_cites_the_value_error_guard_by_line() -> None:
    normalized = _normalized(_amendment_body())
    assert "raises a `ValueError` when the" in normalized
    assert "(`lionagi/operations/chat/_prepare.py:295`, cited by D2 §3 above)" in normalized


def test_operations_types_module_holds_the_operation_shapes() -> None:
    _in_span(_TYPES, 32, 32, "class ParseError(ValueError):")
    _in_span(_TYPES, 102, 102, "class ChatParam(MorphParam):")
    _in_span(_TYPES, 141, 141, "class RunParam(ChatParam):")
    _in_span(_TYPES, 182, 182, "class Middle(Protocol):")
    source = (REPO_ROOT / _TYPES).read_text(encoding="utf-8")
    for absent in ("CompiledChatRequest", "TurnRequest", "SectionAuthority", "SectionBound"):
        assert absent not in source, f"{absent} now exists; the amendment's 'today' list is stale"


def test_the_active_prepare_path_is_still_the_one_cited() -> None:
    _in_span(_PREPARE, 174, 180, "def _prepare_run_kwargs(")
    _in_span(_PREPARE, 180, 180, "context_blocks: Sequence[str] | None = None")
    _in_span(_PREPARE, 285, 286, 'injected = "\\n".join(context_blocks) if context_blocks else ""')
    _in_span(_PREPARE, 234, 239, "_collect_action_dicts(_act_res)")
    _in_span(
        _PREPARE,
        249,
        254,
        "current.with_updates(prompt_context=extended_ctx)",
    )
    _in_span("lionagi/operations/chat/chat.py", 62, 62, "_prepare_run_kwargs(")
    _in_span("lionagi/operations/run/run.py", 429, 429, "_prepare_run_kwargs(")
    _in_span(_PREPARE_MSGS, 100, 100, "def prepare_messages_for_chat(")


def test_the_registry_and_counters_are_where_cited() -> None:
    _in_span(_PROVIDERS, 115, 117, "if entry.max_tokens and tokens > entry.max_tokens:")
    _in_span(_PROVIDERS, 115, 117, "report.skipped.append(entry.name)")
    _in_span(_PROVIDERS, 129, 132, "if total + tokens > self.budget:")
    _in_span(_PROVIDERS, 25, 25, "_DEFAULT_BUDGET = 2000")
    _in_span(
        _PROVIDERS,
        63,
        70,
        '"recall_turns": 0,',
        '"blocks_injected": 0,',
        '"writeback_failed": 0,',
    )
    _in_span(_BRANCH, 291, 303, "def last_context_report(self):", "``skipped``")
    _in_span("lionagi/cli/monitor.py", 669, 680, '"blocks_injected",')


def test_the_untyped_authority_and_budget_shapes_are_where_cited() -> None:
    _in_span(
        _INSTRUCTION,
        206,
        218,
        '"Context": self.prompt_context,',
        'doc = {k: v for k, v in doc.items() if v not in (None, "", [], {})}',
    )
    _in_span(
        "lionagi/service/token_budget.py",
        88,
        88,
        "def lookup_context_window(model_name: str, provider: str | None = None) -> int:",
    )
    _in_span("lionagi/service/token_budget.py", 150, 150, "def get_token_budget(branch: Branch)")
    _in_span("lionagi/agent/nudge.py", 207, 207, "get_token_budget(self.branch)")

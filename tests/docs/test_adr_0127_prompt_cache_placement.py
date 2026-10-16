# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the decisions of ADR-0127 (prompt-cache placement) against its own prose.

Each test names one decision the record makes and asserts the sentence that makes
it, so a later reword cannot drop the decision while the record still reads as
though it holds. Real phrases are matched on whitespace-normalized text, so
reflowing a paragraph is free and removing a clause is not.
"""

from __future__ import annotations

import re
from pathlib import Path

ADR_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/adr/ADR-0127-prompt-cache-placement-for-prefix-caching-providers.md"
)


def _flat() -> str:
    return re.sub(r"\s+", " ", ADR_PATH.read_text(encoding="utf-8"))


def _decision(number: int) -> str:
    """The text of ``### D<number>`` up to the next ``###`` heading, normalized."""
    text = ADR_PATH.read_text(encoding="utf-8")
    start = text.index(f"### D{number}:")
    rest = text.index("\n### ", start) if "\n### " in text[start:] else len(text)
    return re.sub(r"\s+", " ", text[start:rest])


def test_scope_gate_requires_declaration_and_option() -> None:
    flat = _flat()
    assert "These decisions apply only when both gates are on" in flat
    assert "the selected provider declares that it caches by prompt prefix" in flat
    assert "the model configuration turns prompt caching on" in flat
    assert "every provider's compiled request is byte-identical to today's" in flat
    assert "every provider's compiled request is byte-identical to today's" in _decision(5)


def test_relations_name_every_amendment() -> None:
    flat = _flat()
    relations_start = flat.index("- **Relations**:")
    relations = flat[relations_start : flat.index("Purpose:", relations_start)]
    assert "amends ADR-0007 D1" in relations
    assert "D2 steps 2, 4 and 5, and D4" in relations
    assert "and ADR-0008 D4" in relations
    assert "supersedes" not in relations


def test_relation_to_adr_0126_names_the_claude_code_reference() -> None:
    flat = _flat()
    assert "the cache placement ADR-0126 R3 assigns to this record" in flat
    assert "Claude Code's placement (lion1 ADR-0028 S10 and A1)" in flat


def test_response_sections_render_once_in_the_stable_segment() -> None:
    d2 = _decision(2)
    assert "The response schema and the response format a run declares are fixed for the run" in d2
    assert "they render once, in the stable segment beside the tools" in d2
    assert "An instruction renders the same bytes whether it is current or history" in d2
    stable = _decision(1)
    assert "where D2 puts them there, the response sections a run declares" in stable


def test_native_tools_field_is_out_of_scope() -> None:
    flat = _flat()
    assert "The provider's native `tools` field is out of scope" in _decision(2)
    assert (
        "it is not written until a record maps the `tool_use` and `tool_result` blocks" in _flat()
    )
    assert "sorted by tool name" not in flat
    assert "a `tools` field" not in flat


def test_run_is_the_lifetime_of_one_branch_conversation_identity() -> None:
    flat = _flat()
    assert (
        "ADR-0126 D3 defines a **run** as the lifetime of one Branch conversation identity" in flat
    )
    assert "An agent run is a loop of model calls" not in flat
    run_class = _decision(1)
    assert "The Branch owns this segment" in run_class
    assert "the position where its previous request ended" in run_class
    assert "`_prepare_run_kwargs` reads from the branch it is handed" in _decision(4)


def test_context_blocks_are_a_second_block_of_the_system_field() -> None:
    d4 = _decision(4)
    assert "the run segment as a second block of the system field, after the stable block" in d4
    assert "each block carrying its own mark" in d4
    assert (
        "The turn segment's first message is the first user message, carrying only its own content"
        in d4
    )
    assert "Amends ADR-0008 D4" in d4
    assert '"refreshed per turn"' in d4
    assert "source attribution renders as it does today" in d4
    assert "moves to ADR-0007 D3's canonical JSON once that record is accepted" in d4
    assert "with their source attribution" not in _flat()


def test_marks_take_the_recorded_position_and_come_only_from_the_option() -> None:
    d5 = _decision(5)
    assert (
        "the segment boundaries and the position in this request where the previous request ended"
        in d5
    )
    assert "one five-minute mark at that recorded position" in d5
    assert "Marks come only from this option" in d5
    assert "is removed" in d5
    assert "the request-option filter stops dropping the marks the adapter writes" not in _flat()


def test_minimum_cacheable_length_is_stated_the_same_way_in_context_and_decision() -> None:
    assert _flat().count("shorter than the model's minimum cacheable length") == 2


def test_consequences_state_the_write_prices() -> None:
    consequences_start = _flat().index("## Consequences")
    consequences = _flat()[consequences_start:]
    assert "1.25 times input for a five-minute entry" in consequences
    assert "2 times for a one-hour entry" in consequences
    assert "the read price (0.1 times input)" in consequences
    assert "full input price for the stable and run segments" not in _flat()


def test_history_rewrite_is_a_counted_turn_segment_change() -> None:
    d1 = _decision(1)
    assert "A **history rewrite** is a turn-segment change" in d1
    assert "it breaks the prefix from the rewritten message on" in d1
    assert "ADR-0125 step 2" in d1
    assert "`operations/chat/_prepare.py:191-247`" in d1
    assert "counts segment changes, including a history rewrite" in _decision(6)


def test_guidance_is_turn_segment_not_stable() -> None:
    flat = _flat()
    assert "stable guidance" not in flat
    assert "guidance fixed for the agent" not in flat
    assert "Per-call `guidance` stays in the instruction, in the turn segment" in _decision(3)
    assert "it is not part of the stable block" in _decision(3)


def test_accounting_names_the_per_lifetime_write_fields() -> None:
    d6 = _decision(6)
    assert "the five-minute and one-hour cache-creation token counts" in d6
    assert "`session/signal.py:290-294`" in d6
    assert "this record extends it to carry both" in d6


def test_acceptance_names_three_tests_on_the_api_chat_path() -> None:
    d7 = _decision(7)
    assert "Four tests, all written to pass after this record's change" in d7
    assert "drives the API chat path on a scripted endpoint" in d7
    assert "the tool section appears once in each request, in the system block" in d7
    assert "with the prompt-caching option off" in d7
    assert "byte-identical to today's" in d7
    assert "a separate characterization" in d7


def test_per_turn_response_format_lands_in_the_turn_segment() -> None:
    d2 = _decision(2)
    assert "lands in the turn segment, as that turn's content" in d2
    assert "never rewrites the stable segment" in d2
    assert "the one-hour write price again" in d2
    assert "the stable segment to stay byte-identical across every request of the run" in _decision(
        7
    )


def test_adapter_mark_removal_is_named_as_a_bill_change() -> None:
    flat = _flat()
    phrase = "changes today's bill for callers who get a five-minute cache write on every call"
    assert "no existing caller's bill changes" not in flat
    assert phrase in flat
    assert phrase in _decision(5)
    assert phrase in flat[flat.index("## Consequences") :]


def test_measured_lengths_are_labelled_as_json_encoded() -> None:
    flat = _flat()
    assert "3,170 characters of JSON-encoded content" in flat
    assert "from 3,240 to 112 characters of JSON-encoded content" in flat
    assert "shares no message-level prefix with request 0 at all" in flat

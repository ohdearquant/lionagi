# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the decisions of ADR-0127 (prompt-cache placement) against its own prose.

These are phrase pins, not acceptance tests. Each test names a clause the record decides and asserts
that clause verbatim, so a later reword cannot drop the decision while the record still reads as
though it holds. Real phrases are matched on whitespace-normalized text, so reflowing a paragraph is
free and removing a clause is not. D7's acceptance tests are specifications for the record's
implementation; this file does not implement them.
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
    """The text of ``### D<number>`` up to the next heading, normalized."""
    text = ADR_PATH.read_text(encoding="utf-8")
    start = text.index(f"### D{number}:")
    heading = re.search(r"\n#{2,3} ", text[start:])
    end = start + heading.start() if heading else len(text)
    return re.sub(r"\s+", " ", text[start:end])


def test_scope_gate_requires_declaration_and_option() -> None:
    flat = _flat()
    assert "These decisions apply only when both gates are on" in flat
    assert "the selected provider declares that it accepts a native system block" in flat
    assert "and caches by prompt prefix" in flat
    assert "the model configuration turns prompt caching on" in flat


def test_option_off_is_byte_identical_on_every_path_with_no_exception() -> None:
    flat = _flat()
    assert "There is no ungated change" in flat
    assert "every provider's compiled request is byte-identical to today's on every path" in flat
    assert (
        "every provider's compiled request is byte-identical to today's on every path"
        in _decision(5)
    )
    assert "the mark removal" not in flat
    assert "changes today's bill" not in flat


def test_the_mark_stays_wherever_it_fires_today_while_the_option_is_off() -> None:
    flat = _flat()
    assert "the Anthropic adapter's `cache_control`-triggered last-message mark" in flat
    assert "stays exactly as today wherever it fires today" in flat
    d5 = _decision(5)
    assert "byte-identical to today's on every path" in d5
    assert "the adapter's `cache_control`-triggered last-message mark" in d5
    assert "included wherever it fires today" in d5


def test_p4_states_both_paths_for_the_flag() -> None:
    flat = _flat()
    preamble = flat[flat.index("## Decision") : flat.index("### D1:")]
    assert (
        "on a default `iModel` the request-option filter drops the flag before the mark code, so no "
        "mark is placed; on an endpoint with no request-option filter" in preamble
    )
    assert (
        "the flag reaches the mark code and one five-minute mark lands on the last message"
        in preamble
    )
    assert (
        "on a default `iModel` the request-option filter drops the flag before the mark code"
        in flat
    )
    assert "the request-option filter drops the flag" in flat
    assert "on an endpoint with no request-option filter" in flat
    assert "an `iModel` restored with `request_options` None, or a dict-configured model" in flat
    assert "the flag reaches the mark code" in flat
    assert "one ephemeral five-minute mark lands on the last message's last block" in flat


def test_relations_name_every_amendment_and_dependency() -> None:
    flat = _flat()
    relations_start = flat.index("- **Relations**:")
    relations = flat[relations_start : flat.index("Purpose:", relations_start)]
    assert "amends ADR-0007 D1" in relations
    assert "D2 steps 2, 4 and 5, and D4" in relations
    assert "and ADR-0008 D4" in relations
    assert "depends on ADR-0018 D2" in relations
    assert "ADR-0027 D4" in relations
    assert "is the one promise D4 changes, and only while the option is on" in relations
    assert "supersedes" not in relations


def test_relation_to_adr_0126_names_the_claude_code_reference() -> None:
    flat = _flat()
    assert "the cache placement ADR-0126 R3 assigns to this record" in flat
    assert "Claude Code's placement (lion1 ADR-0028 S10 and A1" in flat
    assert "a claim external to this repository's ADR corpus" in flat
    assert "held here at `lion1/docs/adr/028-anthropic/`" in flat


def test_stable_segment_is_the_system_text_and_the_tool_definitions_only() -> None:
    d2 = _decision(2)
    assert "The stable segment is the system text and the tool definitions only" in d2
    stable = _decision(1)
    assert "- **stable**: the system text and the tool definitions" in stable
    assert "the response sections a run declares" not in _flat()
    assert "the response format a run declares" not in _flat()


def test_response_sections_render_in_the_turn_segment() -> None:
    d2 = _decision(2)
    assert "Every response schema and response format renders in the turn segment" in d2
    assert "as part of the instruction that asks for it" in d2
    assert "renders the same bytes whether it is current or history" in d2
    assert "a run declares" not in _flat()
    assert "The rendered schema is the `ResponseSchema` of the request operative" in d2
    assert "`reason` when the call asks for it" in d2
    assert (
        "`action_required`, `action_requests` and `action_responses` when the call passes actions"
        in d2
    )
    assert "the response sections beside the tools" not in _flat()
    assert "in the stable segment beside the tools" not in _flat()


def test_stable_tool_definitions_come_from_the_conversation_tool_set() -> None:
    d2 = _decision(2)
    assert (
        "They are the conversation's tool set — the tools the Branch exposes to the operation" in d2
    )
    assert "not the per-call `action_param`" in d2
    assert "A call that requests no action, such as `ReAct`'s final answer" in d2
    assert "operations/ReAct/ReAct.py" in d2
    assert "sets `action_param` None" in d2
    assert "still carries the same tool section in the stable segment" in d2
    assert "its turn content says that no action is requested" in d2
    assert "is therefore a counted stable-segment change (D6)" in d2


def test_native_tools_field_is_out_of_scope() -> None:
    flat = _flat()
    assert "The provider's native `tools` field is out of scope" in _decision(2)
    assert (
        "it is not written until a record maps the `tool_use` and `tool_result` blocks" in _flat()
    )
    assert "sorted by tool name" not in flat
    assert "a `tools` field" not in flat


def test_conversation_segment_lifetime_and_its_lock() -> None:
    flat = _flat()
    assert "ADR-0126 D3 defines a **run**, for this router" in flat
    assert "ADR-0123 D1 defines a **Run** as exactly one admitted execution attempt" in flat
    assert "This record uses **conversation** for the first sense" in flat
    assert "A retried attempt (an ADR-0123 run) inside one conversation reuses the segment" in flat
    d1 = _decision(1)
    assert "ends when the Branch is closed or when its conversation identity changes" in d1
    assert "both end events discard the segment" in d1
    assert (
        "keyed by the Branch's conversation identity and built once, under the Branch lock a top-level "
        "turn holds" in d1
    )
    assert "so ADR-0008 D4's call-local rule holds for everything else a call threads" in d1
    assert "run segment" not in flat
    assert "until the run ends" not in flat


def test_context_blocks_are_the_system_blocks_after_the_stable_block() -> None:
    d4 = _decision(4)
    assert "in the system field as the conversation blocks, after the stable block" in d4
    assert "with one mark at the end of the conversation segment (D5)" in d4
    assert "each block carrying its own mark" not in d4
    assert (
        "The turn segment's first message is the first user message, carrying only its own content"
        in d4
    )
    assert "Amends ADR-0008 D4" in d4
    assert '"refreshed per turn"' in d4
    assert "source attribution stays in `ProviderReport.fired`" in d4
    assert "moves to ADR-0007 D3's canonical JSON once that record is accepted" in d4
    assert (
        "the adapter must accept an ordered list of system blocks and mark the end of each segment, "
        "rather than lifting a single message" in d4
    )
    assert "ADR-0027 D4 owns that boundary" in d4
    assert "renders as it does today" not in _flat()


def test_marks_take_the_recorded_position_and_come_only_from_the_option() -> None:
    d5 = _decision(5)
    assert (
        "the ordered lists — the stable blocks and the conversation blocks — and the position in this "
        "request where the previous request ended" in d5
    )
    assert "one mark with a one-hour lifetime at the end of the stable segment" in d5
    assert "one five-minute mark at the end of the conversation segment when it is non-empty" in d5
    assert "one five-minute mark on the last block of the turn segment" in d5
    assert (
        "when a turn adds more than the provider's lookback, one five-minute mark at that recorded "
        "position, on the previous request's last block" in d5
    )
    assert "At most four marks" in d5
    assert "one-hour marks precede five-minute marks" in d5
    assert "this decision's placement replaces that one `cache_control`-triggered mark" in d5
    assert "the adapter's own last-message mark is not placed on top of it" in d5
    assert "marks come only from this option" in d5
    assert (
        "The adapter must accept an ordered list of system blocks and mark the end of each segment "
        "(D4)" in d5
    )
    assert "the compiler does not emit the native system field" in d5
    assert "the request-option filter stops dropping the marks the adapter writes" not in _flat()
    assert "put one mark on each" not in _flat()


def test_prompt_caching_is_a_configuration_option_outside_the_body() -> None:
    d5 = _decision(5)
    assert (
        "The option is `prompt_caching` (bool, default False), a field of the endpoint configuration"
        in d5
    )
    assert "reads it from that configuration before the request-option filter runs" in d5
    assert "never written into the request body" in d5


def test_minimum_cacheable_length_is_stated_the_same_way_in_context_and_decision() -> None:
    flat = _flat()
    context = flat[flat.index("## Context") : flat.index("## Decision")]
    clause = "shorter than the model's minimum cacheable length"
    assert clause in context
    assert clause in _decision(5)
    assert "is processed without caching and without an error" in context
    assert "A mark is placed whatever the length of its prefix" in _decision(5)
    assert "minimum cacheable length is not placed" not in _decision(5)


def test_context_states_the_read_price_per_model() -> None:
    flat = _flat()
    context = flat[flat.index("## Context") : flat.index("## Decision")]
    assert "a read 0.1 times for most models" in context
    assert "0.05 times for Opus 5.5 and Sonnet 5.5" in context
    assert "0.025 times for Fable 5.1 and Mythos 5.1" in context


def test_consequences_state_the_write_prices() -> None:
    consequences_start = _flat().index("## Consequences")
    consequences = _flat()[consequences_start:]
    assert "once per entry lifetime" in consequences
    assert "1.25 times input for a five-minute entry" in consequences
    assert "2 times for a one-hour entry" in consequences
    assert "the read price (0.1 times input, less on the newest models)" in consequences
    assert "full input price for the stable and run segments" not in _flat()


def test_history_rewrite_is_a_counted_turn_segment_change() -> None:
    d1 = _decision(1)
    assert "A **history rewrite** is a turn-segment change" in d1
    assert "it breaks the prefix from the rewritten message on" in d1
    assert "the hide, show and summarize directives of ADR-0125 step 2" in d1
    assert "operations/chat/_prepare.py" in d1
    assert "context dedupe" not in d1
    assert "counts segment changes, including a history rewrite" in _decision(6)


def _amendment(number: int) -> str:
    """The text of ``## Amendment <number>`` up to the next level-two heading, normalized."""
    text = ADR_PATH.read_text(encoding="utf-8")
    start = text.index(f"## Amendment {number} ")
    heading = re.search(r"\n## ", text[start + 1 :])
    end = start + 1 + heading.start() if heading else len(text)
    return re.sub(r"\s+", " ", text[start:end])


def test_guidance_is_turn_segment_not_stable() -> None:
    outside_amendment = _flat().replace(_amendment(1), "")
    assert "stable guidance" not in outside_amendment
    assert "guidance fixed for the agent" not in _flat()
    assert "Per-call `guidance` stays in the instruction, in the turn segment" in _decision(3)
    assert "it is not part of the stable block" in _decision(3)


def test_amendment_1_moves_only_middle_owned_guidance_and_only_with_the_option_on() -> None:
    am1 = _amendment(1)
    assert "Status: Proposed. Extends D1, D2, D3, D5 and D7" in am1
    assert (
        "compiled requests keep their previous bytes, wording and guidance placement, and no loop "
        "default changes" in am1
    )
    assert "Request compilation reads both only from there, never from ambient context" in am1
    assert (
        "Guidance the caller supplies keeps its place in the first round's instruction and is never "
        "copied into the stable guidance" in am1
    )
    assert "a repair never rebuilds it" in am1
    assert "D6 counts it before the request is sent" in am1
    assert "which does not establish a quality difference in either direction" in am1
    assert (
        "Amendment 1 qualifies ADR-0132 Amendment 2's placement of the first round's guidance"
        in (_flat())
    )
    adr_0132 = re.sub(
        r"\s+",
        " ",
        (ADR_PATH.parent / "ADR-0132-batched-lndl-rounds-for-api-model-legs.md").read_text(
            encoding="utf-8"
        ),
    )
    assert "ADR-0127 Amendment 1 (Proposed) qualifies this placement" in adr_0132


def test_amendment_2_keeps_a_turns_context_at_that_turn_only_with_the_option_on() -> None:
    am2 = _amendment(2)
    assert (
        "Status: Proposed. Replaces D4's placement of context blocks and empties D1's "
        "conversation segment" in am2
    )
    assert (
        "With the option off, or on an endpoint without the declaration, compiled requests keep "
        "their previous bytes" in am2
    )
    assert "a gather repeated for the same instruction does not replace them" in am2
    assert "so it never rewrites an earlier message" in am2
    assert "Nothing is placed in the system field" in am2
    assert "D5's mark at its end is never placed" in am2
    assert "the kept blocks survive a serialized round trip" in am2


def test_the_adapter_builds_the_native_system_block() -> None:
    d3 = _decision(3)
    assert (
        "the compiler hands the adapter the stable blocks — the system text and the tool section — as "
        "an ordered list (D5)" in d3
    )
    assert "the adapter is what builds the provider's native system field (ADR-0027 D4)" in d3
    assert "and on ADR-0027 D4" in _flat()


def test_accounting_names_the_usage_fields_the_extractor_reads() -> None:
    d6 = _decision(6)
    assert "`cache_read_input_tokens` and `cache_creation_input_tokens`" in d6
    assert "session/signal.py" in d6
    assert "This record extends it to carry both" in d6
    assert "`ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`" in d6
    assert "lion1/hub/backend/anthropic.py" in d6


def test_d7_lists_six_tests_and_the_api_chat_path() -> None:
    d7 = _decision(7)
    assert "Six tests, all written to pass after this record's change" in d7
    assert "drives the API chat path on a scripted endpoint" in d7
    assert "the tool section appears once in each request, in the system block" in d7
    assert "with the prompt-caching option off" in d7
    assert (
        "on both an endpoint whose request-option filter is present and one whose `request_options` is "
        "None" in d7
    )
    assert "byte-identical to today's on both paths" in d7
    assert (
        "requires the tool section's bytes to be identical in every request of the conversation, the "
        "final call included" in d7
    )
    assert "require the key to be absent from the request body in both" in d7
    assert "a separate characterization" in d7


def test_tool_and_response_sections_do_not_rewrite_the_stable_segment() -> None:
    d2 = _decision(2)
    assert (
        "A format that changes between turns therefore renders in the turn segment and never rewrites "
        "the stable segment." in d2
    )
    assert "the one-hour write price again" in d2


def test_measured_lengths_are_labelled_as_json_dumps_of_the_message_object() -> None:
    flat = _flat()
    assert (
        "about 3,170 characters of `json.dumps` of the whole message object, role wrapper included"
        in flat
    )
    assert "3,141" in flat
    assert "from 3,240 to 112 characters of `json.dumps` of the whole message object" in flat
    assert "3,211 to 83" in flat
    assert "46 characters of raw text" in flat
    assert "shares no message-level prefix with request 0 at all" in flat


def test_decision_table_counts_turn_segment_changes() -> None:
    flat = _flat()
    assert "a turn segment whose changes are counted" in flat
    assert "append-only" not in flat


def test_p1_cites_the_renderer_keys_span() -> None:
    flat = _flat()
    assert "While an instruction is current it renders its `Tools`, `ResponseSchema` and" in flat
    assert "protocols/messages/instruction.py" in flat


def test_amended_records_carry_a_pointer_to_this_record() -> None:
    adr_dir = ADR_PATH.parent
    for name in (
        "ADR-0007-canonical-turn-request-compilation-boundary.md",
        "ADR-0008-pre-turn-context-provider-execution-and-attribution.md",
    ):
        text = re.sub(r"\s+", " ", (adr_dir / name).read_text(encoding="utf-8"))
        assert "Amended by ADR-0127" in text, name


def test_the_record_names_its_instruction_and_the_part_it_defers() -> None:
    flat = _flat()
    head = flat[flat.index("- **Relations**:") : flat.index("Purpose:")]
    assert (
        "**Instruction.** This record serves an instruction the project's maintainer gave on 2026-10-16"
        in head
    )
    assert "the caching management, mainly" in head
    assert "commonly 1 hour" in head
    assert (
        "This record departs from that part: the per-request input ceiling and the context management past it are deferred to their own record, not yet written and not yet numbered"
        in head
    )
    assert (
        "which the instruction this record serves asked for (deferred to their own record" in flat
    )


def test_the_stable_class_counts_the_branch_tool_set_as_configuration() -> None:
    d1 = _decision(1)
    assert (
        "It is the same for every call of one configuration, and the tool set the Branch exposes (D2) is part of that configuration."
        in d1
    )
    assert "every call of every conversation" not in d1

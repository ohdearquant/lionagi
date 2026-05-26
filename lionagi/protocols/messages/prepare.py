# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue

from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression
from lionagi.protocols.messages.action_request import ActionRequestContent
from lionagi.protocols.messages.action_response import ActionResponseContent
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.protocols.messages.instruction import InstructionContent
from lionagi.protocols.messages.message import MessageContent
from lionagi.protocols.messages.system import SystemContent

if TYPE_CHECKING:
    from lionagi.protocols.messages import Message

__all__ = ("prepare_messages_for_chat",)

# ---------------------------------------------------------------------------
# Type aliases — production content types are canonical.
# ---------------------------------------------------------------------------
Instruction = InstructionContent
System = SystemContent
ActionRequest = ActionRequestContent
ActionResponse = ActionResponseContent
Assistant = AssistantResponseContent
RoledContent = MessageContent


def _get_text(content: MessageContent, attr: str) -> str:
    """Get text from content attr, returning '' if None/missing."""
    val = getattr(content, attr, None)
    return "" if val is None else str(val)


def _build_context(content: InstructionContent, action_outputs: list[str]) -> list[JsonValue]:
    """Build context list by appending action outputs to existing context."""
    existing = content.prompt_context
    if not existing:
        return cast("list[JsonValue]", list(action_outputs))
    return cast("list[JsonValue]", list(cast("list[JsonValue]", existing)) + action_outputs)


def _aggregate_round_actions(
    requests: list[ActionRequestContent],
    responses: list[ActionResponseContent],
    round_num: int,
) -> str:
    """Aggregate action request/response pairs into a compact round summary."""
    ts = int(time.time())
    lines = [f"round: {round_num}", f"time: {ts}"]

    if not responses:
        return "\n".join(lines)

    lines.append("actions:")
    for i, resp in enumerate(responses):
        req = requests[i] if i < len(requests) else None
        call_str = req.render_compact() if req else "unknown()"
        status = resp.render_summary()
        lines.append(f"  - {call_str}: {status}")

    return "\n".join(lines)


def _build_round_notification(
    progression: Progression | None,
    round_num: int,
    msg_count: int,
    scratchpad: dict[str, str] | None = None,
) -> str:
    """Build system notification block for agent grounding between rounds."""
    ts = int(time.time())

    capabilities: set[str] = set()
    resources: set[str] = set()

    # Branch is session-layer; import lazily to avoid circular dependency.
    try:
        from lionagi.beta.session.session import Branch  # type: ignore[import]

        if isinstance(progression, Branch):
            capabilities = getattr(progression, "capabilities", set())
            resources = getattr(progression, "resources", set())
    except ImportError:
        pass

    parts = [f'<system round="{round_num}" time="{ts}">']
    if capabilities:
        parts.append(f"  tools: [{', '.join(sorted(capabilities))}]")
    if resources:
        parts.append(f"  resources: [{', '.join(sorted(resources))}]")
    parts.append(f"  context: {msg_count} msgs | round {round_num}")
    if scratchpad:
        scratch_lines = ["  scratchpad:"]
        for k, v in scratchpad.items():
            v_str = str(v)
            scratch_lines.append(f"    {k}: {v_str}")
        parts.extend(scratch_lines)
    parts.append("</system>")
    return "\n".join(parts)


def prepare_messages_for_chat(
    messages: Pile[Message],
    progression: Progression | None = None,
    new_instruction: Message | InstructionContent | None = None,
    to_chat: bool = True,
    system_prefix: str | None = None,
    aggregate_actions: bool = False,
    round_notifications: bool = False,
    scratchpad: dict[str, str] | None = None,
) -> list[MessageContent] | list[dict[str, Any]]:
    """Prepare messages for chat API with intelligent content organization.

    Algorithm:
    1. Auto-detect system message from first message (if SystemContent)
    1b. Prepend system_prefix if provided (e.g., LNDL format instructions)
    2. Collect action messages and embed into following instruction's context
       - aggregate_actions=True: correlate requests/responses, produce compact summaries
       - aggregate_actions=False: render each ActionResponse individually (legacy)
    3. Merge consecutive AssistantResponses
    4. Embed system into first instruction
    5. Append new_instruction
    """
    # Resolve message sequence — apply progression ordering if provided.
    to_use: Pile[Message] = messages if progression is None else messages[progression]

    if len(to_use) == 0:
        if new_instruction:
            new_content = (
                new_instruction.content  # type: ignore[union-attr]
                if _is_message(new_instruction)
                else new_instruction
            )
            new_content: InstructionContent = new_content.with_updates(copy_containers="deep")
            if to_chat:
                chat_msg = {
                    "role": new_content.role.value,
                    "content": new_content.rendered,
                }
                if chat_msg and chat_msg.get("content"):
                    return [chat_msg]
                return []
            return [new_content]
        return []

    # Phase 1: Extract system message (auto-detect from first message)
    system_text: str | None = None
    start_idx = 0

    first_msg = to_use[0]
    first_content = _get_content(first_msg)
    if isinstance(first_content, SystemContent):
        system_text = first_content.render()
        start_idx = 1

    # Phase 1b: Prepend system_prefix (e.g., LNDL prompt)
    if system_prefix:
        system_text = f"{system_prefix}\n\n{system_text}" if system_text else system_prefix

    # Phase 2: Process messages — collect action outputs for next instruction
    _use_msgs: list[MessageContent] = []
    pending_actions: list[str] = []
    pending_requests: list[ActionRequestContent] = []
    pending_responses: list[ActionResponseContent] = []
    round_num = 1
    msg_count = len(to_use)

    for i, msg in enumerate(to_use):
        if i < start_idx:
            continue

        content: MessageContent = _get_content(msg)

        if isinstance(content, ActionRequestContent):
            if aggregate_actions:
                pending_requests.append(content)
            continue

        if isinstance(content, ActionResponseContent):
            if aggregate_actions:
                pending_responses.append(content)
            else:
                pending_actions.append(content.render())
            continue

        # System in middle: skip
        if isinstance(content, SystemContent):
            continue

        # Instruction: embed pending action outputs
        if isinstance(content, InstructionContent):
            # Clear tool_schemas and response_format from history messages
            updates: dict[str, Any] = {"tool_schemas": None, "response_format": None}

            if aggregate_actions and pending_responses:
                context_parts: list[str] = []
                if round_notifications:
                    context_parts.append(
                        _build_round_notification(progression, round_num, msg_count, scratchpad)
                    )
                context_parts.append(
                    _aggregate_round_actions(pending_requests, pending_responses, round_num)
                )
                updates["context"] = _build_context(content, context_parts)
                pending_requests.clear()
                pending_responses.clear()
                round_num += 1
            elif pending_actions:
                updates["context"] = _build_context(content, pending_actions)
                pending_actions = []
                round_num += 1

            _use_msgs.append(content.with_updates(**updates))
            continue

        # Other (AssistantResponse, non-aggregated ActionRequest): copy as-is
        _use_msgs.append(content.with_updates())

    # Phase 3: Merge consecutive AssistantResponses
    if len(_use_msgs) > 1:
        merged: list[MessageContent] = [_use_msgs[0]]
        for content in _use_msgs[1:]:
            if isinstance(content, AssistantResponseContent) and isinstance(
                merged[-1], AssistantResponseContent
            ):
                prev = _get_text(merged[-1], "assistant_response")
                curr = _get_text(content, "assistant_response")
                merged[-1] = AssistantResponseContent(assistant_response=f"{prev}\n\n{curr}")
            else:
                merged.append(content)
        _use_msgs = merged

    # Phase 4: Embed system message into first instruction
    system_embedded = False
    if system_text:
        if len(_use_msgs) == 0 and new_instruction:
            # No history: embed into new_instruction
            new_content_inner = (
                new_instruction.content  # type: ignore[union-attr]
                if _is_message(new_instruction)
                else new_instruction
            )
            if isinstance(new_content_inner, InstructionContent):
                curr = _get_text(new_content_inner, "instruction")
                system_updates: dict[str, Any] = {"primary": f"{system_text}\n\n{curr}"}
                ctx_parts: list[str] = []
                if aggregate_actions and pending_responses:
                    if round_notifications:
                        ctx_parts.append(
                            _build_round_notification(progression, round_num, msg_count, scratchpad)
                        )
                    ctx_parts.append(
                        _aggregate_round_actions(pending_requests, pending_responses, round_num)
                    )
                    pending_requests.clear()
                    pending_responses.clear()
                elif pending_actions:
                    ctx_parts.extend(pending_actions)
                    pending_actions = []
                if ctx_parts:
                    system_updates["context"] = _build_context(new_content_inner, ctx_parts)
                _use_msgs.append(new_content_inner.with_updates(**system_updates))
                new_instruction = None
                system_embedded = True
        elif _use_msgs and isinstance(_use_msgs[0], InstructionContent):
            curr = _get_text(_use_msgs[0], "instruction")
            _use_msgs[0] = _use_msgs[0].with_updates(primary=f"{system_text}\n\n{curr}")
            system_embedded = True

    # Phase 5: Append new_instruction (with any remaining action outputs)
    if new_instruction:
        final_updates: dict[str, Any] = {}
        new_content_final = (
            new_instruction.content  # type: ignore[union-attr]
            if _is_message(new_instruction)
            else new_instruction
        )
        if isinstance(new_content_final, InstructionContent):
            context_parts_final: list[str] = []
            if aggregate_actions and pending_responses:
                if round_notifications:
                    context_parts_final.append(
                        _build_round_notification(progression, round_num, msg_count, scratchpad)
                    )
                context_parts_final.append(
                    _aggregate_round_actions(pending_requests, pending_responses, round_num)
                )
                pending_requests.clear()
                pending_responses.clear()
            elif pending_actions:
                context_parts_final.extend(pending_actions)
                pending_actions = []
            if context_parts_final:
                final_updates["context"] = _build_context(new_content_final, context_parts_final)
            if system_text and not system_embedded:
                curr = _get_text(new_content_final, "instruction")
                final_updates["primary"] = f"{system_text}\n\n{curr}"
        _use_msgs.append(new_content_final.with_updates(**final_updates))

    if to_chat:
        result = []
        for m in _use_msgs:
            result.append(
                {
                    "role": m.role.value,
                    "content": m.rendered,
                }
            )
        return result
    return _use_msgs


def _is_message(obj: Any) -> bool:
    """Check if obj is a session Message (duck-typed, no hard import)."""
    return hasattr(obj, "content") and isinstance(getattr(obj, "content", None), MessageContent)


def _get_content(msg: Any) -> MessageContent:
    """Extract MessageContent from a Message or return as-is if already MessageContent."""
    if isinstance(msg, MessageContent):
        return msg
    return msg.content  # type: ignore[union-attr]

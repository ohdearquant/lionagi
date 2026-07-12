# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue

from lionagi.ln._to_list import to_list
from lionagi.protocols.messages import (
    ActionResponse,
    AssistantResponse,
    Instruction,
    MessageRole,
)
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.protocols.messages.instruction import InstructionContent
from lionagi.protocols.messages.message import Message, MessageContent

from ..types import ChatParam, RunParam

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


@dataclass
class _PreparedContent:
    """A prepared content item and, when safe, its reusable source rendering."""

    source: Message | None = None
    cache_variant: str | None = None
    content: MessageContent | None = None

    @property
    def base_content(self) -> MessageContent:
        if self.content is not None:
            return self.content
        if self.source is not None:
            return self.source.content
        raise RuntimeError("Prepared content has neither source nor content.")

    @property
    def role(self) -> MessageRole:
        return self.base_content.role

    def materialize(self) -> MessageContent:
        """Build a mutable overlay only for a transformation boundary."""
        if self.content is None:
            if self.source is None:
                raise RuntimeError("Prepared content has neither source nor content.")
            if self.cache_variant == "prepared_instruction":
                self.content = self.source.content.with_updates(
                    tool_schemas=[], response_format=None
                )
            elif self.cache_variant == "prepared_assistant":
                self.content = self.source.content.with_updates()
            else:
                self.content = self.source.content
            self.source = None
            self.cache_variant = None
        return self.content


def _build_instruction(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    param: ChatParam,
) -> Instruction:
    to_exclude = {"imodel", "imodel_kw", "include_token_usage_to_model", "progression"}
    if isinstance(param, RunParam):
        to_exclude.add("stream_persist")
        to_exclude.add("persist_dir")
        to_exclude.add("snapshot_dir")

    params = param.to_dict(exclude=to_exclude)
    params["sender"] = param.sender or branch.user or "user"
    params["recipient"] = param.recipient or branch.id
    params["instruction"] = instruction

    return branch.msgs.create_instruction(**params)


def _prepare_run_kwargs(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    param: ChatParam,
    *,
    ins: Instruction | None = None,
    _use_render_cache: bool = True,
) -> tuple[Instruction, dict]:
    if ins is None:
        ins = _build_instruction(branch, instruction, param)

    _use_ins_content = None
    _contents: list[_PreparedContent] = []
    _act_res = []
    progression = param.progression or branch.progression

    for msg in (branch.msgs.messages[j] for j in progression):
        if isinstance(msg, ActionResponse):
            _act_res.append(msg)

        elif isinstance(msg, AssistantResponse):
            _contents.append(
                _PreparedContent(
                    source=msg,
                    cache_variant="prepared_assistant",
                )
            )

        elif isinstance(msg, Instruction):
            updates = {"tool_schemas": [], "response_format": None}
            has_action_context = bool(_act_res)

            if _act_res:
                d_ = _collect_action_dicts(_act_res)
                extended_ctx = list(msg.content.prompt_context)
                extended_ctx.extend(z for z in d_ if z not in extended_ctx)
                updates["prompt_context"] = extended_ctx
                _act_res = []

            _contents.append(
                _PreparedContent(
                    source=msg if not has_action_context else None,
                    cache_variant="prepared_instruction" if not has_action_context else None,
                    content=(msg.content.with_updates(**updates) if has_action_context else None),
                )
            )

    if _act_res:
        d_ = _collect_action_dicts(_act_res)
        extended_ctx = list(ins.content.prompt_context)
        extended_ctx.extend(z for z in d_ if z not in extended_ctx)
        _use_ins_content = ins.content.with_updates(prompt_context=extended_ctx)

    _contents = [entry for entry in _contents if entry.role != MessageRole.UNSET]

    # Merge consecutive assistant responses
    if len(_contents) > 1:
        merged = [_contents[0]]
        for entry in _contents[1:]:
            content = entry.base_content
            if isinstance(content, AssistantResponseContent):
                if isinstance(merged[-1].base_content, AssistantResponseContent):
                    previous = merged[-1]
                    previous_content = previous.materialize()
                    previous_content.assistant_response = (
                        f"{previous_content.assistant_response}\n\n{content.assistant_response}"
                    )
                else:
                    merged.append(entry)
            else:
                if isinstance(merged[-1].base_content, AssistantResponseContent):
                    merged.append(entry)
        _contents = merged

    if branch.msgs.system:

        def f(c):
            g = c.guidance or ""
            if not isinstance(g, str):
                from lionagi.libs.schema.minimal_yaml import minimal_yaml

                g = minimal_yaml(g).strip()
            turn_injections = branch._context_injection_slot
            injected = "\n".join(turn_injections) if turn_injections else ""
            return branch.msgs.system.rendered + injected + g

        if len(_contents) == 0:
            _contents.append(
                _PreparedContent(content=ins.content.with_updates(guidance=f(ins.content)))
            )
        elif len(_contents) >= 1:
            first = _contents[0].materialize()
            if not isinstance(first, InstructionContent):
                raise ValueError("First message in progression must be an Instruction or System")
            _contents[0] = _PreparedContent(content=first.with_updates(guidance=f(first)))
            content_to_append = _use_ins_content or ins.content
            if content_to_append is not None:
                _contents.append(_PreparedContent(content=content_to_append))
    else:
        content_to_append = _use_ins_content or ins.content
        if content_to_append is not None:
            _contents.append(_PreparedContent(content=content_to_append))

    kw = (param.imodel_kw or {}).copy()

    chat_msgs = []
    for entry in _contents:
        if _use_render_cache and entry.source is not None and entry.cache_variant is not None:
            source = entry.source
            if entry.cache_variant == "prepared_instruction":
                rendered = source._render_cached(
                    entry.cache_variant,
                    lambda source=source: (
                        source.content.with_updates(tool_schemas=[], response_format=None).rendered
                    ),
                )
            else:
                rendered = source._render_cached(
                    entry.cache_variant, lambda source=source: source.content.rendered
                )
        else:
            rendered = entry.materialize().rendered
        if not rendered:
            continue
        role = entry.role
        role_str = role.value if isinstance(role, MessageRole) else str(role)
        chat_msgs.append({"role": role_str, "content": rendered})

    kw["messages"] = chat_msgs
    return ins, kw


async def _apply_context_providers(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    param: ChatParam,
) -> Instruction | None:
    """Gather registered ContextProviders into the branch's per-turn injection
    slot; returns the pre-built Instruction, or None when no providers are
    registered (zero-overhead path). A branch with no system message has no
    render target — providers are skipped, not invoked; see `branch.last_context_report`.
    """
    if not branch._context_providers:
        return None

    from lionagi.protocols.context_providers import ProviderReport

    if not branch.msgs.system:
        branch._last_context_report = ProviderReport(skipped=list(branch._context_providers.names))
        return None

    ins = _build_instruction(branch, instruction, param)
    report = await branch._context_providers.gather(branch, ins)
    branch._last_context_report = report
    branch._context_injection_slot = report.blocks
    return ins


def _collect_action_dicts(act_res_msgs):
    d_ = []
    for k in to_list(act_res_msgs, flatten=True, unique=True):
        if hasattr(k.content, "function"):
            d_.append(
                {
                    "function": k.content.function,
                    "arguments": k.content.arguments,
                    "output": k.content.output,
                }
            )
        else:
            d_.append(k.content)
    return d_

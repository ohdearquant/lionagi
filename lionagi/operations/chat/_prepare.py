# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue

from lionagi._errors import EmptyOutgoingContentError
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
    from lionagi.protocols.context_providers import ProviderReport
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
    to_exclude = {
        "imodel",
        "imodel_kw",
        "include_token_usage_to_model",
        "progression",
        "turn_origin",
    }
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
    context_blocks: Sequence[str] | None = None,
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
            injected = "\n".join(context_blocks) if context_blocks else ""
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

    # The current turn's content is always the last entry appended to
    # `_contents` above (guidance-merge branch, action-context branch, and
    # plain-append branch all push it last). If the caller supplied real
    # instruction text/media but that entry rendered empty and got filtered
    # out of `chat_msgs` by `if not rendered: continue` above, the model call
    # would silently go out carrying only scaffolding (system/guidance) and
    # no user content — worse than a loud failure, since the run "completes"
    # with a useless reply. `rendered` here still holds the last loop
    # iteration's value even when that iteration hit `continue`.
    if _contents and _has_real_instruction_text(ins.content) and not rendered:
        _instruction_text = getattr(ins.content, "instruction", None)
        _plain_content = getattr(ins.content, "plain_content", None)
        _instruction_len = len(_instruction_text) if _instruction_text else 0
        _plain_content_len = len(_plain_content) if _plain_content else 0
        _has_images = bool(getattr(ins.content, "images", None))
        raise EmptyOutgoingContentError(
            "Refusing to call the model: the assembled outgoing message list "
            "is empty for the current turn despite a non-empty instruction "
            f"being supplied (instruction_len={_instruction_len}, "
            f"plain_content_len={_plain_content_len}, has_images={_has_images}). "
            "The instruction content was lost or filtered during message "
            "assembly — this is a bug, not a valid empty-prompt call."
        )

    kw["messages"] = chat_msgs
    return ins, kw


async def _apply_context_providers(
    branch: "Branch",
    instruction: JsonValue | Instruction,
    param: ChatParam,
    *,
    ins: Instruction | None = None,
) -> tuple[Instruction | None, "ProviderReport | None"]:
    """Gather registered ContextProviders for call-local prompt rendering.

    Returns the (pre-)built Instruction and its report, or ``(None, None)``
    when no providers are registered (zero-overhead path). A branch with no
    system message has no render target — providers are skipped, not invoked;
    see ``branch.last_context_report``.

    ``ins``, when given, is reused as-is instead of building a second
    Instruction — for callers that already constructed one before this
    async, potentially side-effecting gather runs.
    """
    if not branch._context_providers:
        return None, None

    from lionagi.protocols.context_providers import ProviderReport

    if not branch.msgs.system:
        report = ProviderReport(skipped=list(branch._context_providers.names))
        branch._last_context_report.set(report)
        branch._last_context_report_fallback = report
        return None, report

    if ins is None:
        ins = _build_instruction(branch, instruction, param)
    report = await branch._context_providers.gather(branch, ins)
    branch._last_context_report.set(report)
    branch._last_context_report_fallback = report
    return ins, report


def _has_real_instruction_text(content) -> bool:
    """True if an InstructionContent carries caller-supplied text/media.

    Deliberately ignores ``guidance``/``prompt_context`` (scaffolding this
    module injects itself) so the check reflects only what the caller asked
    for, not what the system/context providers added around it.
    """
    if content is None:
        return False
    return bool(
        getattr(content, "instruction", None)
        or getattr(content, "plain_content", None)
        or getattr(content, "images", None)
    )


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

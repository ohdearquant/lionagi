# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

# ReAct's caller context while one of its later calls runs (ReAct.py). That call's request shows
# the context once: `_prepare_run_kwargs`, in the same pass that renders the history, leaves it out
# of every rendered instruction after the first one carrying it, the current turn included.
_react_context: ContextVar[list[Any] | None] = ContextVar("_react_context", default=None)


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


def _render_context(context: Sequence[Any]) -> list[str]:
    """Each context item as an instruction renders it, an entry of its context list."""
    from lionagi.libs.schema.minimal_yaml import minimal_yaml

    return [minimal_yaml([value]) for value in context]


def _held_context(instruction: Instruction) -> list[str]:
    """The instruction's context items as it renders them, through its render cache.

    The cache renders afresh whenever the content changed in place or holds a value whose
    change it cannot observe, the rule the history's own rendering follows, so a stored
    context changed in place is read as it renders now.
    """
    held = instruction.content.prompt_context
    return instruction._render_cached("context_items", lambda: _render_context(held))


def _carries_context(instruction: Instruction, shown: Sequence[str]) -> bool:
    """Whether the instruction, rendered now, shows every one of these rendered context items.

    An instruction renders the context it was sent with as a list, one entry per item,
    unless plain content replaces its rendering. Each item has to render as the same text
    as a different item the instruction holds, so ``True`` does not stand in for ``1``
    nor ``0.0`` for ``-0.0``, and a context that repeats an item is carried only by an
    instruction that holds it as often. A value that cannot be rendered answers no.
    """
    if instruction.content.plain_content:
        return False
    try:
        if len(instruction.content.prompt_context) < len(shown):
            return False
        held = _held_context(instruction)
        for text in shown:
            if text not in held:
                return False
            held.remove(text)
    except Exception:
        return False
    return True


def _history_carriers(branch: "Branch", progression: Sequence, shown: Sequence[str]) -> list[int]:
    """The positions in ``progression`` of the instructions the history pass of
    ``_prepare_run_kwargs`` renders carrying the context, oldest first.

    Mirrors that pass: action responses fold into the next instruction, other message
    kinds are skipped, and an instruction that directly follows another instruction is
    dropped, so of consecutive instructions only the first is rendered.
    """
    carriers = []
    previous = None
    for i, msg in enumerate(branch.msgs.messages[j] for j in progression):
        if isinstance(msg, ActionResponse) or not isinstance(msg, AssistantResponse | Instruction):
            continue
        if (
            isinstance(msg, Instruction)
            and (previous is None or isinstance(previous, AssistantResponse))
            and _carries_context(msg, shown)
        ):
            carriers.append(i)
        previous = msg
    return carriers


def _without_context(instruction: Instruction, shown: Sequence[str]) -> list[Any]:
    """The instruction's context with the items rendered as ``shown`` left out, one held
    item per shown item; only for an instruction that carries them."""
    held = list(instruction.content.prompt_context)
    rendered = _held_context(instruction)
    for text in shown:
        k = rendered.index(text)
        del rendered[k], held[k]
    return held


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

    # Within one of ReAct's later calls the request shows the caller's context once, where
    # the history first renders it: later rendered instructions carrying it, and the current
    # turn, go out without those items. Only this request's copies change, never a stored
    # instruction, which another request may render. Decided in this pass, with no await
    # between it and the history read below, so nothing that runs earlier in the call (a
    # hook, a context provider) can change the history after the decision. A context that
    # cannot be rendered is left in: sent twice costs less than lost.
    shown: list[str] | None = None
    _leave_out: set[int] = set()
    context = _react_context.get()
    if context:
        try:
            shown = _render_context(context)
        except Exception:
            shown = None
    if shown is not None:
        carriers = _history_carriers(branch, progression, shown)
        if carriers:
            _leave_out = set(carriers[1:])
            if _carries_context(ins, shown):
                _use_ins_content = ins.content.with_updates(
                    prompt_context=_without_context(ins, shown)
                )

    for i, msg in enumerate(branch.msgs.messages[j] for j in progression):
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
            overlay = bool(_act_res) or i in _leave_out

            if i in _leave_out:
                updates["prompt_context"] = _without_context(msg, shown)

            if _act_res:
                d_ = _collect_action_dicts(_act_res)
                extended_ctx = list(updates.get("prompt_context", msg.content.prompt_context))
                extended_ctx.extend(z for z in d_ if z not in extended_ctx)
                updates["prompt_context"] = extended_ctx
                _act_res = []

            _contents.append(
                _PreparedContent(
                    source=msg if not overlay else None,
                    cache_variant="prepared_instruction" if not overlay else None,
                    content=(msg.content.with_updates(**updates) if overlay else None),
                )
            )

    if _act_res:
        d_ = _collect_action_dicts(_act_res)
        current = _use_ins_content if _use_ins_content is not None else ins.content
        extended_ctx = list(current.prompt_context)
        extended_ctx.extend(z for z in d_ if z not in extended_ctx)
        _use_ins_content = current.with_updates(prompt_context=extended_ctx)

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

    # The current turn's content is always the last entry appended to
    # `_contents` above; captured explicitly by index for the guard below,
    # rather than left to whatever the loop holds after its final iteration.
    _last_index = len(_contents) - 1
    _current_turn_rendered = None

    chat_msgs = []
    for i, entry in enumerate(_contents):
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
        if i == _last_index:
            _current_turn_rendered = rendered
        if not rendered:
            continue
        role = entry.role
        role_str = role.value if isinstance(role, MessageRole) else str(role)
        chat_msgs.append({"role": role_str, "content": rendered})

    # If the caller supplied real content but it rendered empty and got
    # filtered out above, the call would silently go out carrying only
    # scaffolding — worse than a loud failure.
    if _contents and _has_real_instruction_text(ins.content) and not _current_turn_rendered:
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

    Returns ``(None, None)`` when no providers are registered (zero-overhead
    path), or when the branch has no system message (no render target —
    providers are skipped, not invoked; see ``branch.last_context_report``).
    ``ins``, when given, is reused as-is rather than building a second
    Instruction, since the caller's copy may already reflect a side-effecting
    gather.
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

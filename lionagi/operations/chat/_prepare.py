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
    System,
)
from lionagi.protocols.messages._result_projection import _projection_session
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
    tool_refs: list[Any] | None = None
    # Context providers' blocks kept for this turn, sent after its content as one more block.
    context: tuple[str, ...] = ()
    # With the prompt cache placed: the round's tool results, rendered as their own text block
    # ahead of the content (``results_of`` names the results it renders).
    results: str | None = None
    results_of: tuple[Any, ...] = ()

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
            elif self.cache_variant == "instruction_tool_refs":
                self.content = self.source.content.with_updates(tool_schemas=self.tool_refs or [])
            elif self.cache_variant == "prepared_assistant":
                self.content = self.source.content.with_updates()
            else:
                self.content = self.source.content
            self.source = None
            self.cache_variant = None
            self.tool_refs = None
        return self.content


def _tool_name(schema: Any) -> str | None:
    """The function name a tool schema carries, or None."""
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    name = function.get("name") if isinstance(function, dict) else schema.get("name")
    return name if isinstance(name, str) and name else None


def _tool_refs(schemas: Sequence[Any], registered: frozenset[str]) -> list[Any]:
    """ADR-0127 D2: the requested tools as an instruction shows them when the system block
    carries the tool section. A tool the Branch has registered is named, since its definition
    is in that section; any other schema stays whole, so no definition is lost."""
    return [name if (name := _tool_name(s)) in registered else s for s in schemas]


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
    imodel: Any = None,
    _use_render_cache: bool = True,
    cache_view: dict | None = None,
) -> tuple[Instruction, dict]:
    """Compile the request for ``ins`` from the branch's active view.

    With the prompt cache placed, ``cache_view``, when given, is filled with where the compiled
    messages hold the end of the first round (``frontier``: message index, block index, and the
    round's message ids) and the newest compaction note (``note``), as (message, block) places in
    ``kw["messages"]``.
    """
    if ins is None:
        ins = _build_instruction(branch, instruction, param)

    # ADR-0127 D3: an endpoint that places the prompt cache takes the system text as its own
    # block, so it is not folded into the first instruction. Off, the fold is unchanged.
    places_cache = bool(getattr(getattr(imodel, "endpoint", None), "places_prompt_cache", False))
    projection = _projection_session(branch, imodel)

    # ADR-0127 Amendment 2: there the blocks a context provider gathers for a turn are kept on
    # that turn's instruction and sent after it on this and every later request, so no later
    # request rewrites an earlier one; a repeated gather for the same instruction keeps the first.
    # Off, each request shows only its own blocks, in the first instruction, and none are kept.
    if places_cache and context_blocks and _TURN_CONTEXT not in ins.metadata:
        ins.metadata[_TURN_CONTEXT] = list(context_blocks)

    # ADR-0127 D2: there the Branch's tool set renders once, as a section of the system block,
    # and every instruction, current or history, renders the same bytes: its requested tools by
    # name and its response format kept. Off, history drops both, as before.
    registered: frozenset[str] = frozenset()
    tool_section = None
    if places_cache:
        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        schemas = [] if _no_tool_section.get() else branch.acts.schema_list
        registered = frozenset(name for s in schemas if (name := _tool_name(s)))
        if schemas:
            tool_section = minimal_yaml({"Tools": schemas}).strip()

    def _as_sent(content: InstructionContent) -> InstructionContent:
        if not places_cache:
            return content
        return content.with_updates(tool_schemas=_tool_refs(content.tool_schemas, registered))

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

    frozen = _frozen_projection(branch, places_cache)

    def _results(responses: list) -> str:
        return _results_text(responses, projection, frozen)

    for i, msg in enumerate(branch.msgs.messages[j] for j in progression):
        if isinstance(msg, ActionResponse):
            _act_res.append(msg)

        elif isinstance(msg, AssistantResponse):
            if places_cache and _act_res:
                # ADR-0130 Amendment 4: results whose next instruction is gone keep their own
                # user message at their place, the bytes they had at the head of that message.
                _contents.append(
                    _PreparedContent(
                        content=InstructionContent(plain_content=_results(_act_res)),
                        results_of=tuple(_act_res),
                    )
                )
                _act_res = []
            _contents.append(
                _PreparedContent(
                    source=msg,
                    cache_variant="prepared_assistant",
                )
            )

        elif isinstance(msg, Instruction):
            refs = _tool_refs(msg.content.tool_schemas, registered) if places_cache else None
            updates = (
                {"tool_schemas": refs}
                if places_cache
                else {"tool_schemas": [], "response_format": None}
            )
            merges_results = bool(_act_res) and not places_cache
            overlay = merges_results or i in _leave_out

            if i in _leave_out:
                updates["prompt_context"] = _without_context(msg, shown)

            results, results_of = None, ()
            if merges_results:
                d_ = _collect_action_dicts(_act_res, projection)
                extended_ctx = list(updates.get("prompt_context", msg.content.prompt_context))
                extended_ctx.extend(z for z in d_ if z not in extended_ctx)
                updates["prompt_context"] = extended_ctx
                _act_res = []
            elif _act_res:
                results, results_of = _results(_act_res), tuple(_act_res)
                _act_res = []

            variant = "instruction_tool_refs" if places_cache else "prepared_instruction"
            _contents.append(
                _PreparedContent(
                    source=msg if not overlay else None,
                    cache_variant=variant if not overlay else None,
                    content=(msg.content.with_updates(**updates) if overlay else None),
                    tool_refs=refs if not overlay else None,
                    context=_turn_context(msg) if places_cache else (),
                    results=results,
                    results_of=results_of,
                )
            )

    current_results, current_results_of = None, ()
    if _act_res and places_cache:
        current_results, current_results_of = _results(_act_res), tuple(_act_res)
    elif _act_res:
        d_ = _collect_action_dicts(_act_res, projection)
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
                if places_cache:
                    # Joined into one message at assembly, one text block each, so a later
                    # response or note appends a block and leaves the earlier ones' bytes.
                    merged.append(entry)
                elif isinstance(merged[-1].base_content, AssistantResponseContent):
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
            if places_cache:
                return g
            injected = "\n".join(context_blocks) if context_blocks else ""
            return branch.msgs.system.rendered + injected + g

        current_context = _turn_context(ins) if places_cache else ()
        if len(_contents) == 0:
            _contents.append(
                _PreparedContent(
                    content=_as_sent(ins.content.with_updates(guidance=f(ins.content))),
                    context=current_context,
                    results=current_results,
                    results_of=current_results_of,
                )
            )
        elif len(_contents) >= 1:
            head = _contents[0]
            first = head.materialize()
            if not isinstance(first, InstructionContent):
                raise ValueError("First message in progression must be an Instruction or System")
            _contents[0] = _PreparedContent(
                content=first.with_updates(guidance=f(first)),
                context=head.context,
                results=head.results,
                results_of=head.results_of,
            )
            content_to_append = _use_ins_content or ins.content
            if content_to_append is not None:
                _contents.append(
                    _PreparedContent(
                        content=_as_sent(content_to_append),
                        context=current_context,
                        results=current_results,
                        results_of=current_results_of,
                    )
                )
    else:
        content_to_append = _use_ins_content or ins.content
        if content_to_append is not None:
            _contents.append(
                _PreparedContent(
                    content=_as_sent(content_to_append),
                    context=_turn_context(ins) if places_cache else (),
                    results=current_results,
                    results_of=current_results_of,
                )
            )

    kw = (param.imodel_kw or {}).copy()
    if places_cache:
        stable = []
        if branch.msgs.system and branch.msgs.system.rendered:
            stable.append(branch.msgs.system.rendered)
        if tool_section:
            stable.append(tool_section)
        if stable:
            kw["system_blocks"] = stable

    # The current turn's content is always the last entry appended to
    # `_contents` above; captured explicitly by index for the guard below,
    # rather than left to whatever the loop holds after its final iteration.
    _last_index = len(_contents) - 1
    _current_turn_rendered = None

    placed = cache_view is not None and places_cache
    round_ids = _round_ids(branch, progression, places_cache) if placed else None
    if cache_view is not None and round_ids:
        cache_view["round"] = round_ids
    chat_msgs = []
    # Without a system message, fold the caller's blocks into the first
    # outgoing instruction. A plain-content instruction ignores guidance, so
    # it also needs this fold when a system message is present.
    pending_blocks = "\n".join(context_blocks or ()) if not places_cache else ""
    needs_block_fold = bool(pending_blocks)
    for i, entry in enumerate(_contents):
        # The stored message this entry renders, when it renders one: a note or a round member.
        source = entry.source
        if _use_render_cache and entry.source is not None and entry.cache_variant is not None:
            source = entry.source
            if entry.cache_variant == "prepared_instruction":
                rendered = source._render_cached(
                    entry.cache_variant,
                    lambda source=source: (
                        source.content.with_updates(tool_schemas=[], response_format=None).rendered
                    ),
                )
            elif entry.cache_variant == "instruction_tool_refs":
                # Keyed by the names shown, since which tools are named depends on the
                # registry, which the content's own revision does not track.
                refs = entry.tool_refs or []
                named = "\x00".join(r for r in refs if isinstance(r, str))
                rendered = source._render_cached(
                    f"instruction_tool_refs\x00{named}",
                    lambda source=source, refs=refs: (
                        source.content.with_updates(tool_schemas=refs).rendered
                    ),
                )
            else:
                rendered = source._render_cached(
                    entry.cache_variant, lambda source=source: source.content.rendered
                )
        else:
            rendered = entry.materialize().rendered
        # Whitespace-only text carries nothing, and some providers refuse the whole request
        # over it (a model reply of only newlines, cut at max_tokens, is one source).
        if isinstance(rendered, str) and not rendered.strip():
            rendered = ""
        if i == _last_index:
            _current_turn_rendered = rendered
        if not rendered and entry.results is None:
            continue
        is_first_plain = (
            bool(pending_blocks)
            and bool(branch.msgs.system)
            and i == 0
            and isinstance(entry.base_content, InstructionContent)
            and bool(entry.base_content.plain_content)
        )
        if branch.msgs.system and i == 0 and not is_first_plain:
            # The system/guidance fold already included these blocks.
            needs_block_fold = False
        if (needs_block_fold or is_first_plain) and isinstance(
            entry.base_content, InstructionContent
        ):
            content = entry.materialize()
            if content.plain_content:
                content = content.with_updates(
                    plain_content=f"{pending_blocks}\n{content.plain_content}"
                )
            else:
                guidance = content.guidance or ""
                if not isinstance(guidance, str):
                    from lionagi.libs.schema.minimal_yaml import minimal_yaml

                    guidance = minimal_yaml(guidance).strip()
                content = content.with_updates(
                    guidance="\n".join(part for part in (pending_blocks, guidance) if part)
                )
            rendered = content.rendered
            needs_block_fold = False
        if rendered and entry.context:
            rendered = _with_context(rendered, entry.context)
        if entry.results is not None:
            # An instruction that renders nothing still sends the results it carries.
            rendered = [
                {"type": "text", "text": entry.results},
                *(_as_blocks(rendered) if rendered else []),
            ]
        elif entry.results_of:
            rendered = _as_blocks(rendered)
        role = entry.role
        role_str = role.value if isinstance(role, MessageRole) else str(role)
        if (
            places_cache
            and role_str == "assistant"
            and chat_msgs
            and chat_msgs[-1]["role"] == "assistant"
        ):
            previous = chat_msgs[-1]
            blocks = _as_blocks(previous["content"])
            slot = len(blocks)
            previous["content"] = [*blocks, *_as_blocks(rendered)]
        else:
            slot = 0
            chat_msgs.append({"role": role_str, "content": rendered})
        if cache_view is not None and places_cache:
            last = slot + len(_as_blocks(rendered)) - 1
            _note_view(cache_view, source, entry, len(chat_msgs) - 1, slot, last, round_ids)

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

    if needs_block_fold and pending_blocks.strip():
        chat_msgs.append({"role": "user", "content": pending_blocks})

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


def _collect_action_dicts(act_res_msgs, projection=None):
    d_ = []
    for k in to_list(act_res_msgs, flatten=True, unique=True):
        if hasattr(k.content, "function"):
            d_.append(
                {
                    "function": k.content.function,
                    "arguments": k.content.arguments,
                    "output": (
                        k.content.output if projection is None else projection.project(k).output
                    ),
                }
            )
        else:
            d_.append(k.content)
    return d_


# Set while an LNDL round runs whose tools are shown only as LNDL calls (lndl_middle.py): that
# round's request carries no tool section in a cached system block.
_no_tool_section: ContextVar[bool] = ContextVar("_no_tool_section", default=False)

# Instruction metadata key holding the context blocks gathered for that instruction's turn.
_TURN_CONTEXT = "context_blocks"


def _turn_context(message: Any) -> tuple[str, ...]:
    """The context blocks kept on an instruction for its turn, or none."""
    if not isinstance(message, Instruction):
        return ()
    blocks = message.metadata.get(_TURN_CONTEXT)
    if isinstance(blocks, list) and all(isinstance(block, str) for block in blocks):
        return tuple(blocks)
    return ()


def _with_context(rendered: str | list, blocks: tuple[str, ...]) -> list:
    """A turn's rendered content followed by its context blocks as one more text block."""
    parts = [{"type": "text", "text": rendered}] if isinstance(rendered, str) else list(rendered)
    return [*parts, {"type": "text", "text": "\n".join(blocks)}]


def _as_blocks(rendered: str | list) -> list:
    return [{"type": "text", "text": rendered}] if isinstance(rendered, str) else list(rendered)


def _results_text(responses: list, projection: Any, frozen: Any) -> str:
    """Tool results as the block that carries them when the prompt cache is placed.

    Results of the protected first round are projected with the bound recorded when the round
    was protected, so they keep their bytes when the current bound changes.
    """
    from lionagi.libs.schema.minimal_yaml import minimal_yaml

    members, frozen_projection = frozen
    dicts = []
    for response in to_list(responses, flatten=True, unique=True):
        used = frozen_projection if str(response.id) in members else projection
        dicts.extend(_collect_action_dicts([response], used))
    return minimal_yaml({"ToolResults": dicts}).strip()


def _frozen_projection(branch: "Branch", places_cache: bool) -> tuple[frozenset, Any]:
    """The protected round's member ids and the projection its results compile with."""
    from lionagi.service._prompt_cache import protection

    record = protection(branch.metadata) if places_cache else None
    if not record or not record.get("included"):
        return frozenset(), None
    from lionagi.protocols.messages._result_projection import _authority, _ProjectionSession

    bound = (record.get("projection") or {}).get("max_chars")
    current = _authority.get()
    authority = current[1] if current is not None and current[0] is branch else None
    session = (
        _ProjectionSession(branch, bound, authority=authority) if isinstance(bound, int) else None
    )
    return frozenset(record.get("members") or ()), session


def _round_ids(branch: "Branch", progression: Sequence, places_cache: bool) -> tuple | None:
    """The first round's member ids: the protection record's once decided, else the view's."""
    from lionagi.service._prompt_cache import protection

    record = protection(branch.metadata) if places_cache else None
    if record is not None:
        return tuple(record["members"]) or None
    return first_round(branch, progression)


def first_round(branch: "Branch", progression: Sequence) -> tuple[str, ...] | None:
    """The ids of the first round in the active view, in order: the first instruction, its
    answer and that answer's tool calls and results, up to the next instruction, answer or
    compaction note; None until that round has an answer."""
    ids: list[str] = []
    answered = False
    for uid in progression:
        msg = branch.msgs.messages[uid]
        if isinstance(msg, System):
            continue
        if is_note(msg):
            # A note stands for the rounds it folded, never for this one.
            break
        if isinstance(msg, Instruction):
            if ids:
                break
        elif not ids:
            return None
        if isinstance(msg, AssistantResponse):
            if answered:
                break
            answered = True
        ids.append(str(msg.id))
    return tuple(ids) if answered else None


def is_note(message: Any) -> bool:
    """A compaction note, recognized by the provenance the context tool writes on it."""
    return isinstance(message, AssistantResponse) and "context_first_index" in message.metadata


def _note_view(view: dict, source, entry, index: int, slot: int, last: int, round_ids) -> None:
    """Record where the newest note and the end of the first round sit in the messages."""
    if source is not None and is_note(source):
        view["note"] = (index, last)
    if not round_ids:
        return
    # The round's results open the message that follows its answer: its last block is theirs.
    if entry.results_of and all(str(r.id) in round_ids for r in entry.results_of):
        view["frontier"] = (index, slot)
    elif source is not None and str(source.id) in round_ids:
        view["frontier"] = (index, last)

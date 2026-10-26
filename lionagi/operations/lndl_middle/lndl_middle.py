# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LNDL seam Middle — advances a branch one LNDL round per inner chat call, looping up to a round budget (ADR-0024 §1-2). Opt-in via ``branch.operate(middle=lndl_middle)``."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import JsonValue, ValidationError

from lionagi.ln import unwrap_optional
from lionagi.lndl import (
    ActionCall,
    Continue,
    Lexer,
    LNDLError,
    Parser,
    Retry,
    RoundOutcome,
    Success,
    assemble,
    build_action_call,
    collect_actions,
    extract_lndl_blocks,
    get_lndl_system_prompt,
    normalize_lndl_text,
    replace_actions,
)

from .._defaults import get_default_action_call
from .._turn_origin import TurnOrigin
from ..chat._prepare import _no_tool_section
from ..types import ActionParam, ChatParam, ParseParam, RunParam

if TYPE_CHECKING:
    from lionagi.protocols.messages.instruction import Instruction
    from lionagi.session.branch import Branch

__all__ = ("DEFAULT_ROUND_BUDGET", "build_lndl_middle", "lndl_middle")

DEFAULT_ROUND_BUDGET = 3

# Shared by every round's action bridge: concurrent, error-suppressed so a
# failed tool call becomes a result the model reacts to, not an aborting exception.
_ACTION_PARAM = ActionParam(
    action_call_params=get_default_action_call(),
    tools=None,
    strategy="concurrent",
    suppress_errors=True,
    verbose_action=False,
)


def _render_type(annotation: Any) -> str:
    """Render a field's type the way LNDL_SYSTEM_PROMPT's ``Specs:`` examples do."""
    annotation = unwrap_optional(annotation)
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        elem = args[0] if args else Any
        if isinstance(elem, type) and hasattr(elem, "model_fields"):
            fields = ", ".join(elem.model_fields.keys())
            return f"list[{elem.__name__}: {fields}]"
        return f"list[{_render_type(elem)}]"
    if origin is dict:
        args = get_args(annotation)
        key_t = _render_type(args[0]) if args else "str"
        val_t = _render_type(args[1]) if len(args) > 1 else "Any"
        return f"dict[{key_t}, {val_t}]"
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        fields = ", ".join(annotation.model_fields.keys())
        return f"{annotation.__name__}: {fields}"
    return getattr(annotation, "__name__", str(annotation))


def _render_target_spec(target: Any) -> str | None:
    """Render the target model's fields as an LNDL ``Specs:`` line; None for a plain-dict/no-target caller."""
    model_fields = getattr(target, "model_fields", None)
    if not model_fields:
        return None
    parts = []
    for name, info in model_fields.items():
        type_str = _render_type(info.annotation)
        if not info.is_required():
            type_str += ", optional"
        parts.append(f"{name}({type_str})")
    return "Specs: " + ", ".join(parts)


async def _bridge_action_calls(branch: Branch, calls: list[ActionCall]) -> dict[str, Any]:
    """Translate ActionCall placeholders into ActionRequests and run them through the branch's normal act() path (ADR-0024 §1)."""
    if not calls:
        return {}

    from ..act.act import act

    requests = [
        branch.msgs.create_action_request(
            function=call.function,
            arguments=call.arguments,
            sender=branch.id,
            recipient=branch.user or "user",
        )
        for call in calls
    ]
    responses = await act(branch, requests, _ACTION_PARAM)
    return {call.name: resp.output for call, resp in zip(calls, responses, strict=True)}


async def _run_round_chat(
    branch: Branch,
    instruction: JsonValue | Instruction,
    chat_param: ChatParam,
) -> str:
    """Run one inner-chat turn, dispatching to communicate() (API) or run_and_collect() (CLI) — mirrors operate()'s own selection."""
    # Transport follows the effective model's CLI flag, same precedence as
    # chat()/operate(). Sentinel status, not truthiness, decides "supplied" —
    # a model whose __bool__ is False must still win over the branch default.
    effective_imodel = (
        branch.chat_model if chat_param._is_sentinel(chat_param.imodel) else chat_param.imodel
    )
    if isinstance(chat_param, RunParam) or getattr(effective_imodel, "is_cli", False):
        from ..run.run import run_and_collect

        return await run_and_collect(branch, instruction, chat_param, skip_validation=True)

    from ..communicate.communicate import communicate

    return await communicate(branch, instruction, chat_param, skip_validation=True)


def _round_instruction(
    original_instruction: JsonValue | Instruction,
    round_num: int,
    round_budget: int,
    prior_error: str | None,
) -> JsonValue | Instruction:
    """Round 1 sends the instruction verbatim (LNDL contract rides in
    ``guidance``); later rounds send a short 'Round N of M.' continuation notice,
    which on the last round says so and after a refused round carries its error."""
    if round_num == 1:
        return original_instruction
    notice = f"Round {round_num} of {round_budget}."
    if round_num == round_budget:
        notice += " This is the last round: write your answer in OUT{} now."
    if prior_error:
        notice += f"\n\nError from your previous round: {prior_error}\nRepair and try again."
    return notice


_CALLS_BESIDE_ANSWER = (
    "this round declares <lact> calls and also writes OUT{}; none of its calls ran and the "
    "answer was not taken. Declare calls in a round without OUT{}, read their results, then "
    "write OUT{} in a later round that declares no <lact>."
)

_NOTHING_RAN = (
    "nothing in this round ran: it has no <lact> call and no OUT{} answer inside a ```lndl "
    "block, and a call written any other way, such as an <invoke> tag, is not run. Open a "
    "block with a line ```lndl, close it with a line ```, and inside it declare calls as "
    "<lact> or write the answer as OUT{}."
)


def _too_many_calls(calls: int, max_actions: int | None) -> str | None:
    if max_actions is None or calls <= max_actions:
        return None
    return (
        f"this round declares {calls} <lact> calls and a round may declare at most "
        f"{max_actions}; none of them ran. Declare at most {max_actions} now and the rest in "
        "a later round."
    )


_PYTHON_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
    "null": "None",
}
_EXAMPLE_VALUES = {"str": '"..."', "int": "1", "float": "1.0", "bool": "True"}


def _signature(function: dict[str, Any]) -> tuple[str, str] | None:
    """A tool's call signature and an example call, from its schema in the OpenAI form."""
    name = function.get("name")
    if not isinstance(name, str):
        return None
    parameters = function.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    properties = parameters.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = parameters.get("required")
    required = set(required) if isinstance(required, list) else set()
    rendered, example = [], []
    for key, schema in properties.items():
        schema = schema if isinstance(schema, dict) else {}
        options = schema.get("anyOf") if isinstance(schema.get("anyOf"), list) else [schema]
        kinds = []
        for option in options:
            # A JSON Schema type may itself be a list, {"type": ["string", "null"]}.
            kind = option.get("type") if isinstance(option, dict) else None
            for type_name in kind if isinstance(kind, list) else [kind]:
                kinds.append(
                    _PYTHON_TYPES.get(type_name, "Any") if isinstance(type_name, str) else "Any"
                )
        kinds = kinds or ["Any"]
        text = f"{key}: {' | '.join(dict.fromkeys(kinds))}"
        if key in required:
            example.append(f"{key}={_EXAMPLE_VALUES.get(kinds[0], '...')}")
        elif "default" in schema:
            text += f" = {schema['default']!r}"
        else:
            text += " (optional)"
        rendered.append(text)
    return f"{name}({', '.join(rendered)})", f"{name}({', '.join(example)})"


def _tool_notes(function: dict[str, Any]) -> list[str]:
    """A tool's description and its parameters' descriptions, indented to sit under its call."""
    notes = []
    description = function.get("description")
    if isinstance(description, str) and description.strip():
        notes.append(f"    {' '.join(description.split())}")
    parameters = function.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    for key, schema in (properties if isinstance(properties, dict) else {}).items():
        text = schema.get("description") if isinstance(schema, dict) else None
        if isinstance(text, str) and text.strip():
            notes.append(f"    {key}: {' '.join(text.split())}")
    return notes


def _render_tool_calls(tool_schemas: list[Any] | None, described: bool = False) -> str | None:
    """How a multi-round run calls its tools: each tool as an LNDL call and a round that
    calls, so the model is not left to write calls in a provider's own markup. *described*
    puts each tool's description and its parameters' under its call."""
    signatures = []
    for schema in tool_schemas or ():
        function = schema.get("function", schema) if isinstance(schema, dict) else None
        if isinstance(function, dict) and (signature := _signature(function)):
            notes = _tool_notes(function) if described else []
            signatures.append((signature[0], signature[1], notes))
    if not signatures:
        return None
    lines = "\n".join("\n".join([signature, *notes]) for signature, _, notes in signatures)
    calls = "\n".join(
        f"<lact c{number}>{example}</lact>"
        for number, (_, example, _) in enumerate(signatures[:2], start=1)
    )
    return (
        "CALLING TOOLS\n\n"
        "A tool runs only when it is called as a <lact> line inside a ```lndl block. Tool-call "
        "markup such as <invoke>, <invoke_tool> or <function_calls> is not run. A call's result "
        "comes back in the next round, never in your own text, so do not write results "
        "yourself. The tools, as calls:\n\n"
        f"{lines}\n\n"
        f"A round that calls:\n\n```lndl\n{calls}\n```"
    )


# An <lvar>/<lact> tag or an OUT{} block, as the lexer opens them.
_ROUND_OPENER = re.compile(r"<(lvar|lact)\s|OUT\{")


def _scan_round(text: str) -> tuple[int, int, str | None]:
    """Count one round's ``<lact>`` calls and ``OUT{}`` blocks by the bounds the parser reads.

    An ``<lvar>`` or ``<lact>`` body runs to the first closing tag after it, as the parser
    extracts it, so a tag quoted inside a body is not counted. An ``OUT{}`` runs to the first
    ``}`` outside a quoted string, as the lexer reads it. Returns ``(calls, answers, problem)``;
    ``problem`` names a tag or ``OUT{}`` the round leaves open, a tag or another ``OUT{`` inside
    an ``OUT{}``, or an ``OUT{`` inside a body: the lexer reads that one as an opened block,
    quoted or not, and goes on reading the text after the body as the block's.
    """
    calls = answers = 0
    pos = 0
    while (opened := _ROUND_OPENER.search(text, pos)) is not None:
        kind = opened.group(1)
        if kind is None:
            answers += 1
            pos, problem = _out_block_end(text, opened.end())
            if problem is not None:
                return calls, answers, problem
            continue
        close = text.find(f"</{kind}>", opened.end())
        if close < 0:
            return calls, answers, f"a <{kind}> is not closed"
        if "OUT{" in text[opened.end() : close]:
            return calls, answers, f"a <{kind}> holds OUT{{, which is read as the answer's start"
        calls += kind == "lact"
        pos = close + len(kind) + 3
    return calls, answers, None


def _out_block_end(text: str, pos: int) -> tuple[int, str | None]:
    """The position after the ``}`` closing the ``OUT{}`` whose body starts at *pos*, or the
    reason the block has none: input ends first, or a tag or another ``OUT{`` opens inside it."""
    quote: str | None = None
    while pos < len(text):
        char = text[pos]
        if quote is not None:
            if char == "\\":
                pos += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "}":
            return pos + 1, None
        elif _ROUND_OPENER.match(text, pos):
            return pos, "OUT{} is not closed before the next <lvar>, <lact> or OUT{"
        pos += 1
    return pos, "OUT{} is not closed"


def _classify_round(
    text: str,
    target: Any,
    action_results: dict[str, Any],
    max_actions: int | None = None,
    lacts_with_out: bool = True,
) -> tuple[RoundOutcome, list[ActionCall], dict[str, Any] | None]:
    """Parse and assemble one round's raw text into a RoundOutcome; returns (outcome, pending_action_calls, assembled_dict).

    A round declaring more than ``max_actions`` ``<lact>`` calls is a repair round: none of its calls runs.
    With ``lacts_with_out=False``, so is a round that declares ``<lact>`` calls beside its ``OUT{}``, one
    that leaves a tag or its ``OUT{}`` open, one whose calls the parser reads differently from its
    tags (``_scan_round``), and one with neither a ``<lact>`` nor an ``OUT{}`` in an ``lndl`` block,
    including one with no block at all: every round there must call or answer, so a round that does
    neither is told so instead of continuing as if its calls had run.
    """
    if not extract_lndl_blocks(text):
        if not lacts_with_out:
            return Retry(error=_NOTHING_RAN), [], None
        return Continue(), [], None

    try:
        # normalize_lndl_text extracts the fenced blocks itself; extracting
        # first would run that pass again on a block's own text and could pick
        # up a fenced example quoted inside it instead of the round's content.
        normalized = normalize_lndl_text(text)
        scanned: tuple[int, int] | None = None
        if not lacts_with_out:
            # When calls and the answer must stay apart, every call the round
            # declares counts, wherever it sits, and the round's tags are counted
            # before the parser reads it: the lexer takes a quoted OUT{ inside a
            # body for an opened block, and an unclosed OUT{} runs to the end of
            # the input, so either could hide a call from the parser.
            calls, answers, problem = _scan_round(normalized)
            if problem is not None:
                return (
                    Retry(
                        error=(
                            f"this round could not be read: {problem}. None of its calls "
                            "ran and the answer was not taken. Close every tag and OUT{}, "
                            "and declare calls and the answer in separate rounds."
                        )
                    ),
                    [],
                    None,
                )
            refusal = _too_many_calls(calls, max_actions)
            if refusal is None and calls and answers:
                refusal = _CALLS_BESIDE_ANSWER
            if refusal is None and answers > 1:
                refusal = (
                    f"this round writes {answers} OUT{{}} blocks and a round may write one; "
                    "the answer was not taken. Write the whole answer in one OUT{}."
                )
            if refusal is not None:
                return Retry(error=refusal), [], None
            scanned = (calls, answers)

        tokens = Lexer(normalized).tokenize()
        parser = Parser(tokens, source_text=normalized)
        # Without that, the parser stops at OUT{}, as it always has.
        program = parser.parse(read_past_out=not lacts_with_out)

        if scanned is None:
            refusal = _too_many_calls(len(program.lacts), max_actions)
            if refusal is not None:
                return Retry(error=refusal), [], None
        elif len(program.lacts) != scanned[0] or (program.out_block is None) == bool(scanned[1]):
            return (
                Retry(
                    error=(
                        "this round's <lact> and OUT{} tags could not be read unambiguously, "
                        "so none of its calls ran and the answer was not taken. Keep tags "
                        "and OUT{} out of quoted text."
                    )
                ),
                [],
                None,
            )

        if program.out_block is None:
            if scanned is not None and not program.lacts:
                # The scan and the parser agree the block neither calls nor answers.
                return Retry(error=_NOTHING_RAN), [], None
            pending = [build_action_call(la.alias, la) for la in program.lacts]
            return Continue(), pending, None

        assembled = assemble(program, target, action_results=action_results)
    except LNDLError as e:
        return Retry(error=str(e)), [], None

    pending = collect_actions(assembled)
    return Success(output=assembled), pending, assembled


def build_lndl_middle(
    round_budget: int = DEFAULT_ROUND_BUDGET,
    max_actions: int | None = None,
    lacts_with_out: bool = True,
    tools_as_calls: bool = False,
):
    """Build an LNDL seam Middle (ADR-0024 §1) with a custom round budget; ``lndl_middle`` is the ready-to-use default.

    ``max_actions`` bounds the ``<lact>`` calls one round may declare; None leaves them unbounded.
    ``lacts_with_out=False`` keeps calls and the answer in separate rounds: a round that declares
    ``<lact>`` calls beside its ``OUT{}`` is a repair round and none of its calls runs, and so is
    a round with neither a ``<lact>`` nor an ``OUT{}`` in an ``lndl`` block.
    ``tools_as_calls=True`` shows the tools only as LNDL calls in the first round's guidance, each
    call with its descriptions, and leaves their schemas out of every round's instruction. It
    needs ``lacts_with_out=False``, since the guidance says a call's result comes back in the
    next round.
    """
    if max_actions is not None and (
        isinstance(max_actions, bool) or not isinstance(max_actions, int) or max_actions < 1
    ):
        raise ValueError(f"max_actions must be a positive integer or None, got {max_actions!r}")
    if tools_as_calls and lacts_with_out:
        raise ValueError("tools_as_calls=True needs lacts_with_out=False")

    async def _lndl_middle(
        branch: Branch,
        instruction: JsonValue | Instruction,
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ) -> Any:
        if clear_messages:
            branch.msgs.clear_messages()

        # The only model type operate()/communicate() ever hands us; what
        # assemble()+model_validate() below must target.
        target = chat_param.response_format
        base_guidance = chat_param.guidance or ""
        guidance_parts = [get_lndl_system_prompt()]
        # LNDL replies in free text, so the response format is dropped. The tools stay: a
        # <lact> names a tool, and an endpoint that does not place the prompt cache shows the
        # model its tools only in the current instruction. With none passed, every tool the
        # branch registers is listed, since a <lact> may call any of them. With tools_as_calls
        # they are shown as LNDL calls in the guidance instead, and no round sends a schema.
        tool_schemas = chat_param.tool_schemas or branch.acts.schema_list
        tool_calls = _render_tool_calls(tool_schemas, described=True) if tools_as_calls else None
        if tool_calls:
            guidance_parts.append(tool_calls)
        target_spec = _render_target_spec(target)
        if target_spec:
            guidance_parts.append(target_spec)
        if base_guidance:
            guidance_parts.append(base_guidance)
        lndl_guidance = "\n\n".join(guidance_parts)
        stripped_chat_param = chat_param.with_updates(
            tool_schemas=[] if tool_calls else tool_schemas, response_format=None
        )

        action_results: dict[str, Any] = {}
        # The most recent refusal, for the exhaustion error, and the one the next round is
        # asked to repair; a round that only declares calls clears the second, since the
        # model has already moved past it.
        last_error: str | None = None
        repair_error: str | None = None

        for round_num in range(1, round_budget + 1):
            if round_num == 1:
                round_chat_param = stripped_chat_param.with_updates(guidance=lndl_guidance)
            else:
                # A continuation/repair round is not a fresh user submission —
                # the outer call's boundary already fired at most once for
                # round 1; later rounds must never re-fire USER_PROMPT_SUBMIT.
                round_chat_param = stripped_chat_param.with_updates(
                    turn_origin=TurnOrigin.no_origin()
                )
            round_instruction = _round_instruction(
                instruction, round_num, round_budget, repair_error
            )
            repair_error = None

            # Tools shown as calls are shown nowhere else: the round's request carries no
            # tool section in a cached system block either.
            token = _no_tool_section.set(True) if tool_calls else None
            try:
                text = await _run_round_chat(branch, round_instruction, round_chat_param)
            finally:
                if token is not None:
                    _no_tool_section.reset(token)
            outcome, pending, assembled = _classify_round(
                text,
                target,
                action_results,
                max_actions=max_actions,
                lacts_with_out=lacts_with_out,
            )

            if isinstance(outcome, Retry):
                last_error = repair_error = outcome.error
                continue

            if isinstance(outcome, Continue):
                if pending:
                    action_results.update(await _bridge_action_calls(branch, pending))
                continue

            assert isinstance(outcome, Success)
            # Success candidate: resolve any pending action results, then
            # validate against the caller's target model (if any).
            if pending:
                action_results.update(await _bridge_action_calls(branch, pending))
            assembled = replace_actions(assembled, action_results)

            if skip_validation or target is None or not hasattr(target, "model_validate"):
                return assembled

            try:
                return target.model_validate(assembled)
            except ValidationError as e:
                last_error = repair_error = str(e)
                continue

        # Round budget exhausted without a valid OUT{}: raise rather than
        # return a bare str/None, which a validated-model caller must never see.
        detail = f": {last_error}" if last_error else " (no OUT{} block was produced)"
        raise LNDLError(
            f"LNDL round budget ({round_budget}) exhausted without a valid OUT{{}}{detail}"
        )

    return _lndl_middle


lndl_middle = build_lndl_middle()

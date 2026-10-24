# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Host-side context fold for an API-model agent with an input cap.

Before a request is compiled, the fold estimates its input; at ``fold_at`` of the model's
``max_input_tokens`` it curates the branch's active view with the context tool's own
``compact``, the mutation the model makes when it compacts, so durable history is never
deleted. The first stage replaces older tool calls and results with a host-written list of the
calls (their function, arguments and output size), so the model still knows which files it
read and which searches it ran. Only if that is not enough does a second stage replace older
reasoning too, with a summary written by one history-free model call. The task instruction and
the newest messages stay verbatim. Every fold is recorded in ``records`` for the run manifest.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

__all__ = ("ContextFold", "estimate_next_request_tokens", "install_context_fold")

logger = logging.getLogger(__name__)

_FOLD_AT = 0.8
_FOLD_TARGET = 0.6
# Newest active messages a fold never touches: the latest round's instruction, answer and
# tool records, then the last two messages when the first pass was not enough.
_KEEP_TAILS = (6, 2)
_MAX_NOTE_CHARS = 8_000
_MAX_RECORDED_CALLS = 50
_MAX_ARG_CHARS = 160
_MAX_PATH_CHARS = 1_000
_MAX_PATH_LIST_CHARS = 4_000
_MAX_SUMMARY_CHARS = 5_000
# Argument names that carry a path: a key ending in one of these (case-insensitive) keeps its
# value whole up to _MAX_PATH_CHARS, each element of a list of them too.
_PATH_KEY_SUFFIXES = (
    "path",
    "paths",
    "file",
    "files",
    "dir",
    "dirs",
    "directory",
    "cwd",
    "root",
    "target",
    "pattern",
)
_NOTE_HEAD = (
    "The host folded {n} earlier messages to keep the next request under the input cap of "
    "{cap:,} tokens. Nothing was deleted: the context tool with action='get_messages' and "
    "scope='all' lists the full record, and action='restore' brings messages back."
)


def estimate_next_request_tokens(branch: Branch) -> int:
    """Estimated input tokens of the branch's next chat request, counted as the input cap counts.

    Compiles the active view with the chat compiler itself, the newest instruction standing in
    for the turn about to be built (an agent loop's turns share one shape: the same tools and
    response format, the newest tool results merged in), builds the provider payload from it
    and counts that with the cap's own estimate. 0 before the first instruction is recorded.
    The instruction being sent is not recorded yet when API_PRE_CALL fires, so the stand-in is
    counted twice and the estimate errs by the difference between the two instructions, and by
    any context a context provider adds to that request alone: high after a long task, low
    before a longer instruction, which the refusal path then folds.
    """
    from lionagi.operations.chat._prepare import _prepare_run_kwargs
    from lionagi.operations.types import ChatParam
    from lionagi.protocols.messages import Instruction
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens

    pile = branch.msgs.messages
    newest = next(
        (
            pile[uid]
            for uid in reversed(list(branch.progression))
            if uid in pile and isinstance(pile[uid], Instruction)
        ),
        None,
    )
    if newest is None:
        return 0
    model = branch.chat_model
    _ins, kw = _prepare_run_kwargs(
        branch, None, ChatParam.from_branch(branch), ins=newest, imodel=model
    )
    payload, _headers = model.endpoint.create_payload(request=kw)
    return estimate_payload_input_tokens(payload)


def _clip(value: Any, limit: int) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else f"{text[:limit]}... ({len(text):,} chars)"


def _short(arguments: Any) -> str:
    """A tool call's arguments for the fold note: each long value clipped on its own, so a long
    ``content`` never pushes a path out; path arguments are kept whole up to _MAX_PATH_CHARS."""
    if not isinstance(arguments, dict):
        clipped = _clip(arguments, _MAX_ARG_CHARS)
        return clipped if isinstance(clipped, str) else json.dumps(clipped, default=str)
    shown = {
        key: _clip_path(value) if _is_path_key(key) else _clip(value, _MAX_ARG_CHARS)
        for key, value in arguments.items()
    }
    return json.dumps(shown, ensure_ascii=False, default=str)


def _is_path_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower().endswith(_PATH_KEY_SUFFIXES)


def _clip_path(value: Any) -> Any:
    if not isinstance(value, list):
        return _clip(value, _MAX_PATH_CHARS)
    kept: list[Any] = []
    used = 0
    for item in value:
        item = _clip(item, _MAX_PATH_CHARS)
        used += _size(item)
        if used > _MAX_PATH_LIST_CHARS:
            kept.append(f"... {len(value) - len(kept)} more")
            break
        kept.append(item)
    return kept


def _size(value: Any) -> int:
    if value is None:
        return 0
    return len(
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    )


class ContextFold:
    """Fold one branch's active view before its requests reach ``cap`` input tokens."""

    def __init__(
        self,
        branch: Branch,
        *,
        cap: int,
        fold_at: float = _FOLD_AT,
        target: float = _FOLD_TARGET,
        log: Callable[[str], None] | None = None,
        estimate: Callable[[Branch], int] = estimate_next_request_tokens,
    ) -> None:
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
            raise ValueError("cap must be a positive integer")
        if not 0 < target < fold_at <= 1:
            raise ValueError("need 0 < target < fold_at <= 1")
        self.branch = branch
        self.cap = cap
        self.fold_at = fold_at
        self.target = target
        self.records: list[dict[str, Any]] = []
        self._log = log or logger.warning
        self._estimate = estimate
        # Raised to refused/estimated when the cap refuses a request this fold let through, so
        # every later check and fold measures against the count the cap applies.
        self._scale = 1.0
        self._busy = False
        # The calls each of this fold's notes lists, by note id: an `all` fold replaces earlier
        # notes too, and their lists carry into its own.
        self._note_calls: dict[Any, list[dict[str, Any]]] = {}
        self._compact: Callable[..., Any] | None = None

    def _provider_scale(self) -> float:
        # The factor the chat model's cap applies to the same count (iModel.input_token_scale).
        scale = getattr(self.branch.chat_model, "input_token_scale", 1.0)
        return scale if isinstance(scale, float) and scale > 1.0 else 1.0

    def estimate(self) -> int:
        return int(self._estimate(self.branch) * self._provider_scale() * self._scale)

    async def on_api_pre_call(self, **_kw: Any) -> None:
        """API_PRE_CALL handler: fold when the next request would pass ``fold_at`` of the cap."""
        if self._busy:
            return
        try:
            if self.estimate() >= self.fold_at * self.cap:
                await self.fold(reason="threshold")
        except Exception as exc:  # noqa: BLE001 — the cap still refuses an oversized request
            self._log(f"context fold failed: {exc!r}")

    async def fold(self, *, reason: str, observed: int | None = None) -> bool:
        """Fold until the estimate is at or under ``target`` of the cap; True if any fold landed.

        Stages, each tried with the wider tail first: tool calls and results (the host lists
        them), then all messages after the task instruction (a model summary plus that list).
        ``observed`` is the estimate a refusal reported for the request it refused; it
        calibrates this estimate first, and the fold then lands at least once whatever the
        estimate reads.
        """
        if self._busy:
            return False
        self._busy = True
        try:
            if observed:
                raw = self._estimate(self.branch) * self._provider_scale()
                if raw > 0:
                    self._scale = max(self._scale, observed / raw)
            landed = False
            for mode in ("tool_io", "all"):
                for keep_tail in _KEEP_TAILS:
                    must_land = observed is not None and not landed
                    if not must_land and self.estimate() <= self.target * self.cap:
                        return landed
                    landed = await self._fold_once(mode, keep_tail, reason) or landed
            return landed
        finally:
            self._busy = False

    def _compact_view(self) -> Callable[..., Any]:
        """The context tool's own function bound to this branch: the fold compacts with it."""
        if self._compact is None:
            from lionagi.tools.context.context import ContextTool

            self._compact = ContextTool().bind(self.branch).func_callable
        return self._compact

    async def _fold_once(self, mode: str, keep_tail: int, reason: str) -> bool:
        from lionagi.protocols.messages import ActionRequest, ActionResponse, Instruction

        active = list(self.branch.progression)
        end = len(active) - keep_tail
        if end <= 0:
            return False
        pile = self.branch.msgs.messages
        span = [pile[uid] for uid in active[:end] if uid in pile]
        tool_msgs = [m for m in span if isinstance(m, ActionRequest | ActionResponse)]
        if mode == "tool_io" and not tool_msgs:
            return False
        # Compaction keeps the first active instruction (the task) and the message at the
        # head of the view whatever the range, so a span holding nothing else has nothing to
        # fold and is not worth a summary request.
        head = active[0] if active else None
        anchor = next((m for m in span if isinstance(m, Instruction)), None)
        folded = [m for m in span if m is not anchor and m.id != head]
        if mode == "all" and not folded:
            return False
        calls = self._tool_calls(tool_msgs)
        # An `all` fold replaces this fold's earlier notes as well; the calls they list carry
        # forward, so the agent keeps every file it read and search it ran whatever the summary
        # keeps.
        listed = self._carried_calls(span) if mode == "all" else calls
        before = self.estimate()
        summary_source = "host"
        model_summary = None
        if mode == "all":
            model_summary = await self._model_summary(span)
            if model_summary:
                summary_source = "model"
        n_folded = len(tool_msgs) if mode == "tool_io" else len(folded)
        note = self._note(n_folded, listed, model_summary)
        result = await self._compact_view()(
            action="compact", start=0, end=end, mode=mode, summary=note
        )
        if not result.get("success"):
            self._log(f"context fold ({mode}) not applied: {result.get('error')}")
            return False
        before_ids = set(active)
        for uid in self.branch.progression:
            if uid not in before_ids:
                self._note_calls[uid] = listed
        after = self.estimate()
        record = {
            "at": time.time(),
            "reason": reason,
            "stage": mode,
            "kept_tail": keep_tail,
            "messages_folded": result.get("compacted", 0),
            "tool_calls": calls[:_MAX_RECORDED_CALLS],
            "tool_calls_total": len(calls),
            "summary_source": summary_source,
            "estimate_before": before,
            "estimate_after": after,
            "cap": self.cap,
            "input_scale": round(self._provider_scale() * self._scale, 4),
        }
        self.records.append(record)
        self._log(
            f"context fold ({reason}, {mode}): {record['messages_folded']} messages folded, "
            f"estimated input {before:,} -> {after:,} tokens (cap {self.cap:,})"
        )
        return True

    @staticmethod
    def _tool_calls(tool_msgs: list[Any]) -> list[dict[str, Any]]:
        from lionagi.protocols.messages import ActionResponse

        calls: list[dict[str, Any]] = []
        for msg in tool_msgs:
            if not isinstance(msg, ActionResponse):
                continue
            c = msg.content
            calls.append(
                {
                    "function": c.function,
                    "arguments": _short(c.arguments),
                    "output_chars": _size(c.output),
                }
            )
        return calls

    def _carried_calls(self, span: list[Any]) -> list[dict[str, Any]]:
        """The calls behind a span in order: its own tool results and its earlier notes' lists."""
        from lionagi.protocols.messages import ActionResponse

        calls: list[dict[str, Any]] = []
        for msg in span:
            if msg.id in self._note_calls:
                calls.extend(self._note_calls[msg.id])
            elif isinstance(msg, ActionResponse):
                calls.extend(self._tool_calls([msg]))
        return calls

    def _note(self, n: int, calls: list[dict[str, Any]], model_summary: str | None) -> str:
        head = _NOTE_HEAD.format(n=n, cap=self.cap)
        body = f"\n\nSummary of the folded reasoning:\n{model_summary}" if model_summary else ""
        text = head + body
        if calls:
            text += f"\n\nTool calls folded ({len(calls)}; function, arguments, output size):"
            for i, call in enumerate(calls):
                line = (
                    f"\n- {call['function']} {call['arguments']} ({call['output_chars']:,} chars)"
                )
                tail = (
                    f"\n- ... {len(calls) - i} more calls, listed by get_messages with scope='all'"
                )
                if len(text) + len(line) + len(tail) > _MAX_NOTE_CHARS:
                    text += tail
                    break
                text += line
        return text[:_MAX_NOTE_CHARS]

    async def _model_summary(self, span: list[Any]) -> str | None:
        """A summary of the span from one model call that carries the span alone, no history.

        The source text is bounded to about half the cap; any failure returns None and the
        fold falls back to the host-written list.
        """
        from lionagi.protocols.messages import AssistantResponse
        from lionagi.service.imodel import _branch_routing_context

        budget_chars = self.cap * 2
        texts: list[str] = []
        used = 0
        for msg in span:
            rendered = msg.rendered
            text = rendered if isinstance(rendered, str) else json.dumps(rendered, default=str)
            if not text:
                continue
            room = budget_chars - used
            if room <= 0:
                break
            texts.append(text[:room])
            used += min(len(text), room)
        if not texts:
            return None
        prompt = (
            "Write a concise, factual summary of the agent work below, which is being removed "
            "from the agent's context. Keep every file path, identifier, command and number it "
            "relies on, the findings so far with their evidence, what was verified and what is "
            f"still open. Do not invent anything. At most {_MAX_SUMMARY_CHARS} characters.\n\n"
            + "\n---\n".join(texts)
        )
        model = self.branch.chat_model
        prior_refusal = getattr(model, "input_cap_refusal", None)
        try:
            with _branch_routing_context(self.branch.id, model):
                api_call = await model.invoke(messages=[{"role": "user", "content": prompt}])
            text = AssistantResponse.from_response(api_call.response).response
        except Exception as exc:  # noqa: BLE001 — the host-written list stands in
            self._log(f"context fold summary call failed: {exc!r}")
            if prior_refusal is None:
                # A refusal of this side request is not the run's: left set, it would refuse
                # every later request of the agent.
                model.input_cap_refusal = None
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        return text.strip()[:_MAX_SUMMARY_CHARS]


def install_context_fold(
    branch: Branch, *, cap: int, log: Callable[[str], None] | None = None
) -> ContextFold:
    """Attach a ContextFold to the branch's API_PRE_CALL hook point and return it.

    A branch with no HookBus yet gets one of its own; a Session joined later takes the handler
    over through ``attach_hook_bus``, as it does for every queued branch handler. A branch keeps
    one fold: installing again returns it, and a different cap is refused.
    """
    from lionagi.hooks.bus import HookBus, HookPoint

    # One fold per branch: a second install returns the first instead of adding a second handler.
    for point, handler in branch._pending_hook_bus_entries:
        existing = getattr(handler, "__self__", None)
        if point == HookPoint.API_PRE_CALL and isinstance(existing, ContextFold):
            if existing.cap != cap:
                raise ValueError(
                    f"branch already folds at a cap of {existing.cap:,} tokens, not {cap:,}"
                )
            return existing
    fold = ContextFold(branch, cap=cap, log=log)
    branch._pending_hook_bus_entries.append((HookPoint.API_PRE_CALL, fold.on_api_pre_call))
    branch.attach_hook_bus(branch._hooks if branch._hooks is not None else HookBus())
    return fold

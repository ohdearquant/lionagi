# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Host-side context fold for an API-model agent with an input cap.

Before a chat request is compiled, the fold estimates its input; at ``fold_at`` of the model's
``max_input_tokens`` it curates the branch's active view with the context tool's own
``compact``, the mutation the model makes when it compacts, so durable history is never
deleted. The first stage replaces older tool calls and results with a host-written list of the
calls (their function, arguments and output size), so the model still knows which files it
read and which searches it ran. Large result bodies, including unread results and the kept tail,
can then be clipped into recoverable copies. If needed, a final stage summarizes delivered
reasoning with one history-free model call. The task instruction stays verbatim. Every pass
is recorded in ``records`` for the run manifest and lands only if it lowers the estimate.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from lionagi.ln import copy

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
    text = _text(value)
    return value if len(text) <= limit else f"{text[:limit]}... ({len(text):,} chars)"


def _short(arguments: Any) -> str:
    """A tool call's arguments for the fold note: each long value clipped on its own, so a long
    ``content`` never pushes a path out; path arguments are kept whole up to _MAX_PATH_CHARS."""
    if not isinstance(arguments, dict):
        clipped = _clip(arguments, _MAX_ARG_CHARS)
        return clipped if isinstance(clipped, str) else _text(clipped)
    shown = {
        key: _clip_path(value) if _is_path_key(key) else _clip(value, _MAX_ARG_CHARS)
        for key, value in arguments.items()
    }
    return _text(shown)


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
    return len(_text(value))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        from yaml import YAMLError

        from lionagi.libs.schema.minimal_yaml import minimal_yaml

        try:
            return minimal_yaml(value)
        except YAMLError:
            return json.dumps(value, ensure_ascii=False, default=str)


def _validate_fold_pair(fold_at: float, target: float) -> None:
    if (
        not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 < value <= 1
            and math.isfinite(value)
            for value in (fold_at, target)
        )
        or not target < fold_at
    ):
        raise ValueError("need finite numbers with 0 < target < fold_at <= 1")


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
        _validate_fold_pair(fold_at, target)
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
        for uid, calls in branch.metadata.get("context_fold_calls", {}).items():
            if uid in branch.msgs.messages:
                self._note_calls[branch.msgs.messages[uid].id] = copy(calls, deep=True)
        self._compact: Callable[..., Any] | None = None
        self._summary_usage: dict[str, Any] | None = None
        branch._context_restore_check = self._restore_fits
        self._request_views: ContextVar[tuple[tuple[str, ...] | None, ...]] = ContextVar(
            f"context_fold_requests_{id(self)}", default=()
        )
        if "context_delivered" not in branch.metadata:
            from lionagi.protocols.messages import AssistantResponse

            full = list(branch.msgs.progression)
            last_answer = next(
                (
                    index
                    for index in range(len(full) - 1, -1, -1)
                    if isinstance(branch.msgs.messages[full[index]], AssistantResponse)
                    and not str(branch.msgs.messages[full[index]].response).startswith(
                        "[CONTEXT COMPACTION]"
                    )
                ),
                0,
            )
            branch.metadata["context_delivered"] = [str(uid) for uid in full[:last_answer]]

    def _provider_scale(self) -> float:
        # The factor the chat model's cap applies to the same count (iModel.input_token_scale).
        scale = getattr(self.branch.chat_model, "input_token_scale", 1.0)
        return scale if isinstance(scale, float) and scale > 1.0 else 1.0

    def estimate(self) -> int:
        return int(self._estimate(self.branch) * self._provider_scale() * self._scale)

    def _estimate_view(self, messages: list[Any]) -> int:
        """Count a candidate without adding its messages to the durable record or view."""
        from lionagi.protocols.generic.progression import Progression
        from lionagi.protocols.messages.manager import MessageManager

        candidate = self.branch.model_copy()
        candidate.metadata = dict(self.branch.metadata)
        candidate._message_manager = MessageManager(messages=messages)
        candidate._message_manager.system = self.branch.msgs.system
        candidate.metadata["current_progression"] = Progression(
            order=[message.id for message in messages]
        )
        from lionagi.protocols.messages._result_projection import (
            _projection_authority,
            _projection_session,
        )

        projection = _projection_session(self.branch, self.branch.chat_model)
        if projection is not None:
            with _projection_authority(candidate, projection):
                return int(self._estimate(candidate) * self._provider_scale() * self._scale)
        return int(self._estimate(candidate) * self._provider_scale() * self._scale)

    async def on_api_pre_call(self, **_kw: Any) -> None:
        """API_PRE_CALL handler: fold when the next request would pass ``fold_at`` of the cap."""
        from lionagi.tools.context.context import _automatic_summary

        if _automatic_summary.get():
            self._request_views.set((*self._request_views.get(), None))
            return
        try:
            if not self._busy and self.estimate() >= self.fold_at * self.cap:
                await self.fold(reason="threshold")
        except Exception as exc:  # noqa: BLE001 — the cap still refuses an oversized request
            from lionagi.state.compat import refusal_in

            refusal = refusal_in(exc)
            if refusal is exc:
                raise
            if refusal is not None:
                raise refusal from exc
            self._log(f"context fold failed: {exc!r}")
        self._request_views.set(
            (*self._request_views.get(), tuple(str(uid) for uid in self.branch.progression))
        )

    async def on_api_post_call(self, *, status: str, **_kw: Any) -> None:
        pending = self._request_views.get()
        if not pending:
            return
        self._request_views.set(pending[:-1])
        sent = pending[-1]
        if sent is None or status != "completed":
            return
        delivered = self.branch.metadata["context_delivered"]
        known = set(delivered)
        delivered.extend(uid for uid in sent if uid not in known)
        self.branch.metadata.pop("context_read_results", None)
        self.branch.metadata.pop("context_restored_results", None)

    def _restore_fits(self, view: list[Any]) -> bool:
        pile = self.branch.msgs.messages
        return self._estimate_view([pile[uid] for uid in view]) <= self.cap

    def _delivery_keep(self) -> set:
        """Undelivered results and their original calls survive either compaction mode."""
        from lionagi.protocols.messages import ActionResponse

        delivered = set(self.branch.metadata["context_delivered"])
        pile = self.branch.msgs.messages
        keep = {uid for uid in self.branch.progression if str(uid) not in delivered}
        for uid in list(keep):
            message = pile[uid]
            if not isinstance(message, ActionResponse):
                continue
            original = message
            original_id = message.metadata.get("clipped_from")
            if original_id is not None and original_id in pile:
                original = pile[original_id]
            request_id = original.content.action_request_id
            if request_id is not None and request_id in pile:
                keep.add(pile[request_id].id)
        return keep

    async def fold(self, *, reason: str, observed: int | None = None) -> bool:
        """Fold until the estimate is at or under ``target`` of the cap; True if any fold landed.

        Stages: delivered tool calls/results, recoverable result clips, then delivered
        reasoning when the floor view fits. The two folds try their wider tail first.
        ``observed`` is the estimate a refusal reported for the request it refused; it
        calibrates this estimate first; a refusal resumes only after a pass strictly shrinks.
        """
        if self._busy:
            return False
        self._busy = True
        try:
            if observed:
                raw = self._estimate(self.branch) * self._provider_scale()
                if raw > 0:
                    self._scale = max(self._scale, observed / raw)
            # Compile before any pass, including clip: values without a YAML form refuse here.
            self.estimate()
            landed = False
            for keep_tail in _KEEP_TAILS:
                if self.estimate() <= self.target * self.cap and not (
                    observed is not None and not landed
                ):
                    return landed
                landed = await self._fold_once("tool_io", keep_tail, reason) or landed
            if self.estimate() > self.target * self.cap:
                landed = await self._clip_once(reason) or landed
            if self.estimate() <= self.target * self.cap and not (
                observed is not None and not landed
            ):
                return landed
            from lionagi.protocols.messages import Instruction

            active = list(self.branch.progression)
            pile = self.branch.msgs.messages
            keep = self._delivery_keep() | set(active[-2:])
            if active:
                keep.add(active[0])
            anchor = next((uid for uid in active if isinstance(pile[uid], Instruction)), None)
            if anchor is not None:
                keep.add(anchor)
            floor = self._estimate_view([pile[uid] for uid in active if uid in keep])
            if floor > self.cap:
                return landed
            tails = (2,) if floor > self.target * self.cap else _KEEP_TAILS
            for keep_tail in tails:
                if self.estimate() <= self.target * self.cap and not (
                    observed is not None and not landed
                ):
                    return landed
                landed = await self._fold_once("all", keep_tail, reason) or landed
            return landed
        finally:
            self._busy = False

    def _compact_view(self) -> Callable[..., Any]:
        """The context tool's own function bound to this branch: the fold compacts with it."""
        if self._compact is None:
            from lionagi.tools.context.context import ContextTool

            self._compact = ContextTool().bind(self.branch).func_callable
        return self._compact

    async def _clip_bounded_once(self, reason: str, projection: Any) -> bool:
        """Reduce effective original-source allowances; count every proposed full view."""
        from lionagi.protocols.generic.progression import Progression
        from lionagi.protocols.messages import ActionResponse
        from lionagi.protocols.messages._result_projection import (
            _projection_authority,
            _ProjectionError,
        )

        pile = self.branch.msgs.messages
        view = [pile[uid] for uid in self.branch.progression]
        before = self.estimate()
        target = self.target * self.cap
        count = before
        specs = {}
        candidates = []
        restored = set(self.branch.metadata.get("context_restored_results", []))
        read_ranges = self.branch.metadata.get("context_read_results", {})
        full = self.branch.msgs.progression
        for slot, message in enumerate(view):
            if not isinstance(message, ActionResponse):
                continue
            source = projection.source(message)
            original = source.original
            if original.id in specs or str(original.id) in restored:
                continue
            arguments = original.content.arguments
            if (
                original.content.function in ("context", "context_tool")
                and arguments.get("action") == "read_result"
            ):
                index = arguments.get("index")
                if (
                    isinstance(index, int)
                    and 0 <= index < len(full)
                    and str(full[index]) in read_ranges
                ):
                    continue
            projected = projection.project(message)
            kept = [metric.retained for metric in projected.bodies]
            spec = {
                "original": original,
                "source": source,
                "metrics": projected.bodies,
                "kept": kept,
                "initial": list(kept),
                "slot": slot,
                "index": source.index,
                "response": message,
            }
            specs[original.id] = spec
            for number, metric in enumerate(projected.bodies):
                floor = min(1024, metric.k_n)
                if not metric.eligible or metric.retained <= floor:
                    continue
                candidates.append((-metric.retained, source.index, number, original.id, floor))

        def build(spec, projected):
            original = spec["original"]
            return ActionResponse(
                content={
                    "function": original.content.function,
                    "arguments": copy(original.content.arguments, deep=True),
                    "output": projected.output,
                    "error": original.content.error,
                },
                sender=original.sender,
                recipient=original.recipient,
                metadata={
                    "clipped_from": str(original.id),
                    "context_clip_kept": [metric.retained for metric in projected.bodies],
                },
            )

        def measure(spec, projected):
            response = build(spec, projected)
            candidate_view = list(view)
            candidate_view[spec["slot"]] = response
            # Reuse the authoritative originals/positions and descriptor cache for this pass.
            # The inner scope in _estimate_view keys that authority to the temporary branch.
            with _projection_authority(self.branch, projection):
                estimate = self._estimate_view(candidate_view)
            return estimate, response

        for _negative_length, _index, number, original_id, base_floor in sorted(candidates):
            if count <= target:
                break
            spec = specs[original_id]
            metric = spec["metrics"][number]
            body = metric.body
            current = spec["kept"][number]
            # Only this body's recovery ranges constrain this body's trial. An unrelated
            # range already hidden by another body's standing projection must not reject it.
            protected = tuple(
                (max(start, body.start), min(end, body.end))
                for start, end in read_ranges.get(str(original_id), [])
                if start < body.end and end > body.start and start < end
            )

            def protected_fit(retained, body=body, protected=protected):
                head = 3 * retained // 4
                start = body.start + head
                stop = body.end - (retained - head)
                return all(end <= start or begin >= stop for begin, end in protected)

            # Protection containment is monotone in source count; representation fit is not.
            # Never widen the standing preview to reveal a protected range it already hides.
            if not protected_fit(current):
                continue
            low, high = base_floor, current
            while low < high:
                mid = (low + high) // 2
                if protected_fit(mid):
                    high = mid
                else:
                    low = mid + 1
            if low >= current:
                continue

            def project(allowance, minimum, spec=spec, number=number, protected=protected):
                allowances = list(spec["kept"])
                allowances[number] = allowance
                minimums = [0] * len(allowances)
                minimums[number] = minimum
                return projection.project(
                    spec["original"],
                    allowances=allowances,
                    minimums=minimums,
                    protected_ranges=protected,
                )

            # Decimal marker widths can make the numeric floor infeasible while a larger
            # retained count fits. Find the actual lowest legal representation, without
            # treating fit as monotone or probing an empty body that hides its original.
            floor_projection = None
            for legal_floor in range(low, current):
                try:
                    floor_projection = project(legal_floor, legal_floor)
                except _ProjectionError:
                    continue
                break
            if floor_projection is None:
                continue
            floor = floor_projection.bodies[number].retained
            floor_count, floor_response = measure(spec, floor_projection)

            while count > target and current > floor:
                contribution = count - floor_count
                chosen = None
                if contribution > 0:
                    ratio = contribution / (current - floor)
                    allowance = max(
                        floor,
                        current - max(1, math.ceil((count - target) / ratio)),
                    )
                    allowance = min(allowance, current - 1)
                    try:
                        trial = project(allowance, floor)
                    except _ProjectionError:
                        trial = None
                    if trial is not None:
                        after, response = measure(spec, trial)
                        if after < count:
                            chosen = (after, response, trial)
                    # A discontinuous marker or tokenizer can defeat the interpolation.
                    # The measured legal floor is a valid fallback, never an empty probe.
                    if chosen is None:
                        chosen = (floor_count, floor_response, floor_projection)
                else:
                    # No measured floor contribution: skip this heuristic proposal.
                    break
                if chosen is None or chosen[0] >= count:
                    break
                after, response, accepted = chosen
                actual = [item.retained for item in accepted.bodies]
                if actual[number] >= current:
                    break
                # No trial enters the manager, record or local accepted view until both
                # its original-derived allowance and its complete request strictly shrink.
                view[spec["slot"]] = response
                spec["kept"] = actual
                spec["response"] = response
                count = after
                current = actual[number]

        changed = [spec for spec in specs.values() if spec["kept"] != spec["initial"]]
        landed = bool(changed) and count < before
        clipped = []
        for spec in changed:
            for number, metric in enumerate(spec["metrics"]):
                if spec["kept"][number] != spec["initial"][number]:
                    clipped.append(
                        {
                            "index": spec["index"],
                            "chars": metric.original_length,
                            "kept": spec["kept"][number],
                        }
                    )
        if landed:
            for spec in changed:
                response = await self.branch.msgs._a_add_built_response(spec["response"])
                view[spec["slot"]] = response
            self.branch.metadata["current_progression"] = Progression(
                order=[message.id for message in view]
            )
        self.records.append(
            {
                "at": time.time(),
                "reason": reason,
                "stage": "clip",
                "kept_tail": None,
                "messages_folded": 0,
                "tool_calls": [],
                "tool_calls_total": 0,
                "summary_source": None,
                "summary_usage": None,
                "estimate_before": before,
                "estimate_after": count if landed else before,
                "cap": self.cap,
                "input_scale": round(self._provider_scale() * self._scale, 4),
                "outcome": "landed" if landed else "rolled_back",
                "target_reached": (count if landed else before) <= target,
                "clipped": clipped[:_MAX_RECORDED_CALLS] if landed else [],
                "clipped_total": len(clipped) if landed else 0,
            }
        )
        return landed

    async def _clip_once(self, reason: str) -> bool:
        from lionagi.protocols.messages._result_projection import _projection_session

        projection = _projection_session(self.branch, self.branch.chat_model)
        if projection is not None:
            return await self._clip_bounded_once(reason, projection)

        from lionagi.protocols.generic.progression import Progression
        from lionagi.protocols.messages import ActionResponse
        from lionagi.tools.context.context import _result_text

        active = list(self.branch.progression)
        pile = self.branch.msgs.messages
        view = [pile[uid] for uid in active]
        before = self.estimate()
        target = self.target * self.cap
        specs = {}
        candidates = []
        restored = set(self.branch.metadata.get("context_restored_results", []))
        read_ranges = self.branch.metadata.get("context_read_results", {})
        for slot, message in enumerate(view):
            if not isinstance(message, ActionResponse):
                continue
            original_id = message.metadata.get("clipped_from")
            original = (
                pile[original_id] if original_id is not None and original_id in pile else message
            )
            if original.id in specs or str(original.id) in restored:
                continue
            arguments = original.content.arguments
            if (
                original.content.function in ("context", "context_tool")
                and arguments.get("action") == "read_result"
            ):
                index = arguments.get("index")
                full = self.branch.msgs.progression
                if (
                    isinstance(index, int)
                    and 0 <= index < len(full)
                    and str(full[index]) in read_ranges
                ):
                    # The just-read page is itself a new tool result. It must reach a main
                    # request whole, rather than being clipped again as another large body.
                    continue
            try:
                text, bodies = _result_text(original.content.output)
            except (TypeError, ValueError):
                # The compiler has already accepted this YAML value; the JSON encoder
                # refuses it, so it stays whole and is not a clip body.
                continue
            lengths = [body.end - body.start for body in bodies]
            kept = list(message.metadata.get("context_clip_kept", lengths))
            if len(kept) != len(lengths):
                kept = lengths
            spec = {
                "original": original,
                "text": text,
                "bodies": bodies,
                "kept": kept,
                "initial": list(kept),
                "slot": slot,
                "index": self.branch.msgs.progression.index(original.id),
            }
            specs[original.id] = spec
            for number, body in enumerate(bodies):
                n = lengths[number]
                if body.protected or n <= 1024:
                    continue

                def protected_fit(k, body=body, n=n, original=original):
                    head = 3 * k // 4
                    omitted_start = body.start + head
                    omitted_end = body.end - (k - head)
                    return all(
                        end <= omitted_start or start >= omitted_end
                        for start, end in read_ranges.get(str(original.id), [])
                    )

                low, high = 1024, n
                while low < high:
                    mid = (low + high) // 2
                    if protected_fit(mid):
                        high = mid
                    else:
                        low = mid + 1
                if kept[number] > low:
                    candidates.append((-kept[number], spec["index"], number, original.id, low))

        def build(spec, empty=None):
            original = spec["original"]
            output = copy(original.content.output, deep=True)
            for number, body in enumerate(spec["bodies"]):
                if body.protected:
                    continue
                raw = spec["text"][body.start : body.end]
                kept = spec["kept"][number]
                rendered = raw
                if empty == number:
                    rendered = ""
                elif kept < len(raw):
                    head = 3 * kept // 4
                    tail = kept - head
                    start = body.start + head
                    stop = body.end - tail
                    marker = (
                        f"\n[{len(raw) - kept} characters omitted; recovery range [{start}, {stop}); "
                        f'context(action="read_result", index={spec["index"]}, '
                        f"offset={start}, limit={min(16000, stop - start)}); "
                        f"continue at next_offset with limit=min(16000, {stop}-next_offset), "
                        f"stopping at {stop}.]\n"
                    )
                    rendered = raw[:head] + marker + raw[-tail:]
                if body.is_json:
                    rendered = "[JSON preview]\n" + rendered
                if isinstance(output, dict):
                    output[body.key] = rendered
                else:
                    output = rendered
            return ActionResponse(
                content={
                    "function": original.content.function,
                    "arguments": copy(original.content.arguments, deep=True),
                    "output": output,
                    "error": original.content.error,
                },
                sender=original.sender,
                recipient=original.recipient,
                metadata={
                    "clipped_from": str(original.id),
                    "context_clip_kept": list(spec["kept"]),
                },
            )

        count = before
        for _negative_length, _index, number, original_id, floor in sorted(candidates):
            if count <= target:
                break
            spec = specs[original_id]
            current = spec["kept"][number]
            empty_view = list(view)
            empty_view[spec["slot"]] = build(spec, empty=number)
            contribution = count - self._estimate_view(empty_view)
            if contribution <= 0:
                continue
            ratio = contribution / current
            proposal = max(floor, current - max(1, math.ceil((count - target) / ratio)))
            previous_message = view[spec["slot"]]
            while True:
                spec["kept"][number] = proposal
                view[spec["slot"]] = build(spec)
                after = self._estimate_view(view)
                if after <= target or proposal == floor:
                    break
                proposal = max(floor, proposal - max(1, math.ceil((after - target) / ratio)))
            if after < count:
                count = after
            else:
                spec["kept"][number] = current
                view[spec["slot"]] = previous_message
        changed = [spec for spec in specs.values() if spec["kept"] != spec["initial"]]
        landed = bool(changed) and count < before
        clipped = []
        for spec in changed:
            for number, body in enumerate(spec["bodies"]):
                if spec["kept"][number] != spec["initial"][number]:
                    clipped.append(
                        {
                            "index": spec["index"],
                            "chars": body.end - body.start,
                            "kept": spec["kept"][number],
                        }
                    )
        if landed:
            for spec in changed:
                response = await self.branch.msgs._a_add_built_response(build(spec))
                view[spec["slot"]] = response
            self.branch.metadata["current_progression"] = Progression(
                order=[message.id for message in view]
            )
        self.records.append(
            {
                "at": time.time(),
                "reason": reason,
                "stage": "clip",
                "kept_tail": None,
                "messages_folded": 0,
                "tool_calls": [],
                "tool_calls_total": 0,
                "summary_source": None,
                "summary_usage": None,
                "estimate_before": before,
                "estimate_after": count if landed else before,
                "cap": self.cap,
                "input_scale": round(self._provider_scale() * self._scale, 4),
                "outcome": "landed" if landed else "rolled_back",
                "target_reached": (count if landed else before) <= target,
                "clipped": clipped[:_MAX_RECORDED_CALLS] if landed else [],
                "clipped_total": len(clipped) if landed else 0,
            }
        )
        return landed

    async def _fold_once(self, mode: str, keep_tail: int, reason: str) -> bool:
        from lionagi.protocols.messages import ActionRequest, ActionResponse, Instruction

        active = list(self.branch.progression)
        end = len(active) - keep_tail
        if end <= 0:
            return False
        pile = self.branch.msgs.messages
        span = [pile[uid] for uid in active[:end] if uid in pile]
        keep = self._delivery_keep()
        tool_msgs = [
            m for m in span if isinstance(m, ActionRequest | ActionResponse) and m.id not in keep
        ]
        if mode == "tool_io" and not tool_msgs:
            return False
        # Compaction keeps the first active instruction (the task) and the message at the
        # head of the view whatever the range, so a span holding nothing else has nothing to
        # fold and is not worth a summary request.
        head = active[0] if active else None
        anchor = next((m for m in span if isinstance(m, Instruction)), None)
        folded = [m for m in span if m is not anchor and m.id != head and m.id not in keep]
        if mode == "all" and not folded:
            return False
        calls = self._tool_calls(tool_msgs)
        # An `all` fold replaces this fold's earlier notes as well; the calls they list carry
        # forward, so the agent keeps every file it read and search it ran whatever the summary
        # keeps.
        delivered_span = [message for message in span if message.id not in keep]
        listed = self._carried_calls(delivered_span) if mode == "all" else calls
        before = self.estimate()
        summary_source = "host"
        self._summary_usage = None
        model_summary = None
        if mode == "all":
            model_summary = await self._model_summary(delivered_span)
            if model_summary:
                summary_source = "model"
        n_folded = len(tool_msgs) if mode == "tool_io" else len(folded)
        note = self._note(n_folded, listed, model_summary)
        result = await self._compact_view()(
            action="compact",
            start=0,
            end=end,
            mode=mode,
            summary=note,
            _keep=keep,
        )
        if not result.get("success"):
            self._log(f"context fold ({mode}) not applied: {result.get('error')}")
            return False
        before_ids = set(active)
        for uid in self.branch.progression:
            if uid not in before_ids:
                self._note_calls[uid] = listed
        after = self.estimate()
        # Both sides use the scale settled by the summary, rather than the old scale.
        before = self._estimate_view([pile[uid] for uid in active])
        landed = after < before
        if not landed:
            from lionagi.protocols.generic.progression import Progression

            rejected = set(self.branch.progression) - before_ids
            self.branch.metadata["current_progression"] = Progression(order=active)
            for uid in rejected:
                self._note_calls.pop(uid, None)
        else:
            carried = self.branch.metadata.setdefault("context_fold_calls", {})
            for uid in set(self.branch.progression) - before_ids:
                if uid in self._note_calls:
                    carried[str(uid)] = copy(self._note_calls[uid], deep=True)
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
            "outcome": "landed" if landed else "rolled_back",
            "target_reached": (after if landed else before) <= self.target * self.cap,
            "summary_usage": self._summary_usage,
        }
        self.records.append(record)
        self._log(
            f"context fold ({reason}, {mode}): {record['messages_folded']} messages folded, "
            f"estimated input {before:,} -> {after:,} tokens (cap {self.cap:,})"
        )
        return landed

    def _tool_calls(self, tool_msgs: list[Any]) -> list[dict[str, Any]]:
        from lionagi.protocols.messages import ActionResponse

        calls: list[dict[str, Any]] = []
        seen = set()
        for msg in tool_msgs:
            if not isinstance(msg, ActionResponse):
                continue
            original_id = msg.metadata.get("clipped_from")
            if original_id is not None and original_id in self.branch.msgs.messages:
                msg = self.branch.msgs.messages[original_id]
            if msg.id in seen:
                continue
            seen.add(msg.id)
            c = msg.content
            calls.append(
                {
                    "function": c.function,
                    "arguments": _short(c.arguments),
                    "output_chars": _size(c.output),
                    "index": self.branch.msgs.progression.index(msg.id),
                }
            )
        unique = {}
        for call in calls:
            unique.setdefault(call.get("index", (call["function"], call["arguments"])), call)
        return list(unique.values())

    def _carried_calls(self, span: list[Any]) -> list[dict[str, Any]]:
        """The calls behind a span in order: its own tool results and its earlier notes' lists."""
        from lionagi.protocols.messages import ActionResponse

        calls: list[dict[str, Any]] = []
        for msg in span:
            if msg.id in self._note_calls:
                calls.extend(self._note_calls[msg.id])
            elif isinstance(msg, ActionResponse):
                calls.extend(self._tool_calls([msg]))
        unique = {}
        for call in calls:
            unique.setdefault(call.get("index", (call["function"], call["arguments"])), call)
        return list(unique.values())

    def _note(self, n: int, calls: list[dict[str, Any]], model_summary: str | None) -> str:
        head = _NOTE_HEAD.format(n=n, cap=self.cap)
        body = f"\n\nSummary of the folded reasoning:\n{model_summary}" if model_summary else ""
        text = head + body
        if calls:
            size_label = (
                "stored-output size"
                if getattr(self.branch.chat_model, "tool_result_max_chars", None) is not None
                else "output size"
            )
            text += f"\n\nTool calls folded ({len(calls)}; function, arguments, {size_label}):"
            for i, call in enumerate(calls):
                line = f"\n- [{call['index']}] {call['function']} {call['arguments']} ({call['output_chars']:,} chars)"
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
        from lionagi.protocols.messages._result_projection import _projection_session
        from lionagi.service.imodel import _branch_routing_context

        projection = _projection_session(self.branch, self.branch.chat_model)
        budget_chars = self.cap * 2
        texts: list[str] = []
        used = 0
        for msg in span:
            if projection is None:
                rendered = msg.rendered
            else:
                from lionagi.protocols.messages import ActionResponse

                rendered = (
                    msg.content.with_updates(output=projection.project(msg).output).rendered
                    if isinstance(msg, ActionResponse)
                    else msg.rendered
                )
            text = _text(rendered)
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
            from lionagi.operations._api_hooks import _extract_tokens, _typed_usage

            self._summary_usage = _typed_usage(_extract_tokens(api_call.response))
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
    branch: Branch,
    *,
    cap: int,
    fold_at: float = _FOLD_AT,
    target: float = _FOLD_TARGET,
    log: Callable[[str], None] | None = None,
) -> ContextFold:
    """Attach a ContextFold to the branch's API_PRE_CALL hook point and return it.

    This fold supports chat and operate requests using the chat path, which compiles after
    API_PRE_CALL. The streaming run path compiles before that hook and is outside this fold's
    supported request scope.

    A branch with no HookBus yet gets one of its own; a Session joined later takes the handler
    over through ``attach_hook_bus``, as it does for every queued branch handler. A branch keeps
    one fold: installing again returns it only when cap, line and target agree.
    """
    from lionagi.hooks.bus import HookBus, HookPoint

    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise ValueError("cap must be a positive integer")
    _validate_fold_pair(fold_at, target)
    # One fold per branch: a second install returns the first instead of adding a second handler.
    for point, handler in branch._pending_hook_bus_entries:
        existing = getattr(handler, "__self__", None)
        if point == HookPoint.API_PRE_CALL and isinstance(existing, ContextFold):
            if (existing.cap, existing.fold_at, existing.target) != (cap, fold_at, target):
                raise ValueError(
                    "branch already has a fold with a different cap, fold_at or target"
                )
            return existing
    fold = ContextFold(branch, cap=cap, fold_at=fold_at, target=target, log=log)
    branch._pending_hook_bus_entries.append((HookPoint.API_PRE_CALL, fold.on_api_pre_call))
    branch._pending_hook_bus_entries.append((HookPoint.API_POST_CALL, fold.on_api_post_call))
    branch.attach_hook_bus(branch._hooks if branch._hooks is not None else HookBus())
    return fold

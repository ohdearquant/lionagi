# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Context tool — non-destructive active-progression management for a branch."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from lionagi.ln.types import Enum
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.messages._result_projection import (
    _projection_session,
)
from lionagi.protocols.messages._result_projection import (
    _result_json as _result_json,
)
from lionagi.protocols.messages._result_projection import (
    _result_text as _result_text,
)
from lionagi.protocols.messages._result_projection import (
    _ResultBody as _ResultBody,
)
from lionagi.service.token_calculator import TokenCalculator

from ..base import LionTool

if TYPE_CHECKING:
    from lionagi.session.branch import Branch

_automatic_summary: ContextVar[bool] = ContextVar("context_automatic_summary", default=False)


class ContextAction(str, Enum):
    status = "status"
    get_messages = "get_messages"
    evict = "evict"
    evict_action_results = "evict_action_results"
    restore = "restore"
    compact = "compact"
    read_result = "read_result"


class ContextRequest(BaseModel):
    action: ContextAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'status': context usage — active vs total messages, evicted count, "
            "estimated active tokens. Check this to decide when to reclaim space.\n"
            "- 'get_messages': list messages with index, role, content preview. "
            "scope='active' (default) lists your current view; scope='all' lists the "
            "FULL record including evicted messages (marked [evicted]) so you can find "
            "something to restore.\n"
            "- 'evict': remove messages [start:end) from your ACTIVE view (not deleted — "
            "still in the record). Index 0 (system) cannot be evicted.\n"
            "- 'evict_action_results': evict all but the most recent `keep_last` tool-result "
            "messages — the best way to reclaim space from verbose tool outputs.\n"
            "- 'restore': pull evicted messages back into your active view by their index "
            "in the FULL record (use get_messages scope='all' to find them).\n"
            "- 'read_result': recover stored original tool-result text by its full-record "
            "index and character offset; reads at most 16,000 characters without changing "
            "the view or running the tool again.\n"
            "- 'compact': replace a span [start:end) of your active view with a single "
            "`summary` you write yourself (no extra model call). By default only verbose "
            "tool requests/results in the span are collapsed; your own reasoning, the task, "
            "and guidance are KEPT. Originals stay in the record (restorable). Write the "
            "summary to capture what matters: root cause, fix applied, verification state, "
            "and any files you'll need to re-read."
        ),
    )
    start: int | None = Field(
        None,
        description=(
            "Start index (inclusive, 0-based). For 'evict'/'compact'/'get_messages' "
            "scope='active' this indexes your active view. For 'restore' and "
            "'get_messages' scope='all' it indexes the FULL record. Index 0 is the "
            "system message and cannot be evicted/compacted."
        ),
    )
    end: int | None = Field(
        None,
        description=(
            "End index (exclusive, 0-based). If omitted: end of conversation for "
            "get_messages, start+1 for evict/restore, end of view for compact."
        ),
    )
    keep_last: int | None = Field(
        None,
        description=(
            "For 'evict_action_results': keep the N most recent tool-result messages, "
            "evict all older ones. Defaults to 5."
        ),
    )
    summary: str | None = Field(
        None,
        description=(
            "For 'compact': the replacement text you write to stand in for the collapsed "
            "span. Make it self-sufficient — root cause, fix applied (file + lines), what "
            "you've verified, and files to re-read later."
        ),
    )
    mode: str | None = Field(
        None,
        description=(
            "For 'compact': 'tool_io' (default) collapses only tool request/result "
            "messages in the span and keeps your reasoning + the task/guidance in view; "
            "'all' collapses everything in the span except the system message and the "
            "first instruction in your view, which every model call needs."
        ),
    )
    scope: str | None = Field(
        None,
        description=(
            "For 'get_messages': 'active' (default) = your current view; 'all' = the full "
            "record including evicted messages (each marked [active]/[evicted])."
        ),
    )
    auto: bool = Field(
        False,
        description=(
            "For 'compact': when True and `summary` is omitted, generate the summary "
            "automatically with one extra model call over the collapsed span instead of "
            "writing it yourself. Prefer writing your own summary — auto is a fallback "
            "for when you can't, and it can fail if the model call errors."
        ),
    )
    index: int | None = Field(None, ge=0, description="Original result's full-record index.")
    offset: int = Field(0, ge=0, description="Character offset for read_result.")
    limit: int = Field(16000, ge=1, description="Characters to recover, capped at 16,000.")


class ContextTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "context_tool"

    #: Drop-in guidance for a system prompt. Tells the model how to use the tool.
    GUIDANCE = (
        "You can engineer your own context with `context_tool`. The full "
        "conversation is always preserved; you only curate what is fed back to "
        "you each turn. When the active context grows large (check with "
        "action='status'), reclaim space: use action='evict_action_results' to "
        "drop stale tool outputs, or action='compact' with a `summary` you write "
        "to collapse a long exploratory span into a few lines (capture the root "
        "cause, the fix you applied, what you verified, and any files to re-read). "
        "Nothing is lost — use action='get_messages' scope='all' to see evicted "
        "messages and action='restore' to bring any of them back."
    )

    def bind(self, branch: Branch) -> Tool:
        from lionagi.protocols.generic.progression import Progression
        from lionagi.protocols.messages import (
            ActionRequest,
            ActionResponse,
            AssistantResponse,
            Instruction,
        )

        msgs = branch.msgs

        def _ensure_cp() -> Progression:
            """Lazily snapshot the full progression into the active view."""
            if "current_progression" not in branch.metadata:
                cp = Progression()
                for uid in msgs.progression:
                    cp.append(uid)
                branch.metadata["current_progression"] = cp
            return branch.metadata["current_progression"]

        def _grammar_error(view: list, summary_at: int, pile) -> str | None:
            """Why a proposed active view cannot be compiled into a chat call.

            ``view`` is the active view with the collapsed messages removed and
            ``summary_at`` the position the assistant summary would take. The
            chat compiler requires the first model-facing turn to be an
            Instruction when a system message is set.
            """
            roles: list[str] = []
            for i, uid in enumerate([*view, None]):
                if i == summary_at:
                    roles.append("assistant")
                if uid is None:
                    break
                if uid not in pile:
                    return "the active view references an unavailable message"
                message = pile[uid]
                if isinstance(message, Instruction):
                    roles.append("instruction")
                elif isinstance(message, AssistantResponse):
                    roles.append("assistant")
            if msgs.system and roles and roles[0] == "assistant":
                return "the active view would begin with an assistant response"
            return None

        def _tokens(msg) -> int:
            c = getattr(msg, "content", "") or ""
            c = c if isinstance(c, str) else str(c)
            return TokenCalculator.tokenize(c) if c else 0

        def _preview(idx: int, msg, tag: str = "") -> str:
            role = getattr(msg, "role", None) or type(msg).__name__
            c = getattr(msg, "content", "") or ""
            c = c if isinstance(c, str) else str(c)
            c = c[:120].replace("\n", " ") + ("..." if len(c) > 120 else "")
            return f"[{idx}]{tag} {role}: {c}"

        def _chronology(uid) -> int:
            message = msgs.messages[uid]
            original = message.metadata.get("clipped_from")
            if original is not None and original in msgs.messages:
                return msgs.progression.index(msgs.messages[original].id)
            return message.metadata.get("context_first_index", msgs.progression.index(uid))

        async def _auto_summarize(uids: list, pile) -> str | None:
            """Generate a compact summary over `uids` with one direct model call.
            Model-call failures return None for caller fallback; invalid result projections propagate.
            """
            projection = _projection_session(branch, branch.chat_model)
            texts: list[str] = []
            total = 0
            max_chars = 6000
            for u in uids:
                if u not in pile:
                    continue
                m = pile[u]
                c = getattr(m, "content", "") or ""
                if projection is not None and isinstance(m, ActionResponse):
                    c = c.with_updates(output=projection.project(m).output)
                c = c if isinstance(c, str) else str(c)
                if not c:
                    continue
                remaining = max_chars - total
                if remaining <= 0:
                    break
                snippet = c[:remaining]
                texts.append(snippet)
                total += len(snippet)
            if not texts:
                return None
            joined = "\n---\n".join(texts)
            prompt = (
                "Summarize the following collapsed conversation span for context "
                "compaction. Capture: the root cause of anything investigated, the "
                "fix applied (file + lines) if any, what has been verified, and any "
                "files that will need to be re-read later. Keep it to a few sentences."
                "\n\n" + joined
            )
            try:
                token = _automatic_summary.set(True)
                try:
                    text = await branch.chat(instruction=prompt)
                finally:
                    _automatic_summary.reset(token)
            except Exception:
                return None
            if not isinstance(text, str):
                return None
            text = text.strip()
            return text or None

        async def context_tool(
            action: str,
            start: int | None = None,
            end: int | None = None,
            keep_last: int | None = None,
            summary: str | None = None,
            mode: str | None = None,
            scope: str | None = None,
            auto: bool = False,
            index: int | None = None,
            offset: int = 0,
            limit: int = 16000,
            _keep: set | None = None,
        ) -> dict:
            """Engineer your own conversation context — check usage, browse, evict, restore, and compact messages.
            Evicted/compacted messages stay in the full conversation record and can be restored at any time.
            """
            active = branch.progression  # current view (respects evictions)
            full = msgs.progression  # complete durable record
            pile = msgs.messages

            if action == "read_result":
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or not 0 <= index < len(full)
                    or not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or offset < 0
                    or not isinstance(limit, int)
                    or isinstance(limit, bool)
                    or limit < 1
                ):
                    return {"success": False, "error": "Invalid result index, offset or limit."}
                original = pile[full[index]]
                if not isinstance(original, ActionResponse) or original.metadata.get(
                    "clipped_from"
                ):
                    return {
                        "success": False,
                        "error": "Index must address an original tool result.",
                    }
                try:
                    text, _bodies = _result_text(original.content.output)
                except (TypeError, ValueError):
                    return {"success": False, "error": "Result has no JSON recovery text."}
                applied_limit = min(limit, 16000)
                bound = getattr(branch.chat_model, "tool_result_max_chars", None)
                if bound is not None:
                    applied_limit = min(applied_limit, bound)
                stop = min(len(text), offset + applied_limit)
                if stop > offset:
                    ranges = branch.metadata.setdefault("context_read_results", {})
                    ranges.setdefault(str(original.id), []).append([offset, stop])
                result = {
                    "success": True,
                    "text": text[offset:stop],
                    "total": len(text),
                    "next_offset": stop if stop < len(text) else None,
                }
                if bound is not None:
                    result["limit"] = applied_limit
                return result

            if action == "status":
                by_type: dict[str, int] = {}
                tok = 0
                for uid in active:
                    if uid in pile:
                        m = pile[uid]
                        r = getattr(m, "role", None) or type(m).__name__
                        by_type[str(r)] = by_type.get(str(r), 0) + 1
                        tok += _tokens(m)
                return {
                    "success": True,
                    "active_messages": len(active),
                    "total_messages": len(full),
                    "evicted": len(full) - len(active),
                    "by_type": by_type,
                    "estimated_active_tokens": tok,
                }

            if action == "get_messages":
                if (scope or "active") == "all":
                    active_set = set(active)
                    s = max(0, start or 0)
                    e = min(len(full), end if end is not None else len(full))
                    out = []
                    for i in range(s, e):
                        uid = full[i]
                        if uid in pile:
                            tag = " [active]" if uid in active_set else " [evicted]"
                            out.append(_preview(i, pile[uid], tag))
                    return {
                        "success": True,
                        "scope": "all",
                        "range": f"[{s}:{e}] of {len(full)}",
                        "messages": out,
                    }
                s = max(0, start or 0)
                e = min(len(active), end if end is not None else len(active))
                out = [_preview(i, pile[active[i]]) for i in range(s, e) if active[i] in pile]
                return {
                    "success": True,
                    "scope": "active",
                    "range": f"[{s}:{e}] of {len(active)}",
                    "messages": out,
                }

            if action == "evict":
                cp = _ensure_cp()
                s = max(1, start or 1)
                e = end if end is not None else s + 1
                e = min(len(cp), e)
                if s >= e:
                    return {"success": False, "error": f"Invalid range [{s}:{e})"}
                uids = [cp[i] for i in range(s, e) if i < len(cp)]
                cp.exclude(uids)
                return {
                    "success": True,
                    "removed": len(uids),
                    "active": len(cp),
                    "total": len(full),
                }

            if action == "evict_action_results":
                cp = _ensure_cp()
                keep = keep_last if keep_last is not None else 5
                ar = [u for u in cp if u in pile and isinstance(pile[u], ActionResponse)]
                if len(ar) <= keep:
                    return {
                        "success": True,
                        "removed": 0,
                        "message": f"Only {len(ar)} tool results, keeping all.",
                    }
                to_evict = ar[:-keep] if keep > 0 else ar
                cp.exclude(to_evict)
                return {
                    "success": True,
                    "removed": len(to_evict),
                    "active": len(cp),
                    "total": len(full),
                }

            if action == "restore":
                view = list(active)
                s = max(0, start or 0)
                e = end if end is not None else s + 1
                e = min(len(full), e)
                restored = 0
                originals = []
                clipped_originals = {
                    str(message.metadata["clipped_from"])
                    for message in pile
                    if message.metadata.get("clipped_from") is not None
                }

                for i in range(s, e):
                    uid = full[i]
                    message = pile[uid]
                    if message.metadata.get("clipped_from") or uid in view:
                        continue
                    copies = [
                        au for au in view if pile[au].metadata.get("clipped_from") == str(uid)
                    ]
                    if copies:
                        insert_at = view.index(copies[0])
                        view = [au for au in view if au not in copies]
                    else:
                        key = _chronology(uid)
                        insert_at = next(
                            (j for j, au in enumerate(view) if _chronology(au) > key), len(view)
                        )
                    view.insert(insert_at, uid)
                    if isinstance(message, ActionResponse) and str(uid) in clipped_originals:
                        originals.append(uid)
                    restored += 1
                if originals and branch._context_restore_check is not None:
                    if not branch._context_restore_check(view):
                        return {
                            "success": False,
                            "error": "Restore exceeds the input ceiling; use read_result for the original text.",
                        }
                branch.metadata["current_progression"] = Progression(order=view)
                if originals:
                    protected = branch.metadata.setdefault("context_restored_results", [])
                    protected.extend(str(uid) for uid in originals if str(uid) not in protected)
                return {
                    "success": True,
                    "restored": restored,
                    "active": len(view),
                    "total": len(full),
                }

            if action == "compact":
                cp = _ensure_cp()
                s = max(1, start or 1)
                e = end if end is not None else len(cp)
                e = min(len(cp), e)
                if s >= e:
                    return {"success": False, "error": f"Invalid range [{s}:{e})"}

                span = [cp[i] for i in range(s, e) if i < len(cp)]
                if (mode or "tool_io") == "tool_io":
                    collapse = [
                        u
                        for u in span
                        if u in pile and isinstance(pile[u], ActionRequest | ActionResponse)
                    ]
                else:  # "all" — collapse everything in the span (system is at idx 0, excluded)
                    # The first instruction in the view stays: the chat compiler
                    # needs one before any assistant turn, and pinning the first
                    # durable message would not help a view whose prefix is
                    # already evicted.
                    anchor = next(
                        (u for u in cp if u in pile and isinstance(pile[u], Instruction)),
                        None,
                    )
                    collapse = [u for u in span if u != anchor]
                if not collapse:
                    return {
                        "success": False,
                        "error": "Nothing to compact in range (no tool messages; "
                        "use mode='all' to collapse reasoning too).",
                    }
                if _keep:
                    collapse = [uid for uid in collapse if uid not in _keep]
                    if not collapse:
                        return {"success": False, "error": "No delivered messages to compact."}

                # The summary takes the place of the first message it replaces,
                # so a kept message at the start of the span stays ahead of it.
                collapsed = set(collapse)
                view = list(cp)
                summary_at = view.index(collapse[0])
                remaining = [u for u in view if u not in collapsed]
                grammar_error = _grammar_error(remaining, summary_at, pile)
                if grammar_error is not None:
                    return {
                        "success": False,
                        "error": (
                            f"Compaction refused: {grammar_error}, which the next model "
                            "call could not render. Nothing was changed; choose a "
                            "different range."
                        ),
                    }

                if (not summary or not summary.strip()) and auto:
                    summary = await _auto_summarize(collapse, pile)
                    if not summary:
                        return {
                            "success": False,
                            "error": (
                                "Auto-summarization failed or produced nothing — write "
                                "the summary yourself and retry compact with `summary=...`."
                            ),
                        }

                if not summary or not summary.strip():
                    return {"success": False, "error": "compact requires a non-empty `summary`."}

                tokens_freed = sum(_tokens(pile[u]) for u in collapse if u in pile)
                first_index = min(_chronology(uid) for uid in collapse)
                note = await msgs.a_add_message(
                    assistant_response=AssistantResponse(
                        content={"assistant_response": f"[CONTEXT COMPACTION] {summary.strip()}"},
                        metadata={"context_first_index": first_index},
                    )
                )
                # A new message joins the end of the view; the summary belongs where the
                # first collapsed message was, then the collapsed uids are dropped.
                if note.id in cp:
                    cp.remove(note.id)
                cp.insert(summary_at, note.id)
                cp.exclude(collapse)
                return {
                    "success": True,
                    "compacted": len(collapse),
                    "tokens_freed_est": tokens_freed,
                    "active": len(cp),
                    "total": len(full),
                }

            return {"success": False, "error": f"Unknown action: {action}"}

        return Tool(func_callable=context_tool, request_options=ContextRequest)

    def to_tool(self) -> Tool:
        raise NotImplementedError(
            "ContextTool requires branch context. Use ContextTool().bind(branch) instead."
        )

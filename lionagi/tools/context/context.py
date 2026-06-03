# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Context tool — let a model engineer its own inference context.

Design principle: **NON-DESTRUCTIVE**. The full conversation lives forever in
the branch's message Pile (the durable record). What the model curates is the
*active progression* — the ordered subset actually fed to the chat-completion
call (lionagi passes ``progression=branch.progression`` into the request, and
``branch.progression`` returns ``metadata["current_progression"]`` when set).

So "evict" / "compact" only remove messages from the **view** the model is
inferenced over; nothing is deleted. The model can ``get_messages(scope="all")``
to see everything (including evicted), and ``restore`` to pull anything back
into its active context. It is engineering the context window it pays for and
reasons over, while the entire history remains available on demand.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from lionagi.protocols.action.tool import Tool
from lionagi.service.token_calculator import TokenCalculator

from ..base import LionTool

if TYPE_CHECKING:
    from lionagi.session.branch import Branch


class ContextAction(str, Enum):
    status = "status"
    get_messages = "get_messages"
    evict = "evict"
    evict_action_results = "evict_action_results"
    restore = "restore"
    compact = "compact"


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
            "'all' collapses everything in the span except the system message."
        ),
    )
    scope: str | None = Field(
        None,
        description=(
            "For 'get_messages': 'active' (default) = your current view; 'all' = the full "
            "record including evicted messages (each marked [active]/[evicted])."
        ),
    )


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
        from lionagi.protocols.messages import ActionRequest, ActionResponse

        msgs = branch.msgs

        def _ensure_cp() -> Progression:
            """Lazily snapshot the full progression into the active view."""
            if "current_progression" not in branch.metadata:
                cp = Progression()
                for uid in msgs.progression:
                    cp.append(uid)
                branch.metadata["current_progression"] = cp
            return branch.metadata["current_progression"]

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

        async def context_tool(
            action: str,
            start: int = None,
            end: int = None,
            keep_last: int = None,
            summary: str = None,
            mode: str = None,
            scope: str = None,
        ) -> dict:
            """Engineer your own conversation context — check usage, browse, evict,
            restore, and compact. Evicted/compacted messages are hidden from the
            model's view but preserved in the conversation record and can be
            restored at any time.
            """
            active = branch.progression  # current view (respects evictions)
            full = msgs.progression  # complete durable record
            pile = msgs.messages

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
                cp = _ensure_cp()
                active_set = set(cp)
                s = max(0, start or 0)
                e = end if end is not None else s + 1
                e = min(len(full), e)
                restored = 0
                for i in range(s, e):
                    uid = full[i]
                    if uid in active_set:
                        continue
                    # find the active slot to keep chronological order: first
                    # active uid whose full-index exceeds i, else append.
                    insert_at = len(cp)
                    for j, au in enumerate(cp):
                        if full.index(au) > i:
                            insert_at = j
                            break
                    cp.insert(insert_at, uid)
                    active_set.add(uid)
                    restored += 1
                return {
                    "success": True,
                    "restored": restored,
                    "active": len(cp),
                    "total": len(full),
                }

            if action == "compact":
                if not summary or not summary.strip():
                    return {"success": False, "error": "compact requires a non-empty `summary`."}
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
                    collapse = list(span)
                if not collapse:
                    return {
                        "success": False,
                        "error": "Nothing to compact in range (no tool messages; "
                        "use mode='all' to collapse reasoning too).",
                    }

                tokens_freed = sum(_tokens(pile[u]) for u in collapse if u in pile)
                note = await msgs.a_add_message(
                    assistant_response=f"[CONTEXT COMPACTION] {summary.strip()}"
                )
                # place the summary where the span began, then drop the collapsed uids
                cp.insert(s, note.id)
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

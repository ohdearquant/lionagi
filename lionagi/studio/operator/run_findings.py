# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``run_findings`` Operator read tool: what a run produced.

Per-branch message tails, tool calls with inferred outcomes, errors (both
tool-level and branch/session status failures), and declared artifacts with
their verification state. Bounded and redacted on the same rules as
``run_progress``/the existing read tools — see ``redact.py`` and
docs/internals/studio.md ("Bounded read projections"). Tool-call outcomes
are inferred from message content via the same ``_detect_status`` heuristic
``lionagi.studio.services.runs`` already uses to build the run-detail step
list, since a plain session's ActionRequest/ActionResponse messages carry
no structured ``ok: bool``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .redact import (
    ARTIFACT_BYTE_CAP,
    MESSAGE_BYTE_CAP,
    PER_ITEM_TEXT_CAP,
    PER_KIND_ITEM_CAP,
    cap_by_bytes,
    cap_payload_by_bytes,
    public_project,
    redact_arguments,
    scrub_text,
)
from .run_progress import resolve_run

__all__ = ("RunFindingsInput", "run_findings")

_UNSUCCESSFUL_TERMINAL = frozenset({"failed", "timed_out", "aborted", "cancelled"})


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunFindingsInput(_StrictModel):
    run: str = Field(min_length=1, max_length=200)
    agent_filter: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Agent name, branch name, or branch/operation id substring, case-insensitive."
        ),
    )
    kind: Literal["messages", "tool_calls", "errors", "artifacts"] | None = None


def _message_text(content: Any) -> str:
    if isinstance(content, dict):
        for key in ("assistant_response", "instruction", "guidance", "system_message"):
            value = content.get(key)
            if isinstance(value, str) and value:
                return value
        function = content.get("function")
        if function:
            args = redact_arguments(content.get("arguments") or {})
            return f"{function}({args})"
        output = content.get("output")
        if isinstance(output, str) and output:
            return output
        return str(redact_arguments(content))
    if content is None:
        return ""
    return str(content)


def _branch_label(branch: dict[str, Any]) -> tuple[str | None, str | None]:
    return branch.get("name"), branch.get("agent_name")


def _matches_agent_filter(branch: dict[str, Any], needle: str) -> bool:
    name, agent_name = _branch_label(branch)
    branch_id = branch.get("id")
    return (
        needle in (agent_name or "").lower()
        or needle in (name or "").lower()
        or (isinstance(branch_id, str) and needle in branch_id.lower())
    )


def _message_totals(branches: list[dict[str, Any]]) -> tuple[int, int]:
    """``(loaded, total)`` messages across ``branches``.

    ``total`` is the session's own full-progression count per branch, which is
    what makes an honest window flag possible: the carrier is called with
    ``message_limit=PER_KIND_ITEM_CAP``, so ``branch["messages"]`` is already a
    tail window and its length says nothing about what exists. A branch that
    predates ``message_total`` reports its window length, which makes the
    comparison say "nothing dropped" rather than inventing a number.
    """
    loaded = total = 0
    for branch in branches:
        window = len(branch.get("messages") or [])
        loaded += window
        reported = branch.get("message_total")
        total += reported if isinstance(reported, int) and reported >= window else window
    return loaded, total


def _collect_messages(branches: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for branch in branches:
        name, agent_name = _branch_label(branch)
        for message in (branch.get("messages") or [])[-PER_KIND_ITEM_CAP:]:
            text = scrub_text(_message_text(message.get("content")))[:PER_ITEM_TEXT_CAP]
            items.append(
                {
                    "branch": name,
                    "agentName": agent_name,
                    "role": message.get("role"),
                    "lionClass": message.get("lion_class"),
                    "content": text,
                    "timestamp": message.get("timestamp"),
                }
            )
    kept, byte_truncated = cap_by_bytes(items, MESSAGE_BYTE_CAP)
    loaded, total = _message_totals(branches)
    # `truncated` used to report only the byte cap. That is the cap that almost
    # never fires here -- fifty messages against a two-megabyte budget -- while
    # the window above it fires constantly, so the flag read False on responses
    # that had dropped most of the run. Report both, and carry the counts: for
    # a reader deciding whether it has enough to answer from, "the last 50 of
    # 48,123" and "truncated" are not the same information.
    return {
        "items": kept,
        "truncated": byte_truncated or len(kept) < total,
        "returned": len(kept),
        "total": total,
    }


def _short_class(lion_class: str) -> str:
    from lionagi.studio.services.sessions import _short_lion_class

    return _short_lion_class(lion_class or "")


def _derive_tool_calls(branches: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """``(calls, clipped)``, where ``clipped`` means calls were dropped here.

    Two things drop tool calls, and they compose: the message window this runs
    over (a call in a message that was never loaded cannot be derived) and the
    per-branch cap below. Neither has a knowable denominator -- counting the
    calls in messages we do not have is not something this can do -- so this
    reports whether, not how many, and the caller says so rather than
    publishing a total it would have to invent.
    """
    from lionagi.studio.services.runs import _detect_status

    clipped = False
    out: list[dict[str, Any]] = []
    for branch in branches:
        name, agent_name = _branch_label(branch)
        messages = branch.get("messages") or []
        responses = {
            message["id"]: message
            for message in messages
            if _short_class(message.get("lion_class") or "") == "ActionResponse"
        }
        calls = [
            message
            for message in messages
            if _short_class(message.get("lion_class") or "") == "ActionRequest"
        ]
        clipped = clipped or len(calls) > PER_KIND_ITEM_CAP
        for message in calls[-PER_KIND_ITEM_CAP:]:
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            function = content.get("function") or ""
            arguments = redact_arguments(content.get("arguments") or {})
            response_id = content.get("action_response_id")
            response = responses.get(response_id) if response_id else None
            response_content = (
                response.get("content")
                if response and isinstance(response.get("content"), dict)
                else {}
            )
            output_text = str(response_content.get("output", "")) if response_content else ""
            if response is None:
                outcome, exit_code = "pending", None
            else:
                raw_status, exit_code = _detect_status(output_text, function)
                outcome = "success" if raw_status == "ok" else "error"
            out.append(
                {
                    "branch": name,
                    "agentName": agent_name,
                    "function": function,
                    "arguments": arguments,
                    "outcome": outcome,
                    "exitCode": exit_code,
                    "outputPreview": scrub_text(output_text)[:400] if output_text else "",
                    "timestamp": message.get("timestamp"),
                }
            )
    return out, clipped


def _collect_tool_calls(tool_calls: list[dict[str, Any]], clipped: bool) -> dict[str, Any]:
    kept, byte_truncated = cap_by_bytes(tool_calls, MESSAGE_BYTE_CAP)
    return {
        "items": kept,
        "truncated": byte_truncated or clipped or len(kept) < len(tool_calls),
        "returned": len(kept),
    }


def _collect_errors(
    session: dict[str, Any],
    branches: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    partial: bool,
) -> dict[str, Any]:
    """``partial`` is whether the evidence underneath was already incomplete.

    It matters most for this section. Branch and session status rows are read
    whole, so an errors list can look authoritative while the tool-call half of
    it was derived from a fraction of the messages -- which is the reading that
    turns "no errors found" into "no errors happened".
    """
    items: list[dict[str, Any]] = [
        {
            "branch": call["branch"],
            "agentName": call["agentName"],
            "kind": "tool_call",
            "function": call["function"],
            "outputPreview": call["outputPreview"],
            "timestamp": call["timestamp"],
        }
        for call in tool_calls
        if call["outcome"] == "error"
    ]
    for branch in branches:
        status = branch.get("status")
        if status in _UNSUCCESSFUL_TERMINAL:
            name, agent_name = _branch_label(branch)
            items.append(
                {
                    "branch": name,
                    "agentName": agent_name,
                    "kind": "branch_status",
                    "status": status,
                    "timestamp": branch.get("ended_at"),
                }
            )
    if session.get("status") in _UNSUCCESSFUL_TERMINAL and session.get("status_reason_code"):
        items.append(
            {
                "branch": None,
                "agentName": None,
                "kind": "session_status",
                "status": session.get("status"),
                "statusReasonCode": session.get("status_reason_code"),
                "message": scrub_text(session.get("status_reason_summary") or ""),
                "timestamp": session.get("ended_at"),
            }
        )
    kept, byte_truncated = cap_by_bytes(items, MESSAGE_BYTE_CAP)
    result = {
        "items": kept,
        "truncated": byte_truncated or partial or len(kept) < len(items),
        "returned": len(kept),
        "evidenceComplete": not partial,
    }
    if not kept and result["truncated"]:
        # An empty list under a bare truncated flag is unreadable: it cannot
        # say whether zero errors happened or every error was dropped. Say
        # which. Nothing was dropped HERE when kept == items -- the flag is
        # carrying the incomplete evidence window underneath.
        result["note"] = (
            "no errors among the loaded evidence; the message window did not "
            "cover the whole run, so absence here does not certify the run"
            if partial and not byte_truncated
            else "errors existed but exceeded the response byte budget"
        )
    return result


def _collect_artifacts(session: dict[str, Any]) -> dict[str, Any]:
    contract = session.get("artifact_contract_json")
    verification = session.get("artifact_verification_json")
    artifacts_path = session.get("artifacts_path")
    redacted_contract = redact_arguments(contract) if contract else None
    redacted_verification = redact_arguments(verification) if verification else None
    # Redaction alone is not a bound: an oversized (or attacker-controlled)
    # contract/verification payload must not make the response exceed the
    # same aggregate cap every other findings section honors.
    capped_contract, contract_truncated = cap_payload_by_bytes(redacted_contract, ARTIFACT_BYTE_CAP)
    capped_verification, verification_truncated = cap_payload_by_bytes(
        redacted_verification, ARTIFACT_BYTE_CAP
    )
    return {
        "contract": capped_contract,
        "contractTruncated": contract_truncated,
        "verification": capped_verification,
        "verificationTruncated": verification_truncated,
        "artifactsPath": public_project(artifacts_path) if artifacts_path else None,
    }


async def run_findings(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RunFindingsInput.model_validate(arguments)
    resolution = await resolve_run(args.run)
    if not resolution["found"]:
        return {"found": False, "reason": resolution.get("reason")}
    if resolution.get("ambiguous"):
        return {
            "found": True,
            "ambiguous": True,
            "candidates": resolution["candidates"],
            "truncated": resolution.get("truncated", False),
        }

    from lionagi.studio.services.sessions import get_session

    # Exact lifetime counts are opt-in on this read because the listings cannot
    # afford them per row. This tool is single-session and already reports what
    # its window left out, and that claim is only honest against a real total:
    # without it every windowed run reads as complete.
    session = await get_session(
        resolution["session_id"], message_limit=PER_KIND_ITEM_CAP, include_stats=True
    )
    if session is None:
        return {"found": False, "reason": "the resolved run vanished before it could be read"}

    branches = session.get("branches") or []
    if args.agent_filter:
        needle = args.agent_filter.lower()
        branches = [branch for branch in branches if _matches_agent_filter(branch, needle)]

    result: dict[str, Any] = {
        "found": True,
        "ambiguous": False,
        "id": session.get("id"),
        "status": session.get("status"),
    }

    wanted = {args.kind} if args.kind else {"messages", "tool_calls", "errors", "artifacts"}

    tool_calls: list[dict[str, Any]] | None = None
    calls_clipped = False
    if "tool_calls" in wanted or "errors" in wanted:
        tool_calls, calls_clipped = _derive_tool_calls(branches)

    # The message window bounds every section derived from messages, not just
    # the messages section, so it is computed once here and passed down.
    loaded, total = _message_totals(branches)
    window_dropped = loaded < total

    if "messages" in wanted:
        result["messages"] = _collect_messages(branches)
    if "tool_calls" in wanted:
        result["toolCalls"] = _collect_tool_calls(tool_calls or [], calls_clipped or window_dropped)
    if "errors" in wanted:
        result["errors"] = _collect_errors(
            session, branches, tool_calls or [], calls_clipped or window_dropped
        )
    if "artifacts" in wanted:
        result["artifacts"] = _collect_artifacts(session)

    return result

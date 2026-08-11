# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict stdio MCP bridge for real Studio Operator application tools.

The bridge exposes a deliberately small capability set. Read tools return
bounded projections, UI tools only append durable ADR-0083 effects, and the
single launch tool creates a durable proposal then waits for the daemon's
authenticated human-decision path. It never accepts a URL, filesystem path,
shell command, or raw endpoint.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .cancel_run import CANCEL_RUN_DESCRIPTION, CancelRunInput, cancel_run
from .redact import (
    MESSAGE_BYTE_CAP,
    PER_ITEM_TEXT_CAP,
    fold_field_name,
    is_secret_field_name,
    scrub_text,
)
from .rename_session import RENAME_SESSION_DESCRIPTION, RenameSessionInput, rename_session
from .resume_run import RESUME_RUN_DESCRIPTION, ResumeRunInput, resume_run
from .run_detail import RunDetailInput, run_detail
from .run_findings import RunFindingsInput, run_findings
from .run_progress import RunProgressInput, run_progress
from .store import OperatorStore

OperatorSpace = Literal[
    "mission",
    "designer",
    "library",
    "history",
    "schedules",
    "system",
]


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RecentRunsInput(_StrictInput):
    limit: int = Field(default=10, ge=1, le=20)
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None


class ListSchedulesInput(_StrictInput):
    limit: int = Field(default=20, ge=1, le=50)
    enabled: bool | None = None


class ListAgentsInput(_StrictInput):
    limit: int = Field(default=50, ge=1, le=200)


class ListPlaybooksInput(_StrictInput):
    limit: int = Field(default=50, ge=1, le=200)


class RunStatsInput(_StrictInput):
    window: Literal["24h", "7d"] = "24h"


class CurrentViewInput(_StrictInput):
    """No arguments: the view is a property of the turn, not of the request."""


class ListSessionsInput(_StrictInput):
    playbook: str | None = None
    status: str | list[str] | None = None
    project: str | None = None
    search: str | None = None
    limit: int = Field(default=20, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    sort: Literal["recent", "cost"] = "recent"


class SessionDetailInput(_StrictInput):
    session_id: str = Field(min_length=1, max_length=200)
    message_limit: int = Field(default=200, ge=1, le=1_000)
    message_cursor: str | None = None


class SessionSignalsInput(_StrictInput):
    session_id: str = Field(min_length=1, max_length=200)
    after_seq: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class GetInvocationInput(_StrictInput):
    invocation_id: str = Field(min_length=1, max_length=200)


class ListArtifactsInput(_StrictInput):
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=50, ge=1, le=200)


class GetArtifactInput(_StrictInput):
    artifact_id: str = Field(min_length=1, max_length=200)


class NavigateInput(_StrictInput):
    space: OperatorSpace = Field(
        description=(
            "Studio space to open. Use 'history' for the first-class Fleet/run-history view."
        )
    )
    status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None


class PrefillScheduleInput(_StrictInput):
    name: str = Field(min_length=1, max_length=160)
    cron: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=32_768)
    description: str = Field(default="", max_length=1_000)

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        from lionagi.studio.services.schedules import _svc_validate_cron_expr

        _svc_validate_cron_expr(value, required=True)
        return value


class LaunchPlaybookInput(_StrictInput):
    playbook: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    note: str = Field(default="", max_length=500)


class LaunchAgentInput(_StrictInput):
    # The launch service accepts a launch with action_kind == "agent" and no
    # action_agent at all -- it only rejects a leading '-' when the field is
    # present. Requiring `agent` here, not there, is what actually closes
    # that hole for the Operator's one entry point into that code path.
    agent: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    prompt: str = Field(min_length=1, max_length=32_768)
    note: str = Field(default="", max_length=500)

    # No `model` field: model routing is a per-role decision made in the agent
    # profile, and letting a caller override it here would silently defeat
    # that routing. action_model stays unset so the profile's own choice
    # applies, exactly as if the field were never in the launch payload.


class _NavigateEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["navigate"] = "navigate"
    space: OperatorSpace
    params: dict[str, str]


class _PrefillEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["prefill"] = "prefill"
    form: Literal["schedule"] = "schedule"
    values: dict[str, str]


_TOOL_MODELS: dict[str, type[BaseModel]] = {
    "list_recent_runs": RecentRunsInput,
    "run_stats": RunStatsInput,
    "get_current_view": CurrentViewInput,
    "list_schedules": ListSchedulesInput,
    "list_agents": ListAgentsInput,
    "list_playbooks": ListPlaybooksInput,
    "navigate": NavigateInput,
    "prefill_schedule": PrefillScheduleInput,
    "launch_playbook": LaunchPlaybookInput,
    "launch_agent": LaunchAgentInput,
    "run_progress": RunProgressInput,
    "run_findings": RunFindingsInput,
    "run_detail": RunDetailInput,
    "cancel_run": CancelRunInput,
    "resume_run": ResumeRunInput,
    "rename_session": RenameSessionInput,
    "list_sessions": ListSessionsInput,
    "session_detail": SessionDetailInput,
    "session_signals": SessionSignalsInput,
    "get_invocation": GetInvocationInput,
    "list_artifacts": ListArtifactsInput,
    "get_artifact": GetArtifactInput,
}

_TOOL_DESCRIPTIONS = {
    "list_recent_runs": (
        "List at most 20 recent Studio runs as a redacted read-only projection. "
        "Each entry carries 'kind' (agent, play, flow, fanout, or show-play) and "
        "'playbookName' when set -- a run may be a play root coordinating other "
        "runs, and 'agentName' alone never establishes that a run is a single "
        "agent. Read 'kind' before characterizing a run. For 'how is this play "
        "going', use run_progress instead."
    ),
    "run_stats": (
        "Count runs over a whole window (24h or 7d) with per-status totals and "
        "completion rate. Use this for 'how many runs did I have', which "
        "list_recent_runs cannot answer because it only returns the newest 20."
    ),
    "get_current_view": (
        "Read the Studio view the human is on: space, route, selection and "
        "filters. Read-only. The 'source' field says how fresh the answer is: "
        "'live' means the browser reported this view after the instruction was "
        "sent, so it is where they are now; 'turn' means nothing newer has been "
        "reported, so it is where they were when they sent the instruction and "
        "they may have moved since."
    ),
    "list_schedules": (
        "List Studio schedules as a redacted read-only projection: trigger, "
        "next fire time, and recent health."
    ),
    "list_agents": ("List the agent profiles in the library, names and models only."),
    "list_playbooks": (
        "List playbook names available to launch_playbook. Call this before "
        "proposing a launch: launch_playbook needs an exact existing name."
    ),
    "navigate": (
        "Request a typed Studio navigation effect. The browser applies and "
        "acknowledges it; this tool does not claim that navigation completed. "
        "The canonical 'history' space opens Fleet/run history."
    ),
    "prefill_schedule": (
        "Request a typed schedule-form prefill for human review. This never creates a schedule."
    ),
    "launch_playbook": (
        "Propose launching one named Studio playbook. This blocks until the "
        "human explicitly allows or denies the exact durable proposal."
    ),
    "launch_agent": (
        "Propose launching one agent profile with a prompt. This blocks "
        "until the human explicitly allows or denies the exact durable "
        "proposal. Call list_agents first: this needs an exact existing "
        "agent name."
    ),
    "run_progress": (
        "Report how one run is going: status, op totals split into "
        "completed/running/failed/pending (they always sum to the total), "
        "which ops are running right now, elapsed time, and whether it has a "
        "graph. Accepts a run id, an id prefix, a name or playbook substring "
        "(minimum 3 characters), or 'current' for the run the human is "
        "looking at. An ambiguous reference returns candidates instead of "
        "guessing. Every number is a direct database read taken when this "
        "tool is called, not a live process feed -- the returned "
        "'freshness' field says so, the same honesty get_current_view uses "
        "for its own 'source' field."
    ),
    "run_findings": (
        "Report what one run produced: message tails, tool calls with their "
        "inferred outcomes, errors, and declared artifacts, bounded and "
        "redacted the same way the other read tools are. Filter by "
        "agent/branch name substring or a single 'kind' (messages, "
        "tool_calls, errors, artifacts) to narrow the response. Same "
        "reference vocabulary and ambiguity handling as run_progress. "
        "Tool-call outcomes are inferred from response content, not read "
        "from a structured status field the run itself stores, so treat "
        "them as a best-effort read; any section too large to return in "
        "full is capped and says so via its own 'truncated' flag."
    ),
    "run_detail": (
        "Report the full projection of one run: identity, playbook/agent "
        "and model/provider fields, timing, health, cost and token totals, "
        "and other detail fields as stored, bounded and redacted the same "
        "way the other read tools are. Takes a bare run id (no id-prefix or "
        "name-substring resolution). Returns 'known': false with a "
        "'source' of 'unavailable' if the store could not be read, or "
        "'store' if the store was read but no run matched."
    ),
    "cancel_run": CANCEL_RUN_DESCRIPTION,
    "resume_run": RESUME_RUN_DESCRIPTION,
    "rename_session": RENAME_SESSION_DESCRIPTION,
    "list_sessions": (
        "List a filtered page of Studio sessions. The result is capped at 500 rows, "
        "reports the matching total and whether more rows exist, and labels the store source."
    ),
    "session_detail": (
        "Read one Studio session and its bounded per-branch message windows. Content is "
        "redacted, storage paths and internal state are omitted, and continuation cursors "
        "and truncation flags come from the session store."
    ),
    "session_signals": (
        "Read at most 500 ordered signals after a sequence number. Signal payloads are "
        "redacted and capped at 100000 bytes, with row and payload truncation reported."
    ),
    "get_invocation": (
        "Read one invocation with at most 50 child sessions and 50 artifacts. Artifact "
        "content is redacted and capped at 2000000 bytes, and every truncation is reported."
    ),
    "list_artifacts": (
        "List at most 200 artifact metadata records for one session or invocation. "
        "Bodies and storage paths are omitted and row truncation is reported."
    ),
    "get_artifact": (
        "Read one artifact with redacted content capped at 2000000 bytes. Storage paths "
        "are omitted and content truncation is reported."
    ),
}

_TOOL_SCHEMAS = [
    {
        "name": name,
        "description": _TOOL_DESCRIPTIONS[name],
        "inputSchema": model.model_json_schema(),
    }
    for name, model in _TOOL_MODELS.items()
]


def _identity() -> tuple[OperatorStore, str, str]:
    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return OperatorStore(db_path), conversation_id, request_id


def public_project(value: Any) -> str | None:
    """Reduce a project to a leaf name so no filesystem layout is disclosed."""
    if not isinstance(value, str) or not value:
        return None
    if Path(value).is_absolute():
        return Path(value).name or "external-project"
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return windows_path.name or "external-project"
    return value[:160]


_SIGNAL_PAYLOAD_BYTE_CAP = 100_000
_ARTIFACT_CONTENT_BYTE_CAP = 2_000_000
_SESSION_BRANCH_CAP = 500
_SESSION_LIST_DROP = frozenset(
    {
        "node_metadata",
        "artifacts_path",
        "artifact_contract_json",
        "artifact_verification_json",
        "project_source",
        "branch_count",
        "tags",
    }
)
_SESSION_DETAIL_DROP = frozenset(
    {
        "node_metadata",
        "artifacts_path",
        "artifact_contract_json",
        "artifact_verification_json",
        "graph",
        "segments",
        "status_evidence_refs",
        "project_source",
        "source_show",
    }
)
_INVOCATION_DROP = frozenset({"node_metadata", "status_evidence_refs"})
_ARTIFACT_DROP = frozenset({"file_path"})
_URL_FIELD_NAMES = frozenset(
    {
        "url",
        "uri",
        "dsn",
        "store_url",
        "database_url",
        "db_url",
        "connection_url",
        # Compared after folding, so store-url and store.url are covered by the
        # underscored spellings above. Concatenated spellings are not: they
        # carry no separator to fold, and this is an exact-match set, so
        # storeUrl has to be listed in its own right.
        "storeurl",
        "databaseurl",
        "dburl",
        "connectionurl",
    }
)
_PATH_FIELD_NAMES = frozenset({"path", "directory", "cwd", "root"})
_STORE_URL_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|sqlite|redis|mongodb(?:\+srv)?|file)"
    r"(?:\+[a-z0-9_]+)?://[^\s\"']+"
)
_CREDENTIALED_URL_RE = re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^@\s/]+@[^\s\"']+")
_ROOT_POSIX_PATH_RE = re.compile(
    r"(?<![\w/])/(?:Users|home|tmp|var|opt|etc|private|srv|mnt|Volumes)(?:/[^\s\"']+)*"
)
_UNC_PATH_RE = re.compile(r"(?<!\\)(\\\\[^\\\s]+\\[^\\\s]+(?:\\[^\\\s]+)*)")


def _safe_text(value: str) -> str:
    value = _STORE_URL_RE.sub("[redacted-url]", value)
    value = _CREDENTIALED_URL_RE.sub("[redacted-url]", value)
    value = _ROOT_POSIX_PATH_RE.sub(
        lambda match: match.group(0).rsplit("/", 1)[-1] or "[redacted-path]",
        value,
    )
    value = _UNC_PATH_RE.sub(
        lambda match: match.group(0).rsplit("\\", 1)[-1] or "[redacted-path]",
        value,
    )
    return scrub_text(value)


def _secret_field(key: str) -> bool:
    return is_secret_field_name(key)


def _safe_content(value: Any, *, key: str = "") -> Any:
    """Recursively remove credentials, store URLs, and host filesystem layouts."""
    if fold_field_name(key) in _URL_FIELD_NAMES or _secret_field(key):
        # Judge by field name only where the value could carry text. A number
        # is neither a credential nor a URL, and several counters we report
        # carry a marker in their name — input_tokens, output_tokens — so
        # redacting on the name alone deletes usage data to protect nothing.
        # None stays redacted: that costs no information and keeps the
        # null-handling of secret fields unchanged.
        if not isinstance(value, (int, float, bool)):
            return "[redacted]"
    if fold_field_name(key) in _PATH_FIELD_NAMES and isinstance(value, str):
        public_value = public_project(value)
        return _safe_text(public_value) if public_value is not None else None
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, item in value.items():
            safe_key = _safe_text(str(raw_key))
            if safe_key in _ARTIFACT_DROP or safe_key in _SESSION_DETAIL_DROP:
                continue
            projected[safe_key] = _safe_content(item, key=safe_key)
        return projected
    if isinstance(value, list):
        return [_safe_content(item) for item in value]
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"[{type(value).__name__}]"


def _safe_mapping(value: dict[str, Any], *, drop: frozenset[str]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key, item in value.items():
        if key in drop:
            continue
        if key == "project":
            public_value = public_project(item)
            projected[key] = _safe_text(public_value) if public_value is not None else None
        else:
            projected[key] = _safe_content(item, key=key)
    return projected


def _cap_payload(value: Any, limit: int) -> tuple[Any, bool]:
    encoded = json.dumps(value, separators=(",", ":"), default=str).encode()
    if len(encoded) <= limit:
        return value, False
    if isinstance(value, str):
        low, high = 0, len(value)
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate = json.dumps(value[:midpoint], separators=(",", ":")).encode()
            if len(candidate) <= limit:
                low = midpoint
            else:
                high = midpoint - 1
        return value[:low], True
    return {"truncated": True, "reason": "content exceeds the byte cap"}, True


def _cap_strings(value: Any, *, skip_keys: frozenset[str] = frozenset()) -> bool:
    truncated = False
    if isinstance(value, dict):
        for key, item in value.items():
            if key in skip_keys:
                continue
            if isinstance(item, str):
                value[key], item_truncated = _cap_payload(item, PER_ITEM_TEXT_CAP)
                truncated = truncated or item_truncated
            else:
                truncated = _cap_strings(item, skip_keys=skip_keys) or truncated
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index], item_truncated = _cap_payload(item, PER_ITEM_TEXT_CAP)
                truncated = truncated or item_truncated
            else:
                truncated = _cap_strings(item, skip_keys=skip_keys) or truncated
    return truncated


def _safe_artifact(row: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    if not include_content:
        projected = _safe_mapping(
            {key: value for key, value in row.items() if key != "content"},
            drop=_ARTIFACT_DROP,
        )
        projected["metadata_truncated"] = _cap_strings(projected)
        return projected
    projected = _safe_mapping(row, drop=_ARTIFACT_DROP)
    metadata_truncated = _cap_strings(projected, skip_keys=frozenset({"content"}))
    content, truncated = _cap_payload(projected.get("content"), _ARTIFACT_CONTENT_BYTE_CAP)
    projected["content"] = content
    projected["content_truncated"] = truncated
    projected["metadata_truncated"] = metadata_truncated
    return projected


def _bound_session_messages(session: dict[str, Any]) -> bool:
    remaining = MESSAGE_BYTE_CAP
    truncated = False
    branches = session.get("branches")
    if not isinstance(branches, list):
        return False
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        messages = branch.get("messages")
        if not isinstance(messages, list):
            continue
        if remaining < 2:
            if messages:
                truncated = True
                branch["messages_truncated"] = True
            branch["messages"] = []
            continue
        remaining -= 2
        kept_reversed = []
        for message in reversed(messages):
            size = (
                len(
                    json.dumps(
                        message,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                )
                + 1
            )
            if size > remaining:
                truncated = True
                continue
            kept_reversed.append(message)
            remaining -= size
        kept_reversed.reverse()
        if len(kept_reversed) != len(messages):
            branch["messages_truncated"] = True
        branch["messages"] = kept_reversed
    return truncated


def _bound_session_structure(session: dict[str, Any]) -> tuple[bool, bool]:
    branches_truncated = False
    branches = session.get("branches")
    if isinstance(branches, list) and len(branches) > _SESSION_BRANCH_CAP:
        session["branches"] = branches[:_SESSION_BRANCH_CAP]
        branches_truncated = True
    stats_truncated = False
    stats = session.get("message_stats")
    if isinstance(stats, dict):
        for field in ("branches", "errors", "files"):
            value = stats.pop(field, None)
            stats_truncated = stats_truncated or bool(value)
    return branches_truncated, stats_truncated


async def list_sessions(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListSessionsInput.model_validate(arguments)
    from lionagi.studio.services import runs as runs_service
    from lionagi.studio.services import sessions as sessions_service
    from lionagi.studio.services._db import store_exists

    # The carrier answers an absent store with an empty list, so without this
    # check a store that cannot be read is indistinguishable from a store with
    # nothing in it. Those are different answers and a caller acts differently
    # on each: "no runs" invites a conclusion, "I could not look" does not.
    if not store_exists():
        return {"known": False, "source": "unavailable"}

    if isinstance(args.status, str):
        status_filter: str | list[str] | None = args.status
    elif args.status:
        status_filter = [str(status) for status in args.status]
    else:
        status_filter = None
    statuses = runs_service._normalize_status_filter(status_filter)
    where = sessions_service.SessionFilter(
        playbook=args.playbook,
        statuses=statuses,
        project=args.project,
        search=args.search,
    )
    rows = await runs_service.list_runs(
        playbook=args.playbook,
        status=status_filter,
        project=args.project,
        search=args.search,
        limit=args.limit,
        offset=args.offset,
        sort=args.sort,
    )
    total = await sessions_service.count_sessions(where)
    projected = []
    content_truncated = False
    for row in rows[: args.limit]:
        safe = _safe_mapping(row, drop=_SESSION_LIST_DROP)
        row_truncated = _cap_strings(safe)
        safe["content_truncated"] = row_truncated
        content_truncated = content_truncated or row_truncated
        projected.append(safe)
    return {
        "known": True,
        "sessions": projected,
        "total": total,
        "limit": args.limit,
        "offset": args.offset,
        "truncated": args.offset + len(projected) < total,
        "content_truncated": content_truncated,
        "source": "store",
    }


async def session_detail(arguments: dict[str, Any]) -> dict[str, Any]:
    args = SessionDetailInput.model_validate(arguments)
    from lionagi.studio.services import sessions as sessions_service

    source = "store"
    try:
        row = await sessions_service.get_session(
            args.session_id,
            message_limit=args.message_limit,
            message_offset=0,
            message_cursor=args.message_cursor,
        )
    except sessions_service.MessageCursorError:
        if args.message_cursor is None:
            raise
        row = await sessions_service.get_session(
            args.session_id,
            message_limit=args.message_limit,
            message_offset=0,
            message_cursor=None,
        )
        source = "fallback"
    if row is None:
        return {"known": False, "source": "store"}
    projected = _safe_mapping(row, drop=_SESSION_DETAIL_DROP)
    branches_truncated, message_stats_truncated = _bound_session_structure(projected)
    messages_bytes_truncated = _bound_session_messages(projected)
    content_truncated = _cap_strings(
        projected,
        skip_keys=frozenset({"message_cursor", "message_next_cursor"}),
    )
    return {
        "known": True,
        "source": source,
        "message_byte_limit": MESSAGE_BYTE_CAP,
        "messages_bytes_truncated": messages_bytes_truncated,
        "branches_truncated": branches_truncated,
        "message_stats_truncated": message_stats_truncated,
        "content_truncated": content_truncated,
        **projected,
    }


async def session_signals(arguments: dict[str, Any]) -> dict[str, Any]:
    args = SessionSignalsInput.model_validate(arguments)
    from lionagi.studio.services import signals as signals_service
    from lionagi.studio.services._db import store_exists

    # Same reason as list_sessions: the carrier returns an empty page both for a
    # session with no signals and for a store it could not open.
    if not store_exists():
        return {"known": False, "source": "unavailable"}

    rows = await signals_service.get_signals_after(
        args.session_id,
        args.after_seq or 0,
        limit=args.limit + 1,
    )
    projected = []
    for row in rows[: args.limit]:
        safe = _safe_mapping(row, drop=frozenset())
        metadata_truncated = _cap_strings(safe, skip_keys=frozenset({"payload"}))
        payload, truncated = _cap_payload(safe.get("payload"), _SIGNAL_PAYLOAD_BYTE_CAP)
        safe["payload"] = payload
        safe["payload_truncated"] = truncated
        safe["metadata_truncated"] = metadata_truncated
        projected.append(safe)
    return {
        "known": True,
        "signals": projected,
        "limit": args.limit,
        "truncated": len(rows) > args.limit,
        "source": "store",
    }


async def get_invocation(arguments: dict[str, Any]) -> dict[str, Any]:
    args = GetInvocationInput.model_validate(arguments)
    from lionagi.state.db import read_only_open_supported
    from lionagi.studio.services import invocations as invocations_service

    # This tool only reads, and an ordinary open applies schema on the way in —
    # taking a write lock and possibly issuing one-time migration statements to
    # serve a read. Ask for read-only where the store can give it. Unlike
    # _artifact_rows, which refuses the read outright when read-only is
    # unavailable, this degrades to the ordinary open: the tool's guarantee to
    # its caller is the redaction layer, not the connection mode, so losing
    # read-only on a server-backed store costs hygiene rather than safety.
    row = await invocations_service.get_invocation(
        args.invocation_id, readonly=read_only_open_supported()
    )
    if row is None:
        return {"known": False}
    raw_sessions = row.get("sessions")
    sessions: list[Any] = raw_sessions if isinstance(raw_sessions, list) else []
    raw_artifacts = row.get("artifacts")
    artifacts: list[Any] = raw_artifacts if isinstance(raw_artifacts, list) else []
    projected = _safe_mapping(
        {key: value for key, value in row.items() if key not in {"sessions", "artifacts"}},
        drop=_INVOCATION_DROP,
    )
    content_truncated = _cap_strings(projected)
    projected["sessions"] = [
        _safe_mapping(item, drop=frozenset()) for item in sessions[:50] if isinstance(item, dict)
    ]
    for session in projected["sessions"]:
        content_truncated = _cap_strings(session) or content_truncated
    projected["artifacts"] = [
        _safe_artifact(item, include_content=True)
        for item in artifacts[:50]
        if isinstance(item, dict)
    ]
    projected.update(
        {
            "known": True,
            "source": "store",
            "sessions_truncated": len(sessions) > 50,
            "artifacts_truncated": len(artifacts) > 50,
            "content_truncated": content_truncated,
        }
    )
    return projected


async def _artifact_rows(
    *, session_id: str | None, invocation_id: str | None
) -> list[dict[str, Any]] | None:
    """Artifact rows for one owner, or None when the store cannot be read safely.

    ``read_only_open_supported()`` is for callers wanting read-only as an
    optimisation: on the stores it reports False for, it hands back a *writable*
    connection. These tools are read-only by contract, so passing it would ask
    for a guarantee and silently accept its opposite. An unavailable read-only
    open is therefore reported as an unavailable read, never widened into a
    writable one.
    """
    from lionagi.state.db import StateDB, read_only_open_supported, state_db_known_absent
    from lionagi.studio.services.invocations import _serialize_artifact

    if state_db_known_absent() or not read_only_open_supported():
        return None
    async with StateDB(readonly=True) as db:
        if session_id is not None:
            rows = await db.list_artifacts_for_session(session_id)
        else:
            rows = await db.list_artifacts_for_invocation(invocation_id or "")
    return [_serialize_artifact(row) for row in rows]


async def list_artifacts(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListArtifactsInput.model_validate(arguments)
    if (args.session_id is None) == (args.invocation_id is None):
        raise ValueError("Provide exactly one of session_id or invocation_id")
    rows = await _artifact_rows(session_id=args.session_id, invocation_id=args.invocation_id)
    if rows is None:
        return {"known": False, "source": "unavailable"}
    return {
        "known": True,
        "artifacts": [
            _safe_artifact(row, include_content=False)
            for row in rows[: args.limit]
            if isinstance(row, dict)
        ],
        "limit": args.limit,
        "truncated": len(rows) > args.limit,
        "source": "store",
    }


async def get_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
    args = GetArtifactInput.model_validate(arguments)
    from lionagi.state.db import read_only_open_supported
    from lionagi.studio.services import invocations as invocations_service

    # Read-only where the store supports it, for the reason given in
    # get_invocation above.
    row = await invocations_service.get_artifact(
        args.artifact_id, readonly=read_only_open_supported()
    )
    if row is None:
        return {"known": False}
    return {"known": True, "source": "store", **_safe_artifact(row, include_content=True)}


async def list_recent_runs(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RecentRunsInput.model_validate(arguments)
    from lionagi.studio.services.runs import list_runs

    rows = await list_runs(status=args.status, limit=args.limit, offset=0)

    projected = [
        {
            "id": row.get("id"),
            "agentName": row.get("agent_name"),
            "status": row.get("status"),
            "project": public_project(row.get("project")),
            "startedAt": row.get("started_at"),
            "endedAt": row.get("ended_at"),
            "href": f"/runs/{row.get('id')}",
            "kind": row.get("invocation_kind"),
            "playbookName": row.get("playbook_name"),
        }
        for row in rows[: args.limit]
        if isinstance(row.get("id"), str)
    ]
    return {"runs": projected, "count": len(projected), "bounded": True}


async def run_stats(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RunStatsInput.model_validate(arguments)
    from lionagi.studio.services.stats import get_activity_stats

    stats = await get_activity_stats(args.window)
    buckets = stats.get("buckets") or []
    totals = {key: 0 for key in ("completed", "failed", "cancelled", "running")}
    for bucket in buckets:
        for key in totals:
            value = bucket.get(key)
            if isinstance(value, int):
                totals[key] += value
    return {
        "window": stats.get("window"),
        "total": stats.get("total"),
        "byStatus": totals,
        "completionRate": stats.get("completion_rate"),
    }


async def get_current_view(arguments: dict[str, Any]) -> dict[str, Any]:
    CurrentViewInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()
    turn = await store.get_turn(request_id)
    context = turn.get("context")
    context = context if isinstance(context, dict) else None
    source = "turn"

    # The turn's context is frozen at submit, so it is only the freshest answer
    # until the human moves. Prefer a view the SAME PAGE observed later in its
    # own count of the views it has seen.
    #
    # Both halves of that are load-bearing. Server arrival order cannot stand in
    # for the count: a report the browser saw before the instruction can arrive
    # after it, and ordering by arrival would present a view from before the
    # question as the answer to it, labelled live. Nor can a wall clock, which
    # can step backwards and leave a stale view holding the higher number. And a
    # count from a different page cannot be compared at all: two tabs on one
    # conversation are looking at two different pages, they count
    # independently, and only the page the instruction came from can say where
    # the human is.
    #
    # When the turn names no observer or no count there is nothing to compare
    # against, so the honest answer is the turn's own snapshot rather than a
    # freshness claim that cannot be supported.
    turn_seq = (context or {}).get("observationSeq")
    turn_observer = (context or {}).get("observerId")
    if isinstance(turn_seq, int) and isinstance(turn_observer, str):
        reported, reported_seq = await store.get_view(conversation_id, turn_observer)
        if reported is not None and isinstance(reported_seq, int) and reported_seq > turn_seq:
            context, source = reported, "live"

    if context is None:
        return {"known": False}
    return {
        "known": True,
        "space": context.get("space"),
        "route": context.get("route"),
        "project": public_project(context.get("project")),
        "selection": context.get("selection"),
        "filters": context.get("filters"),
        # "turn" means nothing observed later than the instruction has been
        # reported, so the human may have moved since. "live" means this is
        # where they are.
        #
        # The observation count that decided this is deliberately NOT returned.
        # It counts what one page has seen and means nothing outside that page,
        # so a bare number here could only invite a comparison that is not
        # valid -- which is the defect this whole mechanism was built to remove.
        "source": source,
    }


async def list_schedules(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListSchedulesInput.model_validate(arguments)
    from lionagi.studio.services.schedules import list_schedules as _list_schedules

    rows = await _list_schedules(enabled=args.enabled)
    projected = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "enabled": bool(row.get("enabled")),
            "triggerType": row.get("trigger_type"),
            "cron": row.get("cron_expr"),
            "intervalSec": row.get("interval_sec"),
            "actionKind": row.get("action_kind"),
            "nextFireAt": row.get("next_fire_at"),
            "lastFiredAt": row.get("last_fired_at"),
            "lastStatus": row.get("last_status"),
            "consecutiveFailures": row.get("consecutive_failures"),
            "project": public_project(row.get("project")),
        }
        for row in rows[: args.limit]
        if isinstance(row.get("id"), str)
    ]
    return {"schedules": projected, "count": len(projected), "bounded": True}


async def list_agents(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListAgentsInput.model_validate(arguments)
    from lionagi.studio.services.agents import list_agents as _list_agents
    from lionagi.studio.services.redaction import demo_mode_enabled, project_agent_fields

    rows = await anyio.to_thread.run_sync(_list_agents)
    redact = demo_mode_enabled()
    projected = []
    for row in rows[: args.limit]:
        if not isinstance(row.get("name"), str):
            continue
        safe = project_agent_fields(row, redact=redact)
        projected.append(
            {
                "name": row.get("name"),
                "provider": safe.get("provider") or None,
                "model": safe.get("model") or None,
                "description": (safe.get("description") or "")[:500] or None,
            }
        )
    return {"agents": projected, "count": len(projected), "bounded": True}


async def list_playbooks(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ListPlaybooksInput.model_validate(arguments)
    from lionagi.studio.services.playbooks import list_playbooks as _list_playbooks

    rows = await anyio.to_thread.run_sync(_list_playbooks)
    projected = [
        {
            "name": row.get("name"),
            "description": (row.get("description") or "")[:500] or None,
        }
        for row in rows[: args.limit]
        if isinstance(row.get("name"), str)
    ]
    return {"playbooks": projected, "count": len(projected), "bounded": True}


async def navigate(arguments: dict[str, Any]) -> dict[str, Any]:
    args = NavigateInput.model_validate(arguments)
    if args.status is not None and args.space not in {"mission", "history"}:
        raise ValueError("status is only valid for mission/history run navigation")
    params = {"status": args.status} if args.status is not None else {}
    effect = _NavigateEffect(space=args.space, params=params).model_dump()
    store, conversation_id, request_id = _identity()
    frame = await store.append_effect(conversation_id, request_id, effect)
    if frame is None:
        raise RuntimeError("The Operator turn ended before the UI effect was persisted")
    stored = frame["payload"]["effect"]
    return {
        "status": "pending",
        "effectId": stored["id"],
        "kind": stored["kind"],
    }


async def prefill_schedule(arguments: dict[str, Any]) -> dict[str, Any]:
    args = PrefillScheduleInput.model_validate(arguments)
    effect = _PrefillEffect(
        values={
            "name": args.name,
            "cron": args.cron,
            "prompt": args.prompt,
            "description": args.description,
        }
    ).model_dump()
    store, conversation_id, request_id = _identity()
    frame = await store.append_effect(conversation_id, request_id, effect)
    if frame is None:
        raise RuntimeError("The Operator turn ended before the UI effect was persisted")
    stored = frame["payload"]["effect"]
    return {
        "status": "pending",
        "effectId": stored["id"],
        "kind": stored["kind"],
    }


def _redacted_launch_result(proposal: dict[str, Any]) -> dict[str, Any]:
    raw = proposal.get("result")
    result = raw if isinstance(raw, dict) else {}
    allowed = {
        key: result[key]
        for key in ("invocation_id", "run_id", "href", "action_kind")
        if isinstance(result.get(key), (str, int, float, bool))
    }
    return {
        "status": proposal["status"],
        "proposalId": proposal["id"],
        "result": allowed,
        "errorCode": proposal.get("errorCode"),
    }


async def resolve_playbook_version(playbook: str) -> str:
    """Fingerprint the exact playbook content without exposing its host path."""
    from lionagi.studio.services.playbooks import fingerprint_playbook

    return await anyio.to_thread.run_sync(fingerprint_playbook, playbook)


async def launch_playbook(arguments: dict[str, Any]) -> dict[str, Any]:
    args = LaunchPlaybookInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()
    target_version = await resolve_playbook_version(args.playbook)
    command = {
        "action_kind": "play",
        "action_playbook": args.playbook,
    }
    stable = store.canonical_hash(
        {
            "requestId": request_id,
            "tool": "launch_playbook",
            "command": command,
            "targetVersion": target_version,
        }
    )
    summary = f"Launch playbook '{args.playbook}'"
    if args.note:
        summary += f" — {args.note}"
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type="launch",
        command=command,
        risk="execute",
        summary=summary,
        idempotency_key=f"operator-app:{stable}",
        target_version=target_version,
    )
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in {"succeeded", "failed", "cancelled", "expired", "conflict"}:
            return _redacted_launch_result(proposal)
        await asyncio.sleep(0.1)


async def launch_agent(arguments: dict[str, Any]) -> dict[str, Any]:
    args = LaunchAgentInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()
    command = {
        "action_kind": "agent",
        "action_agent": args.agent,
        "action_prompt": args.prompt,
    }
    stable = store.canonical_hash(
        {
            "requestId": request_id,
            "tool": "launch_agent",
            "command": command,
        }
    )
    summary = f"Launch agent '{args.agent}'"
    if args.note:
        summary += f" — {args.note}"
    # No target_version: coordinator._verify_application_target only knows how
    # to re-check a "play" target against resolve_playbook_version, and raises
    # ApplicationTargetConflictError for any other action_kind once a version
    # is set. There is no agent-profile equivalent of playbook fingerprinting,
    # and passing one here would make every agent launch fail at approval time.
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type="launch",
        command=command,
        risk="execute",
        summary=summary,
        idempotency_key=f"operator-app:{stable}",
    )
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in {"succeeded", "failed", "cancelled", "expired", "conflict"}:
            return _redacted_launch_result(proposal)
        await asyncio.sleep(0.1)


_TOOL_HANDLERS = {
    "list_recent_runs": list_recent_runs,
    "run_stats": run_stats,
    "get_current_view": get_current_view,
    "list_schedules": list_schedules,
    "list_agents": list_agents,
    "list_playbooks": list_playbooks,
    "navigate": navigate,
    "prefill_schedule": prefill_schedule,
    "launch_playbook": launch_playbook,
    "launch_agent": launch_agent,
    "run_progress": run_progress,
    "run_findings": run_findings,
    "run_detail": run_detail,
    "cancel_run": cancel_run,
    "resume_run": resume_run,
    "rename_session": rename_session,
    "list_sessions": list_sessions,
    "session_detail": session_detail,
    "session_signals": session_signals,
    "get_invocation": get_invocation,
    "list_artifacts": list_artifacts,
    "get_artifact": get_artifact,
}


async def _dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        return None
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "studio-operator", "version": "1"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {"tools": _TOOL_SCHEMAS},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            return _tool_response(message_id, {"error": "Unknown Operator tool"}, error=True)
        try:
            result = await handler(params.get("arguments") or {})
            return _tool_response(message_id, result)
        except ValidationError as exc:
            return _tool_response(
                message_id,
                {
                    "error": "Invalid Operator tool arguments",
                    "details": exc.errors(
                        include_url=False,
                        include_context=False,
                        include_input=False,
                    ),
                },
                error=True,
            )
        except ValueError as exc:
            return _tool_response(message_id, {"error": scrub_text(str(exc))}, error=True)
        except Exception:  # noqa: BLE001
            return _tool_response(
                message_id,
                {"error": "Studio Operator application service is unavailable"},
                error=True,
            )
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _tool_response(
    message_id: Any,
    value: dict[str, Any],
    *,
    error: bool = False,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(value, sort_keys=True, separators=(",", ":")),
                }
            ],
            **({"isError": True} if error else {}),
        },
    }


async def _main() -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            return
        try:
            message = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        response = await _dispatch(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(_main())

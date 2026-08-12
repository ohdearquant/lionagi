# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Default Branch-backed engine for Studio Operator turns."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .redact import redact_arguments
from .types import OperatorEngineEvent, OperatorEngineTurn

if TYPE_CHECKING:
    from ..config import OperatorExecutionRootResolution

_SYSTEM_PROMPT = """\
You are the resident Operator for Lion Studio. Be concise and factual.
You may inspect the current project and help operate LionAGI. Never claim that
an action completed until its tool result says so. Disk writes, commands, and
other gated native work must go through the configured Studio permission
prompt; wait for the human decision. Use the strict studio_operator MCP tools
for reads, typed UI navigation/prefill, and playbook launches. The launch tool
creates its own human-confirmed durable proposal. Do not attempt to bypass
either gate or invent raw Studio endpoints.

Pick the right read tool. list_recent_runs returns only the newest 20, so it
can never answer "how many" — use run_stats for any count or rate over a
window. Use list_schedules, list_agents and list_playbooks to answer what
exists; call list_playbooks before proposing a launch, because launch_playbook
needs an exact existing name. For one named run, use run_progress for "how is
it going" (status, op counts, elapsed time) and run_findings for "what did it
find" (messages, tool calls, errors, artifacts); neither is a live feed, both
say how fresh their answer is. cancel_run stops a running run through the same
human-confirmed durable proposal launch_playbook uses — it is never automatic,
and a denial leaves the run untouched. resume_run continues a run through the
same human-confirmed proposal flow: an agent run takes a new instruction, a
play/flow/show-play run instead replays its persisted checkpoint and accepts
no instruction. Either way it launches a new invocation rather than
reopening the old run's status, and works on a run in any status, including
a cancelled one.

Every turn tells you which Studio view the human is on, including the route
and any selection or filters, and get_current_view re-reads it on demand. Use
it instead of asking, and never say you cannot tell what they are looking at.
There is no pixel screenshot: the view snapshot is the structured equivalent
and is what you should reason from. You also cannot restyle the page. A visual
change means editing source files through the permission prompt and
rebuilding, so say that plainly rather than implying a live tweak.
"""

_END = object()
_ONE_TURN_PERMISSION_KEYS = {
    "permission_prompt_tool_name",
    "strict_mcp_config",
    "setting_sources",
    "allowed_tools",
}
_REQUEST_SCOPED_MCP_SERVERS = {"studio_permission", "studio_operator"}
_OPERATOR_MCP_TOOLS = [
    "mcp__studio_operator__list_recent_runs",
    "mcp__studio_operator__run_stats",
    "mcp__studio_operator__get_current_view",
    "mcp__studio_operator__list_schedules",
    "mcp__studio_operator__list_agents",
    "mcp__studio_operator__list_playbooks",
    "mcp__studio_operator__navigate",
    "mcp__studio_operator__prefill_schedule",
    "mcp__studio_operator__launch_playbook",
    "mcp__studio_operator__run_progress",
    "mcp__studio_operator__run_findings",
    "mcp__studio_operator__run_detail",
    "mcp__studio_operator__cancel_run",
    "mcp__studio_operator__resume_run",
    "mcp__studio_operator__rename_session",
    "mcp__studio_operator__pause_run",
    "mcp__studio_operator__release_run_pause",
    "mcp__studio_operator__steer_run",
    "mcp__studio_operator__list_sessions",
    "mcp__studio_operator__session_detail",
    "mcp__studio_operator__session_signals",
    "mcp__studio_operator__get_invocation",
    "mcp__studio_operator__list_artifacts",
    "mcp__studio_operator__get_artifact",
]
_MODEL_CONTEXT_FRAME_LIMIT = 64
_MODEL_CONTEXT_BYTE_LIMIT = 128 * 1024
_CONTEXT_VALUE_BYTE_LIMIT = 2 * 1024


class OperatorProviderUnavailableError(RuntimeError):
    """The configured local Operator CLI is not installed."""


class OperatorExecutionRootError(ValueError):
    """The Operator has no explicit, usable directory for provider execution."""


@dataclass(frozen=True, slots=True)
class CompiledOperatorHistory:
    """A bounded, replayable history plus its durable compilation receipt."""

    frames: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _render_history_frame(frame: dict[str, Any]) -> str | None:
    """Render one normalized history frame for the provider-neutral prompt."""
    frame_type = frame.get("type")
    payload = frame.get("payload") or {}
    if frame_type == "text":
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            return None
        role = payload.get("role", "assistant")
        return f"{role}: {content}"
    if frame_type == "tool_call":
        return (
            f"assistant tool call {payload.get('tool', 'native_tool')} "
            f"[{payload.get('callId', '')}]: "
            f"{_canonical_json(payload.get('arguments') or {})}"
        )
    if frame_type == "tool_result":
        state = "ok" if payload.get("ok") else "error"
        return (
            f"tool result [{payload.get('callId', '')}] ({state}): "
            f"{_canonical_json(payload.get('result') or {})}"
        )
    if frame_type in {"proposal", "confirmation", "error"}:
        return f"{frame_type}: {_canonical_json(payload)}"
    return None


def _normalize_complete_turn(
    frames: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Merge streaming deltas and retain only complete native-tool pairs."""
    if not frames or not any(frame.get("type") == "done" for frame in frames):
        return ()

    pending_calls: dict[str, list[int]] = {}
    paired_tool_indices: set[int] = set()
    for index, frame in enumerate(frames):
        payload = frame.get("payload") or {}
        call_id = payload.get("callId")
        if not isinstance(call_id, str) or not call_id:
            continue
        if frame.get("type") == "tool_call":
            pending_calls.setdefault(call_id, []).append(index)
        elif frame.get("type") == "tool_result":
            candidates = pending_calls.get(call_id)
            if candidates:
                paired_tool_indices.add(candidates.pop(0))
                paired_tool_indices.add(index)

    normalized: list[dict[str, Any]] = []
    previous_raw_was_text = False
    for index, frame in enumerate(frames):
        frame_type = frame.get("type")
        payload = frame.get("payload") or {}
        if frame_type == "text":
            content = payload.get("content")
            if not isinstance(content, str) or not content:
                previous_raw_was_text = False
                continue
            role = payload.get("role", "assistant")
            text_format = payload.get("format", "plain")
            if (
                previous_raw_was_text
                and normalized
                and normalized[-1]["type"] == "text"
                and normalized[-1]["payload"].get("role", "assistant") == role
            ):
                prior = normalized[-1]["payload"]
                separator = "" if prior.get("format", "plain") == text_format else "\n\n"
                prior["content"] += separator + content
                prior["format"] = text_format
            else:
                normalized.append(
                    {
                        "version": 1,
                        "conversationId": frame.get("conversationId"),
                        "requestId": frame.get("requestId"),
                        "type": "text",
                        "payload": {
                            "content": content,
                            "format": text_format,
                            "role": role,
                        },
                    }
                )
            previous_raw_was_text = True
            continue

        previous_raw_was_text = False
        if frame_type in {"tool_call", "tool_result"}:
            if index not in paired_tool_indices:
                continue
        elif frame_type not in {"proposal", "confirmation", "error"}:
            continue
        normalized.append(
            {
                "version": 1,
                "conversationId": frame.get("conversationId"),
                "requestId": frame.get("requestId"),
                "type": frame_type,
                "payload": dict(payload),
            }
        )
    return tuple(normalized)


def compile_operator_history(
    complete_turns_newest_first: Sequence[Sequence[dict[str, Any]]],
) -> CompiledOperatorHistory:
    """Select newest complete turns atomically under the model history budget."""
    selected: list[tuple[Sequence[dict[str, Any]], tuple[dict[str, Any], ...]]] = []
    used_frames = 0
    used_bytes = 0
    for source_frames in complete_turns_newest_first:
        normalized = _normalize_complete_turn(source_frames)
        if not normalized:
            continue
        rendered = [
            text for frame in normalized if (text := _render_history_frame(frame)) is not None
        ]
        group_bytes = sum(len(text.encode("utf-8")) for text in rendered)
        group_bytes += max(0, len(rendered) - 1)
        separator_bytes = 1 if selected and rendered else 0
        if (
            used_frames + len(normalized) > _MODEL_CONTEXT_FRAME_LIMIT
            or used_bytes + separator_bytes + group_bytes > _MODEL_CONTEXT_BYTE_LIMIT
        ):
            # Do not skip a newer complete turn to admit an older one.
            break
        selected.append((source_frames, normalized))
        used_frames += len(normalized)
        used_bytes += separator_bytes + group_bytes

    chronological = list(reversed(selected))
    compiled_frames = tuple(frame for _source, group in chronological for frame in group)
    source_sequences = [
        int(frame["sequence"])
        for source, _group in chronological
        for frame in source
        if isinstance(frame.get("sequence"), int)
    ]
    digest = hashlib.sha256(_canonical_json(compiled_frames).encode("utf-8")).hexdigest()
    metadata = {
        "version": 1,
        "firstSequence": min(source_sequences) if source_sequences else None,
        "lastSequence": max(source_sequences) if source_sequences else None,
        "frameCount": len(compiled_frames),
        "turnCount": len(selected),
        "byteCount": used_bytes,
        "hash": digest,
    }
    return CompiledOperatorHistory(compiled_frames, metadata)


def _render_operator_context(context: Any) -> str | None:
    """Render the caller's view snapshot so the Operator knows where the human is.

    The browser sends this with every turn and the store persists it. Dropping
    it here is what makes the Operator answer "I cannot see which page you are
    on" — a statement about this function, not about what the turn carries.
    ``filters`` and ``selection`` are open-ended, so each rendered value is
    capped the same way history frames are.
    """
    if not isinstance(context, dict):
        return None
    lines: list[str] = []

    def add(label: str, value: Any) -> None:
        if value is None or value == {} or value == "":
            return
        text = value if isinstance(value, str) else _canonical_json(value)
        if len(text) > _CONTEXT_VALUE_BYTE_LIMIT:
            text = text[:_CONTEXT_VALUE_BYTE_LIMIT] + "… (truncated)"
        lines.append(f"- {label}: {text}")

    add("space", context.get("space"))
    add("route", context.get("route"))
    add("project", context.get("project"))
    add("selection", context.get("selection"))
    add("filters", context.get("filters"))
    if not lines:
        return None
    return (
        "The human is looking at this Studio view right now. Treat it as "
        "current fact and resolve their deictic references ('this page', "
        "'this agent', 'what am I looking at') against it:\n" + "\n".join(lines)
    )


def _compile_operator_prompt(turn: OperatorEngineTurn) -> str:
    """Render the caller's view plus the coordinator's bounded, turn-atomic history."""
    sections: list[str] = []
    if (view := _render_operator_context(turn.context)) is not None:
        sections.append(view)
    rendered = [
        text for frame in turn.history if (text := _render_history_frame(frame)) is not None
    ]
    if rendered:
        sections.append(
            "Recent durable Operator conversation (oldest to newest):\n" + "\n".join(rendered)
        )
    if not sections:
        return turn.instruction
    sections.append("Current operator instruction:\n" + turn.instruction)
    return "\n\n".join(sections)


async def write_resumable_operator_snapshot(branch: Any, snapshot_dir: str | Path) -> None:
    """Write a branch snapshot without its request-scoped permission bridge.

    The live branch must retain the MCP bridge while its provider subprocess
    runs. A detached resume cannot reuse that bridge: it names a completed
    Operator request. Temporarily removing the bridge only for serialization
    preserves messages and provider state while making resumed gated work fail
    closed through the CLI's ordinary permission mode.
    """
    from lionagi.cli._runs import _atomic_write_json

    kwargs = branch.chat_model.endpoint.config.kwargs
    original = dict(kwargs)
    try:
        for key in _ONE_TURN_PERMISSION_KEYS:
            kwargs.pop(key, None)
        servers = kwargs.get("mcp_servers")
        if isinstance(servers, dict):
            remaining = {
                name: config
                for name, config in servers.items()
                if name not in _REQUEST_SCOPED_MCP_SERVERS
            }
            if remaining:
                kwargs["mcp_servers"] = remaining
            else:
                kwargs.pop("mcp_servers", None)
        snapshot_path = Path(snapshot_dir) / f"{branch.id}.json"
        snapshot = branch.to_dict()
        await asyncio.to_thread(_atomic_write_json, snapshot_path, snapshot)
    finally:
        kwargs.clear()
        kwargs.update(original)


def _message_text(message: Any) -> str | None:
    """Best-effort final-message fallback for providers without delta chunks."""
    for attr in ("response", "content"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for key in ("content", "text"):
                text = value.get(key)
                if isinstance(text, str) and text:
                    return text
    rendered = getattr(message, "rendered", None)
    if isinstance(rendered, str) and rendered:
        return rendered
    return None


class BranchOperatorEngine:
    """Stream a fresh, bounded-context ``Branch.run`` for each durable turn.

    The Branch is an execution detail, not the Operator conversation record.
    Conversation continuity is reconstructed from durable frames supplied in
    ``turn.history``.
    """

    def stream(self, turn: OperatorEngineTurn):
        return self._stream(turn)

    async def _stream(self, turn: OperatorEngineTurn):
        # Preflight the provider this turn will actually run on, not the
        # environment default. Once a turn can carry its own selection, reading
        # the environment here refuses a valid Codex or Gemini turn because an
        # unselected Claude installation is missing.
        provider, _ = resolve_operator_provider_model(turn)
        if provider == "claude_code":
            from lionagi.providers.anthropic.claude_code import CLAUDE_CLI

            if not CLAUDE_CLI:
                raise OperatorProviderUnavailableError(
                    "Claude Code CLI is unavailable. Install it with "
                    "`npm install -g @anthropic-ai/claude-code`, then run "
                    "`claude auth login`."
                )
        branch = turn.runtime_branch or build_operator_branch(turn)
        chat_model = branch.chat_model

        prompt = _compile_operator_prompt(turn)

        queue: asyncio.Queue[Any] = asyncio.Queue()
        saw_text = False

        async def on_chunk(chunk: Any) -> None:
            nonlocal saw_text
            typ = getattr(chunk, "type", None)
            if typ == "system":
                metadata = getattr(chunk, "metadata", None) or {}
                session_id = metadata.get("session_id")
                if isinstance(session_id, str) and session_id:
                    await queue.put(
                        OperatorEngineEvent("session", {"providerSessionId": session_id})
                    )
            elif typ == "text":
                content = getattr(chunk, "content", None)
                if content:
                    saw_text = True
                    await queue.put(
                        OperatorEngineEvent(
                            "text",
                            {"content": str(content), "format": "markdown", "role": "assistant"},
                        )
                    )
            elif typ == "tool_use":
                tool_name = getattr(chunk, "tool_name", None) or "native_tool"
                lowered = tool_name.lower()
                mode = (
                    "draft"
                    if any(
                        token in lowered
                        for token in (
                            "bash",
                            "write",
                            "edit",
                            "notebook",
                            "command",
                            "launch",
                            "navigate",
                            "prefill",
                        )
                    )
                    else "read"
                )
                await queue.put(
                    OperatorEngineEvent(
                        "tool_call",
                        {
                            "callId": getattr(chunk, "tool_id", None) or "",
                            "tool": tool_name,
                            # A native tool call's own arguments are the
                            # model's choice, not something this bridge
                            # validated -- they can carry the same secrets or
                            # host paths a tool's output can.
                            "arguments": redact_arguments(getattr(chunk, "tool_input", None) or {}),
                            "mode": mode,
                        },
                    )
                )
            elif typ == "tool_result":
                await queue.put(
                    OperatorEngineEvent(
                        "tool_result",
                        {
                            "callId": getattr(chunk, "tool_id", None) or "",
                            "ok": not bool(getattr(chunk, "is_error", False)),
                            # Native output may contain secrets or unbounded logs.
                            "result": {"nativeToolCompleted": True},
                        },
                    )
                )

        chat_model.streaming_process_func = on_chunk

        async def consume() -> None:
            final_text: str | None = None
            try:
                # The coordinator owns canonical Operator snapshots because
                # Branch.run would serialize the live request-scoped MCP
                # permission bridge. StateDB message hooks still persist each
                # turn; the coordinator writes a sanitized snapshot at link
                # publication and terminalization.
                async for message in branch.run(prompt):
                    candidate = _message_text(message)
                    if candidate:
                        final_text = candidate
                if final_text and not saw_text:
                    await queue.put(
                        OperatorEngineEvent(
                            "text",
                            {"content": final_text, "format": "markdown", "role": "assistant"},
                        )
                    )
            except BaseException as exc:
                await queue.put(exc)
            finally:
                await queue.put(_END)

        task = asyncio.create_task(consume(), name=f"operator-provider-{turn.request_id}")
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


async def resolve_operator_execution_root(
    project: str | None,
    daemon_resolution: OperatorExecutionRootResolution | None,
) -> Path:
    """Resolve a turn from the daemon's frozen root choice or its project."""
    from lionagi.studio.config import (
        OPERATOR_CWD_ENV_VAR,
        OPERATOR_CWD_RULE_DEFAULT,
        OPERATOR_CWD_RULE_ENV,
    )
    from lionagi.studio.scheduler.engine import _is_usable_execution_root

    daemon_cwd = Path.cwd().resolve()
    if daemon_resolution is None:
        raise OperatorExecutionRootError(
            "Studio Operator execution root was not resolved at daemon startup. "
            "Refusing to resolve it from turn-time process state or to inherit "
            f"the Studio daemon working directory {str(daemon_cwd)!r}."
        )

    if daemon_resolution.rule == OPERATOR_CWD_RULE_ENV:
        if daemon_resolution.root is not None:
            return daemon_resolution.root
        raise OperatorExecutionRootError(
            f"{OPERATOR_CWD_ENV_VAR} is set to "
            f"{daemon_resolution.configured_value!r}, which is not an existing "
            "absolute directory. Refusing to run the Operator from the Studio "
            f"daemon working directory {str(daemon_cwd)!r}."
        )

    if daemon_resolution.rule != OPERATOR_CWD_RULE_DEFAULT:
        raise OperatorExecutionRootError(
            f"Studio Operator execution root startup rule "
            f"{daemon_resolution.rule!r} is unknown. Refusing to inherit the "
            f"Studio daemon working directory {str(daemon_cwd)!r}."
        )

    if project is not None:
        from lionagi.studio.services.projects import get_project

        project_row = await get_project(project)
        project_path = (
            project_row.get("path")
            if project_row is not None and isinstance(project_row.get("path"), str)
            else None
        )
        if project_path is not None and _is_usable_execution_root(project_path):
            return Path(project_path).resolve()
        raise OperatorExecutionRootError(
            f"Studio Operator project {project!r} has no usable registered "
            "execution root. Refusing to replace that selected project with "
            f"the daemon default {str(daemon_resolution.root)!r} or working "
            f"directory {str(daemon_cwd)!r}."
        )

    if daemon_resolution.root is not None:
        return daemon_resolution.root

    raise OperatorExecutionRootError(
        "Studio Operator's shipped daemon-config default "
        f"{daemon_resolution.configured_value!r} is not an existing absolute "
        f"directory. Set {OPERATOR_CWD_ENV_VAR} to an existing absolute "
        "directory. Refusing to inherit the Studio daemon working "
        f"directory {str(daemon_cwd)!r}."
    )


def resolve_operator_provider_model(turn: OperatorEngineTurn) -> tuple[str, str]:
    """Resolve (provider, model) for a turn.

    The turn's own selection wins; the environment variables remain the
    default for a turn that specifies neither, so an unmigrated caller (or a
    conversation that never chose a provider) keeps its old behavior exactly.
    """
    provider = turn.provider or os.environ.get("LIONAGI_STUDIO_OPERATOR_PROVIDER", "claude_code")
    model_name = turn.model or os.environ.get("LIONAGI_STUDIO_OPERATOR_MODEL", "sonnet")
    return provider, model_name


def _apply_operator_effort(
    provider: str, model_name: str, effort: str | None, model_kwargs: dict[str, Any]
) -> str:
    """Fold ``effort`` into ``model_kwargs`` (or the model name) the way each
    provider actually accepts it; returns the (possibly rewritten) model name.

    See ``lionagi/service/providers.py`` for the per-provider tables this
    mirrors: Claude and Codex take effort as a request kwarg (with model-
    dependent clamping); the gemini-code CLI has no effort kwarg at all and
    instead folds it into the resolved ``--model`` display name.
    """
    if not effort:
        return model_name
    from lionagi.service.providers import (
        PROVIDER_EFFORT_KWARG,
        PROVIDERS_EFFORT_VIA_MODEL_NAME,
        _clamp_claude_effort,
        _clamp_codex_effort,
    )

    kwarg = PROVIDER_EFFORT_KWARG.get(provider)
    if kwarg is not None:
        if provider == "codex":
            effort = _clamp_codex_effort(effort, model_name)
        elif provider == "claude_code":
            effort = _clamp_claude_effort(effort, model_name)
        model_kwargs[kwarg] = effort
        return model_name
    if provider in PROVIDERS_EFFORT_VIA_MODEL_NAME:
        from lionagi.providers.google.gemini_code import resolve_agy_model

        return resolve_agy_model(model_name, effort=effort)
    return model_name


def build_operator_branch(
    turn: OperatorEngineTurn,
    *,
    execution_root: Path | None = None,
):
    """Build the real, permission-gated Branch used by a canonical turn run."""
    from lionagi.service.manager import iModel
    from lionagi.session.branch import Branch

    provider, model_name = resolve_operator_provider_model(turn)
    model_kwargs: dict[str, Any] = {}
    if provider == "claude_code":
        from .store import OperatorStore

        db_path = turn.store_path or str(OperatorStore().path())
        model_kwargs = {
            "endpoint": "query_cli",
            # Claude's own auth is used by the CLI endpoint.
            "api_key": "dummy",
            "permission_mode": "default",
            "include_partial_messages": True,
            # These request-scoped application tools are safe to invoke
            # directly: reads are bounded, UI effects remain client-ACKed, and
            # launch_playbook performs its own durable human proposal.
            "allowed_tools": _OPERATOR_MCP_TOOLS,
            "permission_prompt_tool_name": "mcp__studio_permission__request_permission",
            "strict_mcp_config": True,
            # Continue the conversation's own provider session instead of
            # meeting the human as a stranger on every turn. Absent on the
            # first turn, which is the only turn that should start fresh.
            **({"resume": turn.provider_session_id} if turn.provider_session_id else {}),
            # Do not inherit project/user MCP servers into this privileged
            # control-plane conversation.
            "setting_sources": "",
            "mcp_servers": {
                "studio_permission": {
                    "command": sys.executable,
                    "args": ["-m", "lionagi.studio.operator.permission_mcp"],
                    "env": {
                        "LIONAGI_OPERATOR_DB_PATH": db_path,
                        "LIONAGI_OPERATOR_CONVERSATION_ID": turn.conversation_id,
                        "LIONAGI_OPERATOR_REQUEST_ID": turn.request_id,
                    },
                },
                "studio_operator": {
                    "command": sys.executable,
                    "args": ["-m", "lionagi.studio.operator.application_mcp"],
                    "env": {
                        "LIONAGI_OPERATOR_DB_PATH": db_path,
                        "LIONAGI_OPERATOR_CONVERSATION_ID": turn.conversation_id,
                        "LIONAGI_OPERATOR_REQUEST_ID": turn.request_id,
                    },
                },
            },
        }
    elif provider == "codex":
        # Codex's CLI request model has no MCP-server / permission-prompt-tool
        # fields (those are Claude Code CLI-specific) and no session-resume
        # field either, so a Codex-backed turn starts fresh each time and
        # runs without the Operator's application MCP tools.
        model_kwargs = {"endpoint": "query_cli", "api_key": "dummy"}
    elif provider == "gemini_code":
        # Same MCP/permission-tool limitation as Codex; agy does support
        # conversation resume via --conversation, so that continuity carries
        # over even though tool access does not.
        model_kwargs = {
            "endpoint": "query_cli",
            "api_key": "dummy",
            **({"resume": turn.provider_session_id} if turn.provider_session_id else {}),
        }
    if execution_root is not None:
        from lionagi.service.providers import PROVIDER_REPO_KWARG

        repo_kwarg = PROVIDER_REPO_KWARG.get(provider)
        if repo_kwarg is not None:
            model_kwargs[repo_kwarg] = str(execution_root)
    model_name = _apply_operator_effort(provider, model_name, turn.effort, model_kwargs)
    chat_model = iModel(provider=provider, model=model_name, **model_kwargs)
    # The conversation's own durable identity, so this turn's branch/session
    # persists as the same log entry every other turn of this conversation
    # uses instead of a fresh, unrelated one (see OperatorStore.claim_branch_id).
    # No live Branch is ever cached across turns -- only this id is reused.
    branch_kwargs: dict[str, Any] = {"system": _SYSTEM_PROMPT, "chat_model": chat_model}
    if turn.branch_id:
        branch_kwargs["id"] = turn.branch_id
    return Branch(**branch_kwargs)

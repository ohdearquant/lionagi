# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import warnings
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from lionagi.libs.path_safety import check_add_dirs_safe as check_add_dir_entries_safe
from lionagi.libs.path_safety import check_path_safe, check_paths_safe
from lionagi.libs.path_safety import contain_paths_in_root as contain_paths_in_repo
from lionagi.ln.concurrency.utils import maybe_await
from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers._cli_subprocess import (
    build_declarative_cli_args,
    discover_cli,
    ndjson_from_cli,
    print_readable,
    resolve_cli_workspace,
    validate_message_prompt,
)
from lionagi.providers._cli_subprocess import (
    make_cli_flag as _cli,
)
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from ._config import CodexConfigs

HAS_CODEX_CLI, CODEX_CLI = discover_cli("codex")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("codex-cli")


# --------------------------------------------------------------------------- types
CodexSandboxMode = Literal[
    "read-only",
    "workspace-write",
    "danger-full-access",
]

CodexApprovalMode = Literal[
    "untrusted",
    "on-request",
    "never",
]

CodexColorMode = Literal["always", "never", "auto"]

CodexReasoningEffort = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
]

__all__ = ("CodexCodeRequest", "stream_codex_cli", "CodexCLIEndpoint")


# --------------------------------------------------------------------------- request model
class CodexCodeRequest(BaseModel):
    """Configuration + prompt for an OpenAI Codex CLI invocation."""

    # ── prompt (always required) ──────────────────────────────────
    prompt: str = Field(description="The prompt for Codex CLI")

    # ── model & runtime (order 10–19) ─────────────────────────────
    model: str | None = Field(
        default="gpt-5.3-codex",
        description="Codex model to use",
        json_schema_extra=_cli("-m", 10),
    )
    profile: str | None = Field(
        default=None,
        description="Configuration profile from ~/.codex/config.toml",
        json_schema_extra=_cli("-p", 11),
    )
    oss: bool = Field(
        default=False,
        description="Use local open-source model provider (Ollama)",
        json_schema_extra=_cli("--oss", 12, "bool"),
    )
    search: bool = Field(
        default=False,
        description=(
            "Enable live web search for Codex. Since 2026-04, the old "
            "`--search` flag was removed; web search is now exposed as the "
            "`tool_search` feature flag (`stable`, default `true`). When "
            "search=True we emit `--enable tool_search`; when False we emit "
            "`--disable tool_search` to explicitly opt out."
        ),
    )

    # ── approval & sandbox (order 20–29) ──────────────────────────
    # bypass_approvals, full_auto, sandbox are special-cased (mutual exclusivity)
    ask_for_approval: CodexApprovalMode | None = Field(
        default=None,
        description="When Codex pauses for human approval",
    )
    full_auto: bool = Field(
        default=False,
        description="Auto-approve with workspace-write sandbox",
    )
    sandbox: CodexSandboxMode | None = Field(
        default=None,
        description="Sandbox mode for shell commands",
    )
    bypass_approvals: bool = Field(
        default=False,
        description="Skip ALL approvals and sandbox (DANGEROUS)",
    )

    # ── workspace (order 30–39) ───────────────────────────────────
    repo: Path = Field(default_factory=Path.cwd, exclude=True)
    ws: str | None = Field(default=None, exclude=True)
    add_dir: list[str] | None = Field(
        default=None,
        description="Additional directories to grant write access",
        json_schema_extra=_cli("--add-dir", 30, "repeat"),
    )

    # ── system prompt (order 40) ──────────────────────────────────
    system_prompt: str | None = None

    # ── output (order 50–59) ──────────────────────────────────────
    output_schema: str | Path | None = Field(
        default=None,
        description="Path to JSON Schema file for structured output",
        json_schema_extra=_cli("--output-schema", 50),
    )
    output_last_message: str | Path | None = Field(
        default=None,
        description="Write the final message to a file",
        json_schema_extra=_cli("--output-last-message", 51),
    )
    color: CodexColorMode | None = Field(
        default=None,
        description="ANSI color mode",
        json_schema_extra=_cli("--color", 52),
    )

    # ── features (order 60–69) ────────────────────────────────────
    skip_git_repo_check: bool = Field(
        default=True,
        description=(
            "Allow running outside a git repository. Default True: agents routinely "
            "run in per-task artifact dirs that are not git repos, where codex would "
            "otherwise refuse with 'Not inside a trusted directory'."
        ),
        json_schema_extra=_cli("--skip-git-repo-check", 60, "bool"),
    )
    ephemeral: bool = Field(
        default=False,
        description="Don't persist session to disk",
        json_schema_extra=_cli("--ephemeral", 61, "bool"),
    )
    no_alt_screen: bool = Field(
        default=False,
        description="Disable alternate screen mode for TUI",
        json_schema_extra=_cli("--no-alt-screen", 62, "bool"),
    )
    include_plan_tool: bool = Field(
        default=False,
        description="Include the plan tool in the conversation",
        json_schema_extra=_cli("--include-plan-tool", 63, "bool"),
    )

    # ── feature flags (order 70–79) ───────────────────────────────
    enable_features: list[str] | None = Field(
        default=None,
        description="Feature flags to enable",
        json_schema_extra=_cli("--enable", 70, "repeat"),
    )
    disable_features: list[str] | None = Field(
        default=None,
        description="Feature flags to disable",
        json_schema_extra=_cli("--disable", 71, "repeat"),
    )

    # ── reasoning (order 75, emitted as -c overrides) ───────────
    reasoning_effort: CodexReasoningEffort | None = Field(
        default=None,
        description="Reasoning effort level (emitted as -c reasoning_effort=<val>)",
    )
    plan_mode_reasoning_effort: CodexReasoningEffort | None = Field(
        default=None,
        description="Plan-mode reasoning effort (emitted as -c plan_mode_reasoning_effort=<val>)",
    )

    # ── fast mode (fast service tier) ────────────────────────────
    fast_mode: bool = Field(
        default=False,
        description=(
            "Route this request through OpenAI's *fast* service tier for "
            "lower latency. Emitted as ``-c service_tier=fast``. "
            "Does NOT cap or change ``reasoning_effort`` — "
            "``fast_mode=True`` with ``reasoning_effort='xhigh'`` is valid "
            "and gives maximum reasoning depth on the fast lane."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _clamp_effort(cls, values):
        """Clamp max/ultra down to the target model's supported ceiling (model-dependent: the gpt-5.6 family accepts max, sol/terra also ultra; earlier models top out at xhigh)."""
        from lionagi.service.providers import _clamp_codex_effort

        for key in ("reasoning_effort", "plan_mode_reasoning_effort"):
            effort = values.get(key)
            if isinstance(effort, str):
                values[key] = _clamp_codex_effort(effort, values.get("model"))
        return values

    # ── images & config (special-cased) ───────────────────────────
    images: list[str] = Field(
        default_factory=list,
        description="Image file paths to attach to the prompt",
    )
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Config overrides as key=value pairs (-c flag)",
    )

    # ── lionagi internal (no CLI flags) ───────────────────────────
    verbose_output: bool = Field(default=False, exclude=True)
    cli_display_theme: Literal["light", "dark"] = Field(default="light", exclude=True)
    cli_include_summary: bool = Field(default=False, exclude=True)

    # ── validators ────────────────────────────────────────────────

    @field_validator("add_dir", mode="before")
    def _norm_add_dir(cls, v):
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("add_dir", mode="after")
    @classmethod
    def _validate_add_dir(cls, v):
        if v is None:
            return v
        return check_add_dir_entries_safe(v, "add_dir")

    @field_validator("images", mode="after")
    @classmethod
    def _validate_images(cls, v):
        return check_paths_safe(v, "images")

    @field_validator("output_schema", "output_last_message", mode="before")
    @classmethod
    def _validate_output_paths(cls, v):
        if v is None:
            return v
        check_path_safe(str(v), "output_schema/output_last_message")
        return v

    @model_validator(mode="after")
    def _contain_path_fields_in_repo(self):
        repo_root = self.repo.resolve()
        if self.images:
            contain_paths_in_repo(self.images, repo_root, "images")
        if self.output_schema is not None:
            contain_paths_in_repo([str(self.output_schema)], repo_root, "output_schema")
        if self.output_last_message is not None:
            contain_paths_in_repo([str(self.output_last_message)], repo_root, "output_last_message")
        return self

    @model_validator(mode="before")
    @classmethod
    def _validate_message_prompt(cls, data):
        return validate_message_prompt(data)

    @model_validator(mode="after")
    def _warn_dangerous_settings(self):
        if self.bypass_approvals:
            warnings.warn(
                "CodexCodeRequest: bypass_approvals=True skips ALL approval "
                "prompts and disables sandboxing. EXTREMELY DANGEROUS. Only "
                "use in externally sandboxed environments.",
                UserWarning,
                stacklevel=4,
            )
        return self

    # ── workspace path ────────────────────────────────────────────

    def cwd(self) -> Path:
        return resolve_cli_workspace(self.repo, self.ws)

    # ── CLI command builder ───────────────────────────────────────

    def as_cmd_args(self) -> list[str]:
        """Build argument list for ``codex exec`` subcommand."""
        args: list[str] = ["exec", "--json"]

        args.extend(self._build_declarative_args())

        # Approval & sandbox: mutually exclusive hierarchy
        if self.bypass_approvals:
            args.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.full_auto:
            args.append("--full-auto")
        else:
            if self.ask_for_approval:
                args.extend(["-a", self.ask_for_approval])
            if self.sandbox:
                args.extend(["-s", self.sandbox])

        # Web search: old `--search` flag was removed upstream (2026-04);
        # express intent via the `tool_search` feature flag instead.
        if self.search:
            args.extend(["--enable", "tool_search"])
        else:
            args.extend(["--disable", "tool_search"])

        # Codex CLI has no --system-prompt flag; uses -c developer_instructions.
        if self.system_prompt:
            args.extend(["-c", f"developer_instructions={self.system_prompt}"])

        if self.reasoning_effort:
            args.extend(["-c", f"reasoning_effort={self.reasoning_effort}"])
        if self.plan_mode_reasoning_effort:
            args.extend(
                [
                    "-c",
                    f"plan_mode_reasoning_effort={self.plan_mode_reasoning_effort}",
                ]
            )

        if self.fast_mode:
            args.extend(["-c", "service_tier=fast"])

        for image in self.images:
            args.extend(["-i", image])

        for key, value in self.config_overrides.items():
            serialized = json.dumps(value) if not isinstance(value, str) else value
            args.extend(["-c", f"{key}={serialized}"])

        # Working directory (always emit)
        args.extend(["-C", str(self.cwd())])

        # Prompt always last, after -- to prevent flag interpretation
        args.extend(["--", self.prompt])

        return args

    def _build_declarative_args(self) -> list[str]:
        return build_declarative_cli_args(self)


CodexSession = CLISession


# --------------------------------------------------------------------------- NDJSON stream


# TODO(#1043 Phase 2): migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: CodexCodeRequest):
    if CODEX_CLI is None:
        raise RuntimeError("Codex CLI not found. Install with: npm i -g @openai/codex")
    cmd = [CODEX_CLI, *request.as_cmd_args()]
    # Do NOT pass cwd here — Codex CLI already gets the workspace via -C <repo>;
    # setting both double-resolves to 'repo/repo'. See docs/internals/runtime.md.
    async with contextlib.aclosing(ndjson_from_cli(cmd)) as stream:
        async for obj in stream:
            yield obj


# --------------------------------------------------------------------------- event stream


async def stream_codex_cli_events(request: CodexCodeRequest):
    """Stream events from Codex CLI."""
    if not CODEX_CLI:
        raise RuntimeError("Codex CLI not found (npm i -g @openai/codex)")
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj
    yield {"type": "done"}


def _pp_text(text: str, theme: str = "light") -> None:
    txt = f"""
    > 🟢 Codex:
    {text}
    """
    print_readable(txt, theme=theme)


def _pp_tool_use(tu: dict[str, Any], theme: str = "light") -> None:
    preview = shorten(str(tu.get("input", {})).replace("\n", " "), 130)
    body = f"- 🔧 Tool Use — {tu.get('name', 'unknown')}: {preview}"
    print_readable(body, border=False, panel=False, theme=theme)


def _pp_tool_result(tr: dict[str, Any], theme: str = "light") -> None:
    body_preview = shorten(str(tr.get("content", "")).replace("\n", " "), 130)
    status = "ERR" if tr.get("is_error") else "OK"
    body = f"- 📋 Tool Result — {status}: {body_preview}"
    print_readable(body, border=False, panel=False, theme=theme)


def _pp_final(sess: CLISession, theme: str = "light") -> None:
    usage = sess.usage or {}
    cost_str = f"${sess.total_cost_usd:.4f}" if sess.total_cost_usd else "N/A"
    txt = (
        f"\n### Codex Session complete\n"
        f"**Result:** {sess.result or ''}\n"
        f"- cost: {cost_str}\n"
        f"- turns: {sess.num_turns}\n"
        f"- duration: {sess.duration_ms} ms\n"
        f"- tokens: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
    )
    print_readable(txt, theme=theme)


# --------------------------------------------------------------------------- main parser


async def stream_codex_cli(
    request: CodexCodeRequest,
    session: CLISession | None = None,
    *,
    on_text: Callable[[str], None] | None = None,
    on_tool_use: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    on_final: Callable[[CLISession], None] | None = None,
) -> AsyncIterator[StreamChunk | CLISession]:
    """Consume the JSONL stream from Codex CLI, yield StreamChunks and populate a CodexSession."""
    if session is None:
        session = CLISession()
    theme = request.cli_display_theme or "light"
    _start_monotonic = asyncio.get_running_loop().time()

    # turn.completed reports usage/cost as a running total-to-date, not a
    # per-turn delta. run.py stamps each "result" chunk's metadata onto
    # whichever AssistantResponse it next flushes, and _collect_branch_usage
    # SUMS that metadata across every message on the branch -- if a tool call
    # flushes a message between two turn.completed events, each cumulative
    # snapshot lands on a different message and earlier turns get counted
    # again. Track the last-seen cumulative values here so only the marginal
    # (this-turn-only) delta is ever exposed per event.
    _prev_input_tokens = 0
    _prev_output_tokens = 0
    _prev_cost: float = 0.0
    _seen_cost = False

    stream = stream_codex_cli_events(request)
    try:
        async for obj in stream:
            typ = obj.get("type", "unknown")

            # -- thread / session start --
            if typ in ("thread.started", "system", "init", "session.start"):
                session.session_id = obj.get(
                    "thread_id",
                    obj.get("session_id", obj.get("id")),
                )
                session.model = obj.get("model")
                # Codex uses "thread_id" not "session_id"; normalize into the
                # metadata key every CLI provider's system chunk carries.
                sc = StreamChunk(type="system", metadata={**obj, "session_id": session.session_id})
                session.chunks.append(sc)
                yield sc

            # -- item.completed (agent_message, reasoning, tool calls) --
            elif typ == "item.completed":
                item = obj.get("item", {})
                item_type = item.get("type", "")

                if item_type == "agent_message":
                    text = item.get("text", "")
                    session.messages.append(item)
                    if on_text:
                        await maybe_await(on_text(text))
                    if request.verbose_output:
                        _pp_text(text, theme)
                    sc = StreamChunk(type="text", content=text, metadata=obj)
                    session.chunks.append(sc)
                    yield sc

                elif item_type in ("function_call", "tool_call"):
                    tu = {
                        "id": item.get("id", item.get("call_id", "")),
                        "name": item.get("name", item.get("function", "")),
                        "input": item.get(
                            "arguments",
                            item.get("input", item.get("args", {})),
                        ),
                    }
                    session.tool_uses.append(tu)
                    if on_tool_use:
                        await maybe_await(on_tool_use(tu))
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    sc = StreamChunk(
                        type="tool_use",
                        tool_name=tu["name"],
                        tool_id=tu["id"],
                        tool_input=tu["input"],
                        metadata=obj,
                    )
                    session.chunks.append(sc)
                    yield sc

                elif item_type == "command_execution":
                    item_id = item.get("id", "")
                    command = item.get("command", "")
                    output = item.get("aggregated_output", "")
                    exit_code = item.get("exit_code")
                    status = item.get("status", "")
                    is_error = status == "failed" or (exit_code is not None and exit_code != 0)

                    tu = {"id": item_id, "name": "Bash", "input": {"command": command}}
                    session.tool_uses.append(tu)
                    if on_tool_use:
                        await maybe_await(on_tool_use(tu))
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    sc = StreamChunk(
                        type="tool_use",
                        tool_name="Bash",
                        tool_id=item_id,
                        tool_input={"command": command},
                        metadata=obj,
                    )
                    session.chunks.append(sc)
                    yield sc

                    tr = {"tool_use_id": item_id, "content": output, "is_error": is_error}
                    session.tool_results.append(tr)
                    if on_tool_result:
                        await maybe_await(on_tool_result(tr))
                    if request.verbose_output:
                        _pp_tool_result(tr, theme)
                    sc = StreamChunk(
                        type="tool_result",
                        tool_id=item_id,
                        tool_output=output,
                        is_error=is_error,
                        metadata=obj,
                    )
                    session.chunks.append(sc)
                    yield sc

                elif item_type == "file_change":
                    item_id = item.get("id", "")
                    changes = item.get("changes", [])
                    status = item.get("status", "")
                    is_error = status == "failed"

                    tu = {"id": item_id, "name": "Edit", "input": {"changes": changes}}
                    session.tool_uses.append(tu)
                    if on_tool_use:
                        await maybe_await(on_tool_use(tu))
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    sc = StreamChunk(
                        type="tool_use",
                        tool_name="Edit",
                        tool_id=item_id,
                        tool_input={"changes": changes},
                        metadata=obj,
                    )
                    session.chunks.append(sc)
                    yield sc

                    summary_parts = [
                        f"{c.get('kind', 'change')}: {c.get('path', '?')}"
                        for c in changes
                        if isinstance(c, dict)
                    ]
                    tr = {
                        "tool_use_id": item_id,
                        "content": "; ".join(summary_parts) or status,
                        "is_error": is_error,
                    }
                    session.tool_results.append(tr)
                    if on_tool_result:
                        await maybe_await(on_tool_result(tr))
                    if request.verbose_output:
                        _pp_tool_result(tr, theme)
                    sc = StreamChunk(
                        type="tool_result",
                        tool_id=item_id,
                        tool_output=tr["content"],
                        is_error=is_error,
                        metadata=obj,
                    )
                    session.chunks.append(sc)
                    yield sc

                elif item_type == "function_call_output":
                    tr = {
                        "tool_use_id": item.get("call_id", item.get("id", "")),
                        "content": item.get("output", item.get("content", "")),
                        "is_error": item.get("is_error", False),
                    }
                    session.tool_results.append(tr)
                    if on_tool_result:
                        await maybe_await(on_tool_result(tr))
                    if request.verbose_output:
                        _pp_tool_result(tr, theme)
                    sc = StreamChunk(
                        type="tool_result",
                        tool_id=tr["tool_use_id"],
                        tool_output=tr["content"],
                        is_error=tr["is_error"],
                        metadata=obj,
                    )
                    session.chunks.append(sc)
                    yield sc

                elif item_type == "reasoning":
                    sc = StreamChunk(type="thinking", content=item.get("text"), metadata=obj)
                    session.chunks.append(sc)
                    yield sc

            # -- turn.completed (usage stats) --
            elif typ == "turn.completed":
                turn_usage = obj.get("usage", {}) or {}
                session.usage = turn_usage
                turn_cost = obj.get("total_cost_usd", obj.get("cost"))
                session.total_cost_usd = turn_cost
                session.num_turns = (session.num_turns or 0) + 1

                # Terminal usage/cost/turns -- the only channel run.py reads
                # provider-reported usage from (persisted onto model_response,
                # see run.py's "result" chunk handling; mirrors claude_code's
                # "result" event). turn.completed can fire more than once per
                # run() call for multi-turn codex sessions, and each event's
                # usage/cost is cumulative-to-date, not a per-turn delta --
                # emit only the marginal contribution since the previous
                # event (clamped at 0 in case a provider quirk ever reports a
                # lower running total) so summing across every flushed
                # message reconstructs the true total exactly once, not N
                # times over.
                cur_input = int(
                    turn_usage.get("input_tokens", turn_usage.get("prompt_tokens", 0)) or 0
                )
                cur_output = int(
                    turn_usage.get("output_tokens", turn_usage.get("completion_tokens", 0)) or 0
                )
                delta_input = max(cur_input - _prev_input_tokens, 0)
                delta_output = max(cur_output - _prev_output_tokens, 0)
                _prev_input_tokens = cur_input
                _prev_output_tokens = cur_output

                result_meta: dict[str, Any] = {}
                if delta_input or delta_output:
                    result_meta["usage"] = {
                        "input_tokens": delta_input,
                        "output_tokens": delta_output,
                    }
                if isinstance(turn_cost, (int, float)):
                    delta_cost = float(turn_cost) - (_prev_cost if _seen_cost else 0.0)
                    _prev_cost = float(turn_cost)
                    _seen_cost = True
                    result_meta["total_cost_usd"] = max(delta_cost, 0.0)
                # Each turn.completed occurrence is exactly one new turn --
                # unlike usage/cost this is already a delta, never a running
                # total (num_turns is incremented locally above, not read off
                # the event), so it is always safe to emit as 1.
                result_meta["num_turns"] = 1
                rsc = StreamChunk(type="result", metadata=result_meta)
                session.chunks.append(rsc)
                yield rsc

            # -- turn.failed / error --
            elif typ in ("turn.failed", "error"):
                session.is_error = True
                err = obj.get("error", {})
                # Error message location varies by event type; capture the raw
                # value pre-normalization for the benign-EOS check below.
                _raw_err = err
                if err is None:
                    err = {}
                session.result = (
                    (
                        err.get("message")
                        or obj.get("message")
                        or (
                            f"CLI failure (empty error payload; event type={typ!r})"
                            if err == {}
                            else str(err)
                        )
                    )
                    if isinstance(err, dict)
                    else obj.get("message", str(err))
                )
                # Benign-EOS sentinel on resumed sessions — see docs/internals/runtime.md
                # for the exact 3-condition classification.
                _is_benign_eos = (
                    typ == "error"
                    and "error" in obj  # a bare {"type": "error"} is malformed, not EOF
                    and _raw_err == {}  # null normalised to {} must NOT qualify
                    and not any(k in obj for k in ("code", "message", "status"))
                )
                chunk_meta = dict(obj)
                if _is_benign_eos:
                    chunk_meta["benign_eos"] = True
                    session.is_error = False  # retract: not a real error
                else:
                    if request.verbose_output:
                        log.error("Codex error: %s", session.result)
                sc = StreamChunk(type="error", content=session.result, metadata=chunk_meta)
                session.chunks.append(sc)
                yield sc

            # -- legacy event types (older CLI versions) --
            elif typ in ("message", "assistant", "agent"):
                msg = obj.get("message", obj)
                session.messages.append(msg)

                content = msg.get("content", "")
                if isinstance(content, str):
                    if on_text:
                        await maybe_await(on_text(content))
                    if request.verbose_output:
                        _pp_text(content, theme)
                    sc = StreamChunk(type="text", content=content, metadata=obj)
                    session.chunks.append(sc)
                    yield sc
                elif isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        btype = blk.get("type")
                        if btype == "text":
                            text = blk.get("text", "")
                            if on_text:
                                await maybe_await(on_text(text))
                            if request.verbose_output:
                                _pp_text(text, theme)
                            sc = StreamChunk(type="text", content=text, metadata=obj)
                            session.chunks.append(sc)
                            yield sc
                        elif btype in ("tool_use", "function_call"):
                            tu = {
                                "id": blk.get("id", ""),
                                "name": blk.get("name", blk.get("function", {}).get("name", "")),
                                "input": blk.get("input", blk.get("arguments", {})),
                            }
                            session.tool_uses.append(tu)
                            if on_tool_use:
                                await maybe_await(on_tool_use(tu))
                            if request.verbose_output:
                                _pp_tool_use(tu, theme)
                            sc = StreamChunk(
                                type="tool_use",
                                tool_name=tu["name"],
                                tool_id=tu["id"],
                                tool_input=tu["input"],
                                metadata=obj,
                            )
                            session.chunks.append(sc)
                            yield sc

            elif typ in ("result", "response", "session.end"):
                session.result = obj.get(
                    "result",
                    obj.get("response", obj.get("text", "")),
                ).strip()
                session.usage = obj.get("usage", obj.get("stats", {}))
                session.total_cost_usd = obj.get("total_cost_usd", obj.get("cost"))
                session.num_turns = obj.get("num_turns", obj.get("turns"))
                session.duration_ms = obj.get("duration_ms", obj.get("duration"))
                session.is_error = obj.get("is_error", obj.get("error") is not None)

                # Legacy terminal event (older CLI versions) -- same seam as
                # turn.completed above.
                result_meta: dict[str, Any] = {}
                if session.usage:
                    result_meta["usage"] = session.usage
                if session.total_cost_usd is not None:
                    result_meta["total_cost_usd"] = session.total_cost_usd
                if session.num_turns is not None:
                    result_meta["num_turns"] = session.num_turns
                if session.duration_ms is not None:
                    result_meta["duration_ms"] = session.duration_ms
                if result_meta:
                    rsc = StreamChunk(type="result", metadata=result_meta)
                    session.chunks.append(rsc)
                    yield rsc

            elif typ == "done":
                break
    finally:
        await stream.aclose()

    if not session.result:
        parts = [c.content for c in session.chunks if c.type == "text" and c.content]
        if parts:
            session.result = "\n".join(parts)
    if session.num_turns is None and session.messages:
        session.num_turns = len(session.messages)
    if session.duration_ms is None:
        session.duration_ms = int((asyncio.get_running_loop().time() - _start_monotonic) * 1000)

    if on_final:
        await maybe_await(on_final(session))
    if request.verbose_output:
        _pp_final(session, theme)

    yield session


codex_log = log


CONTEXT_WINDOWS: dict[str, int] = {
    "codex-mini": 200_000,
    "o4-mini": 200_000,
    "o3": 200_000,
}

_CODEX_HANDLER_PARAMS = (
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_final",
)


@CodexConfigs.CLI.register
class CodexCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _CODEX_HANDLER_PARAMS
    _handler_params = _CODEX_HANDLER_PARAMS
    _handler_kwarg = "codex_handlers"
    _request_model = CodexCodeRequest
    # Codex streams a "thread.started"/"system" event right after spawn —
    # see stream_codex_cli() above.
    streams_first_output_early = True

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("codex_handlers", None)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers)

    @property
    def codex_handlers(self):
        return self._handlers

    @codex_handlers.setter
    def codex_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_codex_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, CLISession):
                    if item.is_error:
                        yield StreamChunk(
                            type="error",
                            content=item.result or "Codex session failed",
                        )
                    continue
                yield item

    async def _call(
        self,
        payload: dict,
        headers: dict,
        **kwargs,
    ):
        responses = []
        request: CodexCodeRequest = payload["request"]
        session: CLISession = CLISession()
        handlers = self._runtime_handlers(kwargs)

        async with contextlib.aclosing(stream_codex_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "done":
                        break
                responses.append(chunk)

        codex_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")
        if not session.result:
            texts = [c.content for c in session.chunks if c.type == "text" and c.content]
            session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)

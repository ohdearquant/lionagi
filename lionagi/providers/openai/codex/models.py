# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import codecs
import contextlib
import inspect
import json
import logging
import shutil
import warnings
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from dataclasses import field as datafield
from functools import partial
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from lionagi import ln
from lionagi.libs.schema.as_readable import as_readable

HAS_CODEX_CLI = False
CODEX_CLI = None

if (c := (shutil.which("codex") or "codex")) and shutil.which(c):
    HAS_CODEX_CLI = True
    CODEX_CLI = c

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
]

__all__ = (
    "CodexChunk",
    "CodexCodeRequest",
    "CodexSession",
    "stream_codex_cli",
)


# --------------------------------------------------------------------------- flag metadata
#
# Same declarative pattern as claude_code.py.  Each field with _cli()
# metadata is automatically assembled into CLI args by
# _build_declarative_args(), sorted by order.
#
# kind semantics:
#   value      – ``--flag <str(val)>``
#   bool       – ``--flag`` when truthy, omit otherwise
#   repeat     – ``--flag a --flag b`` (flag repeated per item)


def _cli(
    flag: str,
    order: int,
    kind: str = "value",
) -> dict[str, Any]:
    return {
        "cli_flag": flag,
        "cli_order": order,
        "cli_kind": kind,
    }


# --------------------------------------------------------------------------- request model
class CodexCodeRequest(BaseModel):
    """Configuration + prompt for an OpenAI Codex CLI invocation.

    Fields annotated with ``_cli(...)`` metadata are automatically
    assembled into CLI arguments by :meth:`as_cmd_args`, sorted by
    ``order``.  Special cases (``bypass_approvals``, ``full_auto``,
    ``sandbox``, ``config_overrides``, ``images``) are handled
    explicitly after the declarative pass.

    Adding a new CLI flag is one line: declare the field with ``_cli()``
    metadata and the builder picks it up automatically.
    """

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
        default=False,
        description="Allow running outside a git repository",
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

    # ── fast mode (priority service tier) ────────────────────────
    fast_mode: bool = Field(
        default=False,
        description=(
            "Route this request through OpenAI's *priority* service tier for "
            "lower latency. Emitted as ``-c service_tier=priority``. "
            "Requires an OpenAI account with priority-tier eligibility. "
            "Does NOT cap or change ``reasoning_effort`` — "
            "``fast_mode=True`` with ``reasoning_effort='xhigh'`` is valid "
            "and gives maximum reasoning depth on the fast lane."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _clamp_effort(cls, values):
        """Clamp 'max' → 'xhigh' for both effort fields.

        Agent profiles (critic, orchestrator) use effort: max which is valid
        for Claude Code but not for the Codex enum. This validator catches
        any value that slips past the upstream _CODEX_EFFORT_CLAMP in
        _providers.py (e.g. direct CodexCodeRequest construction).
        """
        for key in ("reasoning_effort", "plan_mode_reasoning_effort"):
            if values.get(key) == "max":
                values[key] = "xhigh"
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

    @model_validator(mode="before")
    @classmethod
    def _validate_message_prompt(cls, data):
        """Convert messages format to prompt if needed."""
        if data.get("prompt"):
            return data

        if not (msg := data.get("messages")):
            raise ValueError("messages or prompt required")

        prompts = []
        for message in msg:
            if message["role"] != "system":
                content = message["content"]
                if isinstance(content, (dict, list)):
                    prompts.append(ln.json_dumps(content))
                else:
                    prompts.append(content)
            elif message["role"] == "system" and not data.get("system_prompt"):
                data["system_prompt"] = message["content"]

        data["prompt"] = "\n".join(prompts)
        return data

    @model_validator(mode="after")
    def _warn_dangerous_settings(self):
        """Emit security warnings for dangerous CLI settings."""
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
        """Get working directory, validating workspace path."""
        if not self.ws:
            return self.repo

        ws_path = Path(self.ws)

        if ws_path.is_absolute():
            raise ValueError(
                f"Workspace path must be relative, got absolute: {self.ws}"
            )

        if ".." in ws_path.parts:
            raise ValueError(
                f"Directory traversal detected in workspace path: {self.ws}"
            )

        repo_resolved = self.repo.resolve()
        result = (self.repo / ws_path).resolve()

        try:
            result.relative_to(repo_resolved)
        except ValueError:
            raise ValueError(
                f"Workspace path escapes repository bounds. "
                f"Repository: {repo_resolved}, Workspace: {result}"
            ) from None

        return result

    # ── CLI command builder ───────────────────────────────────────

    def as_cmd_args(self) -> list[str]:
        """Build argument list for ``codex exec`` subcommand.

        Flags are assembled in two passes:

        1. **Declarative** – fields with ``_cli()`` metadata, sorted by
           ``order``.
        2. **Special cases** – approval/sandbox (mutual exclusivity),
           images (``-i`` repeat), config overrides (``-c key=val``).

        Structure: ``exec --json [flags] -C <cwd> -- <prompt>``
        """
        args: list[str] = ["exec", "--json"]

        # ── pass 1: declarative flags ──
        args.extend(self._build_declarative_args())

        # ── pass 2: special cases ──

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

        # System prompt → -c developer_instructions=<val>
        # (Codex CLI has no --system-prompt flag; uses developer_instructions)
        if self.system_prompt:
            args.extend(["-c", f"developer_instructions={self.system_prompt}"])

        # Reasoning effort → -c reasoning_effort=<val>
        if self.reasoning_effort:
            args.extend(["-c", f"reasoning_effort={self.reasoning_effort}"])
        if self.plan_mode_reasoning_effort:
            args.extend(
                [
                    "-c",
                    f"plan_mode_reasoning_effort={self.plan_mode_reasoning_effort}",
                ]
            )

        # Fast mode → -c service_tier=priority (priority lane, lower latency)
        if self.fast_mode:
            args.extend(["-c", "service_tier=priority"])

        # Images (repeat -i per image)
        for image in self.images:
            args.extend(["-i", image])

        # Config overrides (-c key=value)
        for key, value in self.config_overrides.items():
            serialized = json.dumps(value) if not isinstance(value, str) else value
            args.extend(["-c", f"{key}={serialized}"])

        # Working directory (always emit)
        args.extend(["-C", str(self.cwd())])

        # Prompt always last, after -- to prevent flag interpretation
        args.extend(["--", self.prompt])

        return args

    def _build_declarative_args(self) -> list[str]:
        """Collect fields with ``_cli()`` metadata and emit flags."""
        flagged: list[tuple[int, dict, Any]] = []
        for field_name, field_info in type(self).model_fields.items():
            extra = field_info.json_schema_extra
            if not extra or "cli_flag" not in extra:
                continue
            val = getattr(self, field_name)
            if val is None:
                continue
            if isinstance(val, list) and not val:
                continue
            if val is False:
                continue
            flagged.append((extra["cli_order"], extra, val))

        flagged.sort(key=lambda x: x[0])

        args: list[str] = []
        for _, extra, val in flagged:
            flag = extra["cli_flag"]
            kind = extra.get("cli_kind", "value")

            if kind == "bool":
                if val:
                    args.append(flag)

            elif kind == "repeat":
                for v in val:
                    args.extend([flag, str(v)])

            else:  # "value"
                args.extend([flag, str(val)])

        return args


# --------------------------------------------------------------------------- chunks & session


@dataclass
class CodexChunk:
    """Low-level wrapper around every JSON object from the Codex CLI."""

    raw: dict[str, Any]
    type: str
    # convenience views
    text: str | None = None
    tool_use: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None


@dataclass
class CodexSession:
    """Aggregated view of a whole Codex CLI conversation."""

    session_id: str | None = None
    model: str | None = None

    # chronological log
    chunks: list[CodexChunk] = datafield(default_factory=list)

    # materialized views
    messages: list[dict[str, Any]] = datafield(default_factory=list)
    tool_uses: list[dict[str, Any]] = datafield(default_factory=list)
    tool_results: list[dict[str, Any]] = datafield(default_factory=list)

    # final summary
    result: str = ""
    usage: dict[str, Any] = datafield(default_factory=dict)
    total_cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    is_error: bool = False
    summary: dict | None = None

    def populate_summary(self) -> None:
        self.summary = _extract_summary(self)


def _extract_summary(session: CodexSession) -> dict[str, Any]:
    """Extract summary from session data."""
    tool_counts: dict[str, int] = {}
    tool_details: list[dict[str, Any]] = []
    file_operations: dict[str, list[str]] = {
        "reads": [],
        "writes": [],
        "edits": [],
    }
    key_actions: list[str] = []

    for tool_use in session.tool_uses:
        tool_name = tool_use.get("name", "unknown")
        tool_input = tool_use.get("input", {})
        tool_id = tool_use.get("id", "")

        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        tool_details.append({"tool": tool_name, "id": tool_id, "input": tool_input})

        if tool_name in ("read_file", "Read", "read"):
            file_path = tool_input.get("path", tool_input.get("file_path", "unknown"))
            file_operations["reads"].append(file_path)
            key_actions.append(f"Read {file_path}")

        elif tool_name in ("write_file", "create_file", "Write", "write"):
            file_path = tool_input.get("path", tool_input.get("file_path", "unknown"))
            file_operations["writes"].append(file_path)
            key_actions.append(f"Wrote {file_path}")

        elif tool_name in ("edit_file", "patch", "Edit", "edit"):
            file_path = tool_input.get("path", tool_input.get("file_path", "unknown"))
            file_operations["edits"].append(file_path)
            key_actions.append(f"Edited {file_path}")

        elif tool_name in (
            "shell",
            "terminal",
            "run_shell_command",
            "Bash",
            "bash",
        ):
            command = tool_input.get("command", tool_input.get("cmd", ""))
            command_summary = command[:50] + "..." if len(command) > 50 else command
            key_actions.append(f"Ran: {command_summary}")

        elif tool_name.startswith("mcp_") or tool_name.startswith("mcp__"):
            operation = tool_name.replace("mcp__", "").replace("mcp_", "")
            key_actions.append(f"MCP {operation}")

        else:
            key_actions.append(f"Used {tool_name}")

    key_actions = (
        list(dict.fromkeys(key_actions)) if key_actions else ["No specific actions"]
    )

    for op_type in file_operations:
        file_operations[op_type] = list(dict.fromkeys(file_operations[op_type]))

    result_summary = (
        (session.result[:200] + "...") if len(session.result) > 200 else session.result
    )

    return {
        "tool_counts": tool_counts,
        "tool_details": tool_details,
        "file_operations": file_operations,
        "key_actions": key_actions,
        "total_tool_calls": sum(tool_counts.values()),
        "result_summary": result_summary,
        "usage_stats": {
            "total_cost_usd": session.total_cost_usd,
            "num_turns": session.num_turns,
            "duration_ms": session.duration_ms,
            **session.usage,
        },
    }


# --------------------------------------------------------------------------- NDJSON stream


async def _ndjson_from_cli(request: CodexCodeRequest):
    """
    Yields each JSON object emitted by the Codex CLI (JSONL mode).

    Robust against UTF-8 splits and uses json.JSONDecoder.raw_decode.
    Drains stderr concurrently into a bounded buffer so the subprocess
    cannot deadlock when it produces large stderr volumes before any
    stdout output. The codex CLI launches with ``start_new_session=True``,
    so cancellation terminates the whole process group rather than just
    the direct child — needed for cleanup when shells, ssh, etc. are
    spawned beneath the CLI itself.
    """
    if CODEX_CLI is None:
        raise RuntimeError("Codex CLI not found. Install with: npm i -g @openai/codex")

    proc = await asyncio.create_subprocess_exec(
        CODEX_CLI,
        *request.as_cmd_args(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # isolate from parent's SIGINT
    )
    # Capture PGID NOW — if we wait until teardown, the child may have
    # exited and been reaped, ``os.getpgid(proc.pid)`` would raise
    # ProcessLookupError, and we'd skip the group kill entirely.
    # ``start_new_session=True`` makes pgid == proc.pid.
    _codex_pgid: int = proc.pid

    decoder = codecs.getincrementaldecoder("utf-8")()
    json_decoder = json.JSONDecoder()
    buffer: str = ""

    if proc.stdout is None:
        raise RuntimeError("Failed to capture stdout from Codex CLI")

    # Bounded stderr capture (256 KiB) — enough for a useful error tail,
    # bounded so a runaway logger can't consume unlimited memory.
    stderr_cap = 256 * 1024
    stderr_chunks: list[bytes] = []
    stderr_total = 0

    async def _drain_stderr() -> None:
        nonlocal stderr_total
        if proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                remaining = stderr_cap - stderr_total
                if remaining > 0:
                    take = chunk[:remaining]
                    stderr_chunks.append(take)
                    stderr_total += len(take)
                # Beyond the cap we keep draining the pipe (so the
                # subprocess never blocks on a full pipe buffer) but
                # discard the bytes.
        except Exception as exc:
            log.debug("stderr drain ended: %s", exc)

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break

            buffer += decoder.decode(chunk)

            while buffer:
                buffer = buffer.lstrip()
                if not buffer:
                    break
                try:
                    obj, idx = json_decoder.raw_decode(buffer)
                    yield obj
                    buffer = buffer[idx:]
                except json.JSONDecodeError:
                    break

        buffer += decoder.decode(b"", final=True)
        buffer = buffer.strip()
        if buffer:
            try:
                obj, idx = json_decoder.raw_decode(buffer)
                yield obj
            except json.JSONDecodeError:
                log.error("Skipped unrecoverable JSON tail: %.120s...", buffer)

        if await proc.wait() != 0:
            # Drain task should be done now (stdout EOF + proc exit ⇒
            # stderr EOF). ``shield`` so wait_for's timeout doesn't
            # cancel the drain on slow systems — if we hit the timeout
            # we report what we have and mark it truncated.
            drain_truncated = False
            try:
                await asyncio.wait_for(
                    asyncio.shield(stderr_task), timeout=2.0
                )
            except asyncio.TimeoutError:
                drain_truncated = True
            except asyncio.CancelledError:
                # If WE are cancelled, propagate.
                raise
            err = b"".join(stderr_chunks).decode(errors="replace").strip()
            if drain_truncated:
                err = (err or "") + " [stderr drain timed out]"
            raise RuntimeError(err or "Codex CLI exited non-zero")

    finally:
        # Terminate the whole process group (start_new_session=True
        # above made pgid == proc.pid). Captured up-front so a reap
        # before teardown doesn't make us skip the group kill.
        import os
        import signal

        pgid = _codex_pgid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

        # Reap the stderr drain task. ``contextlib.suppress(Exception)``
        # does NOT catch CancelledError (BaseException) — we have to
        # suppress it explicitly so the intentional cancel here doesn't
        # mask the actual generator outcome.
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001 — intentional teardown reap
            pass


# --------------------------------------------------------------------------- event stream


async def stream_codex_cli_events(request: CodexCodeRequest):
    """Stream events from Codex CLI."""
    if not CODEX_CLI:
        raise RuntimeError("Codex CLI not found (npm i -g @openai/codex)")
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj
    yield {"type": "done"}


async def _maybe_await(func, *args, **kw):
    """Call func which may be sync or async."""
    res = func(*args, **kw) if func else None
    if inspect.iscoroutine(res):
        await res


print_readable = partial(as_readable, md=True, display_str=True)


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


def _pp_final(sess: CodexSession, theme: str = "light") -> None:
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
    session: CodexSession | None = None,
    *,
    on_text: Callable[[str], None] | None = None,
    on_tool_use: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    on_final: Callable[[CodexSession], None] | None = None,
) -> AsyncIterator[CodexChunk | dict | CodexSession]:
    """
    Consume the JSONL stream from Codex CLI and return a populated
    CodexSession.

    Handles flexible event type names since Codex CLI output format
    may vary.
    """
    if session is None:
        session = CodexSession()
    theme = request.cli_display_theme or "light"
    _start_monotonic = asyncio.get_running_loop().time()

    stream = stream_codex_cli_events(request)
    try:
        async for obj in stream:
            typ = obj.get("type", "unknown")
            chunk = CodexChunk(raw=obj, type=typ)
            session.chunks.append(chunk)

            # -- thread / session start --
            if typ in ("thread.started", "system", "init", "session.start"):
                session.session_id = obj.get(
                    "thread_id",
                    obj.get("session_id", obj.get("id")),
                )
                session.model = obj.get("model")
                yield obj

            # -- item.completed (agent_message, reasoning, tool calls) --
            elif typ == "item.completed":
                item = obj.get("item", {})
                item_type = item.get("type", "")

                if item_type == "agent_message":
                    text = item.get("text", "")
                    chunk.text = text
                    session.messages.append(item)
                    await _maybe_await(on_text, text)
                    if request.verbose_output:
                        _pp_text(text, theme)
                    yield chunk

                elif item_type in ("function_call", "tool_call"):
                    tu = {
                        "id": item.get("id", item.get("call_id", "")),
                        "name": item.get("name", item.get("function", "")),
                        "input": item.get(
                            "arguments",
                            item.get("input", item.get("args", {})),
                        ),
                    }
                    chunk.tool_use = tu
                    session.tool_uses.append(tu)
                    await _maybe_await(on_tool_use, tu)
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    yield chunk

                elif item_type == "command_execution":
                    # Codex CLI emits command_execution items with command +
                    # aggregated_output + exit_code. Treat as a paired
                    # tool_use + tool_result so downstream message routing
                    # records both the request and the result.
                    item_id = item.get("id", "")
                    command = item.get("command", "")
                    output = item.get("aggregated_output", "")
                    exit_code = item.get("exit_code")
                    status = item.get("status", "")
                    is_error = status == "failed" or (
                        exit_code is not None and exit_code != 0
                    )

                    tu = {"id": item_id, "name": "Bash", "input": {"command": command}}
                    chunk.tool_use = tu
                    session.tool_uses.append(tu)
                    await _maybe_await(on_tool_use, tu)
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    yield chunk

                    # Emit paired tool_result on a fresh chunk so the caller
                    # always sees both halves of the exchange.
                    result_chunk = CodexChunk(raw=obj, type=typ)
                    tr = {
                        "tool_use_id": item_id,
                        "content": output,
                        "is_error": is_error,
                    }
                    result_chunk.tool_result = tr
                    session.tool_results.append(tr)
                    session.chunks.append(result_chunk)
                    await _maybe_await(on_tool_result, tr)
                    if request.verbose_output:
                        _pp_tool_result(tr, theme)
                    yield result_chunk

                elif item_type == "file_change":
                    # Codex CLI emits file_change items with a `changes` list.
                    # Surface each change as a single Edit tool call so the
                    # branch records what files were touched.
                    item_id = item.get("id", "")
                    changes = item.get("changes", [])
                    status = item.get("status", "")
                    is_error = status == "failed"

                    tu = {
                        "id": item_id,
                        "name": "Edit",
                        "input": {"changes": changes},
                    }
                    chunk.tool_use = tu
                    session.tool_uses.append(tu)
                    await _maybe_await(on_tool_use, tu)
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    yield chunk

                    result_chunk = CodexChunk(raw=obj, type=typ)
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
                    result_chunk.tool_result = tr
                    session.tool_results.append(tr)
                    session.chunks.append(result_chunk)
                    await _maybe_await(on_tool_result, tr)
                    if request.verbose_output:
                        _pp_tool_result(tr, theme)
                    yield result_chunk

                elif item_type == "function_call_output":
                    tr = {
                        "tool_use_id": item.get("call_id", item.get("id", "")),
                        "content": item.get("output", item.get("content", "")),
                        "is_error": item.get("is_error", False),
                    }
                    chunk.tool_result = tr
                    session.tool_results.append(tr)
                    await _maybe_await(on_tool_result, tr)
                    if request.verbose_output:
                        _pp_tool_result(tr, theme)
                    yield chunk

                elif item_type == "reasoning":
                    yield chunk

                else:
                    yield chunk

            # -- turn.completed (usage stats) --
            elif typ == "turn.completed":
                session.usage = obj.get("usage", {})
                session.total_cost_usd = obj.get("total_cost_usd", obj.get("cost"))
                session.num_turns = (session.num_turns or 0) + 1

            # -- turn.failed / error --
            elif typ in ("turn.failed", "error"):
                session.is_error = True
                err = obj.get("error", {})
                session.result = (
                    err.get("message", str(err))
                    if isinstance(err, dict)
                    else obj.get("message", str(err))
                )

            # -- legacy event types (older CLI versions) --
            elif typ in ("message", "assistant", "agent"):
                msg = obj.get("message", obj)
                session.messages.append(msg)

                content = msg.get("content", "")
                if isinstance(content, str):
                    chunk.text = content
                    await _maybe_await(on_text, content)
                    if request.verbose_output:
                        _pp_text(content, theme)
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict):
                            btype = blk.get("type")
                            if btype == "text":
                                text = blk.get("text", "")
                                chunk.text = text
                                await _maybe_await(on_text, text)
                                if request.verbose_output:
                                    _pp_text(text, theme)
                            elif btype in (
                                "tool_use",
                                "function_call",
                            ):
                                tu = {
                                    "id": blk.get("id", ""),
                                    "name": blk.get(
                                        "name",
                                        blk.get("function", {}).get("name", ""),
                                    ),
                                    "input": blk.get(
                                        "input",
                                        blk.get("arguments", {}),
                                    ),
                                }
                                chunk.tool_use = tu
                                session.tool_uses.append(tu)
                                await _maybe_await(on_tool_use, tu)
                                if request.verbose_output:
                                    _pp_tool_use(tu, theme)
                yield chunk

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

            elif typ == "done":
                break
    finally:
        await stream.aclose()

    # Reconstruct session.result from chunk texts when the CLI didn't emit a
    # dedicated "response"/"result" event. CodexChunk has no delta flag, so all
    # text chunks are treated as independent parts.
    if not session.result:
        parts = [c.text for c in session.chunks if c.text is not None]
        if parts:
            session.result = "\n".join(parts)
    if session.num_turns is None and session.messages:
        session.num_turns = len(session.messages)
    if session.duration_ms is None:
        session.duration_ms = int(
            (asyncio.get_running_loop().time() - _start_monotonic) * 1000
        )

    await _maybe_await(on_final, session)
    if request.verbose_output:
        _pp_final(session, theme)

    yield session

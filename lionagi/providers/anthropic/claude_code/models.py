# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import codecs
import contextlib
import inspect
import json
import logging
import os
import shutil
import signal
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from lionagi import ln
from lionagi.libs.schema.as_readable import as_readable
from lionagi.providers._cli_paths import (
    check_add_dir_entries_safe,
    check_path_safe,
    contain_paths_in_repo,
)
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk

HAS_CLAUDE_CODE_CLI = False
CLAUDE_CLI = None

if (c := (shutil.which("claude") or "claude")) and shutil.which(c):
    HAS_CLAUDE_CODE_CLI = True
    CLAUDE_CLI = c

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("claude-cli")


# --------------------------------------------------------------------------- types
ClaudePermissionMode = Literal[
    "default",
    "acceptEdits",
    "plan",
    "dontAsk",
    "bypassPermissions",
]
# Backward-compat alias
ClaudePermission = ClaudePermissionMode

ClaudeEffort = Literal["low", "medium", "high", "xhigh", "max"]
ClaudeOutputFormat = Literal["text", "json", "stream-json"]
ClaudeInputFormat = Literal["text", "stream-json"]


__all__ = (
    "ClaudeCodeRequest",
    "stream_claude_code_cli",
)


# --------------------------------------------------------------------------- flag metadata
#
# Each CLI-mappable field carries a ``json_schema_extra`` dict produced by
# ``_cli()``.  The generic ``_build_declarative_args()`` loop reads these
# dicts (sorted by *order*) and emits the correct flag sequence.
#
# kind semantics:
#   value      – ``--flag <str(val)>``
#   bool       – ``--flag`` when truthy, omit otherwise
#   bool_pair  – ``--flag`` when True, ``--neg-flag`` when False, omit when None
#   list_args  – ``--flag arg1 arg2 …`` (one flag, many positional args)
#   json_value – ``--flag '<json>'``   (dict/list serialised to JSON string)
#   repeat     – ``--flag a --flag b`` (flag repeated per item)


def _cli(
    flag: str,
    order: int,
    kind: str = "value",
    *,
    neg_flag: str | None = None,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "cli_flag": flag,
        "cli_order": order,
        "cli_kind": kind,
    }
    if neg_flag:
        d["cli_neg_flag"] = neg_flag
    return d


# --------------------------------------------------------------------------- request model
class ClaudeCodeRequest(BaseModel):
    """Configuration + prompt for a Claude Code CLI invocation.

    Every field annotated with ``_cli(...)`` metadata is automatically
    assembled into CLI arguments by :meth:`as_cmd_args`, sorted by its
    ``order`` value.  A handful of special cases (``permission_mode``,
    ``max_turns``, ``worktree``, ``debug``, legacy ``mcp_servers``) are
    handled with explicit logic after the declarative pass.

    Adding a new CLI flag is one line: declare the field with ``_cli()``
    metadata and the builder picks it up automatically.
    """

    # ── prompt (always required) ──────────────────────────────────
    prompt: str = Field(description="The prompt for Claude Code")

    # ── session management (order 10–19) ──────────────────────────
    continue_conversation: bool = Field(
        default=False,
        json_schema_extra=_cli("--continue", 10, "bool"),
    )
    resume: str | None = Field(
        default=None,
        json_schema_extra=_cli("--resume", 11),
    )
    session_id: str | None = Field(
        default=None,
        json_schema_extra=_cli("--session-id", 12),
    )
    name: str | None = Field(
        default=None,
        json_schema_extra=_cli("--name", 13),
    )
    fork_session: bool = Field(
        default=False,
        json_schema_extra=_cli("--fork-session", 14, "bool"),
    )

    # ── model & runtime (order 20–29) ─────────────────────────────
    model: Literal["sonnet", "opus", "haiku"] | str | None = Field(
        default="sonnet",
        json_schema_extra=_cli("--model", 20),
    )
    effort: ClaudeEffort | None = Field(
        default=None,
        json_schema_extra=_cli("--effort", 21),
    )
    fallback_model: str | None = Field(
        default=None,
        json_schema_extra=_cli("--fallback-model", 22),
    )
    # max_turns is special-cased (+1 offset) — no _cli metadata
    max_turns: int | None = None
    max_budget_usd: float | None = Field(
        default=None,
        json_schema_extra=_cli("--max-budget-usd", 24),
    )

    # ── system prompt (order 30–39) ───────────────────────────────
    system_prompt: str | None = Field(
        default=None,
        json_schema_extra=_cli("--system-prompt", 30),
    )
    system_prompt_file: str | Path | None = Field(
        default=None,
        json_schema_extra=_cli("--system-prompt-file", 31),
    )
    append_system_prompt: str | None = Field(
        default=None,
        json_schema_extra=_cli("--append-system-prompt", 32),
    )
    append_system_prompt_file: str | Path | None = Field(
        default=None,
        json_schema_extra=_cli("--append-system-prompt-file", 33),
    )

    # ── permissions (order 40–49) ─────────────────────────────────
    # permission_mode is special-cased — no _cli metadata
    permission_mode: ClaudePermissionMode | None = None
    allow_dangerously_skip_permissions: bool = Field(
        default=False,
        json_schema_extra=_cli("--allow-dangerously-skip-permissions", 40, "bool"),
    )
    allowed_tools: list[str] | None = Field(
        default=None,
        json_schema_extra=_cli("--allowedTools", 42, "list_args"),
    )
    disallowed_tools: list[str] | None = Field(
        default=None,
        json_schema_extra=_cli("--disallowedTools", 43, "list_args"),
    )
    tools: str | None = Field(
        default=None,
        description="Restrict available tools (comma-separated or 'default')",
        json_schema_extra=_cli("--tools", 44),
    )
    permission_prompt_tool_name: str | None = Field(
        default=None,
        json_schema_extra=_cli("--permission-prompt-tool", 45),
    )

    # ── MCP (order 50–59) ─────────────────────────────────────────
    mcp_config: str | Path | None = Field(
        default=None,
        json_schema_extra=_cli("--mcp-config", 50),
    )
    strict_mcp_config: bool = Field(
        default=False,
        json_schema_extra=_cli("--strict-mcp-config", 51, "bool"),
    )
    # Legacy: if set and mcp_config is absent, serialised to --mcp-config JSON
    mcp_servers: dict[str, Any] = Field(default_factory=dict, exclude=True)

    # ── agents (order 60–69) ──────────────────────────────────────
    agent: str | None = Field(
        default=None,
        json_schema_extra=_cli("--agent", 60),
    )
    agents: dict[str, Any] | None = Field(
        default=None,
        json_schema_extra=_cli("--agents", 61, "json_value"),
    )

    # ── workspace (order 70–79) ───────────────────────────────────
    repo: Path = Field(default_factory=Path.cwd, exclude=True)
    ws: str | None = Field(default=None, exclude=True)
    add_dir: list[str] | None = Field(
        default=None,
        json_schema_extra=_cli("--add-dir", 70, "list_args"),
    )
    # worktree is special-cased (bool vs str) — no _cli metadata
    worktree: str | bool | None = Field(default=None, exclude=True)

    # ── features (order 80–89) ────────────────────────────────────
    chrome: bool | None = Field(
        default=None,
        json_schema_extra=_cli("--chrome", 80, "bool_pair", neg_flag="--no-chrome"),
    )
    disable_slash_commands: bool = Field(
        default=False,
        json_schema_extra=_cli("--disable-slash-commands", 81, "bool"),
    )
    no_session_persistence: bool = Field(
        default=False,
        json_schema_extra=_cli("--no-session-persistence", 82, "bool"),
    )

    # ── output (order 90–99) ──────────────────────────────────────
    output_format: ClaudeOutputFormat = Field(default="stream-json", exclude=True)
    input_format: ClaudeInputFormat | None = Field(
        default=None,
        json_schema_extra=_cli("--input-format", 91),
    )
    # Named json_schema_output to avoid shadowing Pydantic's json_schema
    json_schema_output: dict[str, Any] | str | None = Field(
        default=None,
        json_schema_extra=_cli("--json-schema", 92, "json_value"),
    )
    include_partial_messages: bool = Field(
        default=False,
        json_schema_extra=_cli("--include-partial-messages", 93, "bool"),
    )

    # ── settings & debug (order 100–109) ──────────────────────────
    settings: str | Path | None = Field(
        default=None,
        json_schema_extra=_cli("--settings", 100),
    )
    setting_sources: str | None = Field(
        default=None,
        json_schema_extra=_cli("--setting-sources", 101),
    )
    betas: list[str] | None = Field(
        default=None,
        json_schema_extra=_cli("--betas", 102, "list_args"),
    )
    # debug is special-cased (bool vs str) — no _cli metadata
    debug: str | bool | None = Field(default=None, exclude=True)

    # ── lionagi internal (never become CLI flags) ─────────────────
    auto_finish: bool = Field(
        default=False,
        exclude=True,
        description="Automatically finish the conversation after the first response",
    )
    verbose_output: bool = Field(default=False, exclude=True)
    cli_display_theme: Literal["light", "dark"] = Field(default="light", exclude=True)
    cli_include_summary: bool = Field(default=False, exclude=True)

    # Legacy fields (kept for backward compat, unused in CLI args)
    mcp_tools: list[str] = Field(default_factory=list, exclude=True)
    max_thinking_tokens: int | None = Field(default=None, exclude=True)

    # ── validators ────────────────────────────────────────────────

    @field_validator("permission_mode", mode="before")
    def _norm_perm(cls, v):
        if v in {
            "dangerously-skip-permissions",
            "--dangerously-skip-permissions",
        }:
            return "bypassPermissions"
        return v

    @field_validator("add_dir", mode="before")
    def _norm_add_dir(cls, v):
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("add_dir", mode="after")
    @classmethod
    def _validate_add_dir(cls, v):
        """Reject traversal sequences in add_dir entries.

        Absolute paths are permitted — add_dir is a read-only grant that the
        spawned CLI uses to determine which directories it may read.  The
        orchestration layer sets repo to a per-agent artifact directory and
        add_dir to the project root, which legitimately lies outside the repo.
        Traversal sequences (``..``) are still rejected because they indicate
        an unintended escape rather than a deliberate grant.
        """
        if v is None:
            return v
        return check_add_dir_entries_safe(v, "add_dir")

    @field_validator(
        "system_prompt_file",
        "append_system_prompt_file",
        "mcp_config",
        "settings",
        mode="before",
    )
    @classmethod
    def _validate_path_fields(cls, v):
        """Reject absolute paths and traversal sequences in file path fields."""
        if v is None:
            return v
        check_path_safe(str(v), "system_prompt_file/append_system_prompt_file/mcp_config/settings")
        return v

    @model_validator(mode="before")
    def _validate_message_prompt(cls, data):
        if "prompt" in data and data["prompt"]:
            return data

        if not (msg := data.get("messages")):
            raise ValueError("messages may not be empty")
        resume = data.get("resume")
        continue_conversation = data.get("continue_conversation")

        prompt = ""

        # 1. if resume or continue_conversation, use the last message
        if resume or continue_conversation:
            continue_conversation = True
            prompt = msg[-1]["content"]
            if isinstance(prompt, dict | list):
                prompt = ln.json_dumps(prompt)

        # 2. else, use entire messages except system message
        else:
            prompts = []
            continue_conversation = False
            for message in msg:
                if message["role"] != "system":
                    content = message["content"]
                    prompts.append(
                        ln.json_dumps(content) if isinstance(content, dict | list) else content
                    )

            prompt = "\n".join(prompts)

        # 3. assemble the request data
        data_: dict[str, Any] = dict(
            prompt=prompt,
            resume=resume,
            continue_conversation=bool(continue_conversation),
        )

        # 4. extract system prompt if available
        if msg[0]["role"] == "system":
            if resume or continue_conversation:
                # Continued session: pass as system_prompt (--system-prompt)
                data_["system_prompt"] = msg[0]["content"]
            else:
                # Fresh session: append to Claude Code's built-in prompt
                data_.setdefault("append_system_prompt", msg[0]["content"])

        if "append_system_prompt" in data and data["append_system_prompt"]:
            data_["append_system_prompt"] = str(data.get("append_system_prompt"))

        data_.update(data)
        return data_

    @model_validator(mode="after")
    def _check_constraints(self):
        # Session flag constraints
        if self.resume:
            self.continue_conversation = False
        if self.fork_session and not (self.resume or self.continue_conversation):
            raise ValueError("--fork-session requires --resume or --continue")

        # Permission flag mutual exclusivity
        if self.allow_dangerously_skip_permissions and self.permission_mode == "bypassPermissions":
            raise ValueError(
                "allow_dangerously_skip_permissions and "
                "permission_mode='bypassPermissions' are mutually exclusive"
            )

        # System prompt mutual exclusivity
        if self.system_prompt and self.system_prompt_file:
            raise ValueError("--system-prompt and --system-prompt-file are mutually exclusive")

        # Workspace bounds check for bypassPermissions
        if self.permission_mode == "bypassPermissions":
            repo_resolved = self.repo.resolve()
            cwd_resolved = self.cwd().resolve()
            try:
                cwd_resolved.relative_to(repo_resolved)
            except ValueError:
                raise ValueError(
                    f"With bypassPermissions, workspace must be within "
                    f"repository bounds. Repository: {repo_resolved}, "
                    f"Workspace: {cwd_resolved}"
                ) from None

        # Repo-containment: resolve write-target path fields and reject symlink
        # escapes.  ``add_dir`` is a read-only grant validated separately by
        # ``_validate_add_dir`` — absolute paths there are deliberate grants,
        # not escapes, and must not be rejected here.
        repo_root = self.repo.resolve()
        for fname, fval in (
            ("system_prompt_file", self.system_prompt_file),
            ("append_system_prompt_file", self.append_system_prompt_file),
            ("mcp_config", self.mcp_config),
            ("settings", self.settings),
        ):
            if fval is not None:
                contain_paths_in_repo([str(fval)], repo_root, fname)

        return self

    # ── workspace path ────────────────────────────────────────────

    def cwd(self) -> Path:
        if not self.ws:
            return self.repo

        ws_path = Path(self.ws)

        if ws_path.is_absolute():
            raise ValueError(f"Workspace path must be relative, got absolute: {self.ws}")

        if ".." in ws_path.parts:
            raise ValueError(f"Directory traversal detected in workspace path: {self.ws}")

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
        """Build argument list for the Claude Code CLI.

        Flags are assembled in two passes:

        1. **Declarative** – fields carrying ``_cli()`` metadata are
           collected, sorted by ``order``, and emitted by ``kind``.
        2. **Special cases** – ``permission_mode``, ``max_turns``,
           ``worktree``, ``debug``, and legacy ``mcp_servers`` need
           non-mechanical logic and are handled explicitly.

        The prompt (``-p``) and ``--output-format`` always come first;
        ``--verbose`` is always appended last.
        """
        args: list[str] = [
            "-p",
            self.prompt,
            "--output-format",
            self.output_format,
        ]

        # ── pass 1: declarative flags ──
        args.extend(self._build_declarative_args())

        # ── pass 2: special cases ──

        # permission_mode → --dangerously-skip-permissions OR --permission-mode
        if self.permission_mode:
            if self.permission_mode == "bypassPermissions":
                args.append("--dangerously-skip-permissions")
            else:
                args.extend(["--permission-mode", self.permission_mode])

        # max_turns → +1 offset (CLI counts agentic turns differently)
        if self.max_turns is not None:
            args.extend(["--max-turns", str(self.max_turns + 1)])

        # worktree → True emits bare flag, str emits flag + name
        if self.worktree is not None and self.worktree is not False:
            if isinstance(self.worktree, str):
                args.extend(["--worktree", self.worktree])
            else:
                args.append("--worktree")

        # debug → True emits bare flag, str emits flag + categories
        if self.debug:
            if isinstance(self.debug, str):
                args.extend(["--debug", self.debug])
            else:
                args.append("--debug")

        # Legacy mcp_servers dict → serialise as --mcp-config JSON inline
        if self.mcp_servers and not self.mcp_config:
            args.extend(
                [
                    "--mcp-config",
                    json.dumps({"mcpServers": self.mcp_servers}),
                ]
            )

        # model default – always emit
        if "--model" not in args:
            args.extend(["--model", self.model or "sonnet"])

        # always verbose for structured stream output
        args.append("--verbose")
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
            if val is False and extra.get("cli_kind") != "bool_pair":
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

            elif kind == "bool_pair":
                if val is True:
                    args.append(flag)
                elif val is False and extra.get("cli_neg_flag"):
                    args.append(extra["cli_neg_flag"])

            elif kind == "list_args":
                args.append(flag)
                args.extend(str(v) for v in val)

            elif kind == "json_value":
                serialized = json.dumps(val) if isinstance(val, dict | list) else str(val)
                args.extend([flag, serialized])

            elif kind == "repeat":
                for v in val:
                    args.extend([flag, str(v)])

            else:  # "value"
                args.extend([flag, str(val)])

        return args


# --------------------------------------------------------------------------- chunks & session


ClaudeSession = CLISession


# --------------------------------------------------------------------------- NDJSON stream


# TODO(#1043 Phase 2): migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: ClaudeCodeRequest):
    """
    Yields each JSON object emitted by the *claude-code* CLI.

    • Robust against UTF-8 splits across chunks (incremental decoder).
    • Robust against braces inside strings (uses json.JSONDecoder.raw_decode)
    • Falls back to `json_repair.repair_json` when necessary.
    """
    from json_repair import repair_json

    workspace = request.cwd()
    workspace.mkdir(parents=True, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        CLAUDE_CLI,
        *request.as_cmd_args(),
        cwd=str(workspace),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # isolate from parent's SIGINT
    )
    # Capture PGID immediately — same pattern as Codex (see
    # lionagi/providers/openai/codex/models.py for the full rationale).
    # Guard against mocked subprocesses in tests where proc.pid may not
    # be a real int: a MagicMock.pid coerces to 1 via __int__, and
    # os.killpg(1, SIGTERM) signals init/the CI runner.
    # os.killpg is POSIX-only: on Windows leave _pgid None so the group-kill
    # path is skipped and cleanup falls through to proc.terminate()/kill()
    # instead of raising AttributeError from the finally block.
    _claude_pgid: int | None = (
        proc.pid if hasattr(os, "killpg") and isinstance(proc.pid, int) and proc.pid > 1 else None
    )

    decoder = codecs.getincrementaldecoder("utf-8")()
    json_decoder = json.JSONDecoder()
    buffer: str = ""  # text buffer that may hold >1 JSON objects

    # Bounded stderr drain — without this a stderr-heavy Claude session
    # deadlocks when the OS pipe buffer fills before stdout EOF.
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
        except Exception as exc:
            log.debug("claude stderr drain ended: %s", exc)

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break

            # 1) decode *incrementally* so we never split multibyte chars
            buffer += decoder.decode(chunk)

            # 2) try to peel off as many complete JSON objs as possible
            while buffer:
                buffer = buffer.lstrip()  # remove leading spaces/newlines
                if not buffer:
                    break
                try:
                    obj, idx = json_decoder.raw_decode(buffer)
                    yield obj
                    buffer = buffer[idx:]  # keep remainder for next round
                except json.JSONDecodeError:
                    # incomplete → need more bytes
                    break

        # 3) flush any tail bytes in the incremental decoder
        buffer += decoder.decode(b"", final=True)
        buffer = buffer.strip()
        if buffer:
            try:
                obj, idx = json_decoder.raw_decode(buffer)
                yield obj
            except json.JSONDecodeError:
                try:
                    fixed = repair_json(buffer)
                    yield json.loads(fixed)
                    log.warning("Repaired malformed JSON fragment at stream end")
                except Exception:
                    log.error("Skipped unrecoverable JSON tail: %.120s…", buffer)

        # 4) propagate non-zero exit code, using the drained stderr buffer.
        if await proc.wait() != 0:
            drain_truncated = False
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2.0)
            except asyncio.TimeoutError:
                drain_truncated = True
            except asyncio.CancelledError:
                raise
            err = b"".join(stderr_chunks).decode(errors="replace").strip()
            if drain_truncated:
                err = (err or "") + " [stderr drain timed out]"
            raise RuntimeError(err or "CLI exited non-zero")

    finally:
        # Terminate the whole process group (start_new_session=True
        # above made pgid == proc.pid). Captured up-front so a reap
        # before teardown doesn't make us skip the group kill.
        pgid = _claude_pgid
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

        # Reap the stderr drain task — explicit CancelledError catch
        # because contextlib.suppress(Exception) doesn't catch it.
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001
            pass


# --------------------------------------------------------------------------- SSE route
async def stream_cc_cli_events(request: ClaudeCodeRequest):
    if not CLAUDE_CLI:
        raise RuntimeError("Claude CLI binary not found (npm i -g @anthropic-ai/claude-code)")
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj
    yield {"type": "done"}


print_readable = partial(as_readable, md=True, display_str=True)


def _pp_system(sys_obj: dict[str, Any], theme) -> None:
    txt = (
        f"◼️  **Claude Code Session**  \n"
        f"- id: `{sys_obj.get('session_id', '?')}`  \n"
        f"- model: `{sys_obj.get('model', '?')}`  \n"
        f"- tools: {', '.join(sys_obj.get('tools', [])[:8])}"
        + ("…" if len(sys_obj.get("tools", [])) > 8 else "")
    )
    print_readable(txt, border=False, theme=theme)


def _pp_thinking(thought: str, theme) -> None:
    text = f"""
    🧠 Thinking:
    {thought}
    """
    print_readable(text, border=True, theme=theme)


def _pp_assistant_text(text: str, theme) -> None:
    txt = f"""
    > 🗣️ Claude:
    {text}
    """
    print_readable(txt, theme=theme)


def _pp_tool_use(tu: dict[str, Any], theme) -> None:
    preview = shorten(str(tu["input"]).replace("\n", " "), 130)
    body = f"- 🔧 Tool Use — {tu['name']}({tu['id']}) - input: {preview}"
    print_readable(body, border=False, panel=False, theme=theme)


def _pp_tool_result(tr: dict[str, Any], theme) -> None:
    body_preview = shorten(str(tr["content"]).replace("\n", " "), 130)
    status = "ERR" if tr.get("is_error") else "OK"
    body = f"- 📄 Tool Result({tr['tool_use_id']}) - {status}\n\n\tcontent: {body_preview}"
    print_readable(body, border=False, panel=False, theme=theme)


def _pp_final(sess: CLISession, theme) -> None:
    usage = sess.usage or {}
    cost_str = f"${sess.total_cost_usd:.4f}" if sess.total_cost_usd is not None else "N/A"
    txt = (
        f"### ✅ Session complete - {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC\n"
        f"**Result:**\n\n{sess.result or ''}\n\n"
        f"- cost: **{cost_str}**  \n"
        f"- turns: **{sess.num_turns}**  \n"
        f"- duration: **{sess.duration_ms} ms** (API {sess.duration_api_ms} ms)  \n"
        f"- tokens in/out: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
    )
    print_readable(txt, theme=theme)


# --------------------------------------------------------------------------- internal utils


async def _maybe_await(func, *args, **kw):
    """Call func which may be sync or async."""
    res = func(*args, **kw) if func else None
    if inspect.iscoroutine(res):
        await res


# --------------------------------------------------------------------------- main parser
async def stream_claude_code_cli(  # noqa: C901
    request: ClaudeCodeRequest,
    session: CLISession | None = None,
    *,
    on_system: Callable[[dict[str, Any]], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_text: Callable[[str], None] | None = None,
    on_tool_use: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    on_final: Callable[[CLISession], None] | None = None,
) -> AsyncIterator[StreamChunk | CLISession]:
    """Consume ND-JSON from the Claude Code CLI, yield StreamChunks,
    and populate a CLISession accumulator."""
    if session is None:
        session = CLISession()
    theme = request.cli_display_theme or "light"
    seen_system_ids: set[str] = set()

    stream = stream_cc_cli_events(request)
    try:
        async for obj in stream:
            typ = obj.get("type", "unknown")

            # ------------------------ SYSTEM -----------------------------------
            if typ == "system":
                session.session_id = obj.get("session_id", session.session_id)
                session.model = obj.get("model", session.model)
                await _maybe_await(on_system, obj)
                if request.verbose_output:
                    sid = str(obj.get("session_id", ""))
                    if obj.get("model") and sid not in seen_system_ids:
                        seen_system_ids.add(sid)
                        _pp_system(obj, theme)
                sc = StreamChunk(
                    type="system",
                    metadata={
                        "session_id": obj.get("session_id"),
                        "model": obj.get("model"),
                        "tools": obj.get("tools", []),
                    },
                )
                session.chunks.append(sc)
                yield sc

            # ------------------------ ASSISTANT --------------------------------
            elif typ == "assistant":
                msg = obj["message"]
                session.messages.append(msg)

                for blk in msg.get("content", []):
                    btype = blk.get("type")
                    if btype == "thinking":
                        thought = blk.get("thinking", "").strip()
                        session.thinking_log.append(thought)
                        await _maybe_await(on_thinking, thought)
                        if request.verbose_output:
                            _pp_thinking(thought, theme)
                        sc = StreamChunk(type="thinking", content=thought, metadata=obj)
                        session.chunks.append(sc)
                        yield sc

                    elif btype == "text":
                        text = blk.get("text", "")
                        await _maybe_await(on_text, text)
                        if request.verbose_output:
                            _pp_assistant_text(text, theme)
                        sc = StreamChunk(type="text", content=text, metadata=obj)
                        session.chunks.append(sc)
                        yield sc

                    elif btype == "tool_use":
                        tu = {"id": blk["id"], "name": blk["name"], "input": blk["input"]}
                        session.tool_uses.append(tu)
                        await _maybe_await(on_tool_use, tu)
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

                    elif btype == "tool_result":
                        tr = {
                            "tool_use_id": blk["tool_use_id"],
                            "content": blk["content"],
                            "is_error": blk.get("is_error", False),
                        }
                        session.tool_results.append(tr)
                        await _maybe_await(on_tool_result, tr)
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

            # ------------------------ USER (tool_result containers) ------------
            elif typ == "user":
                msg = obj["message"]
                session.messages.append(msg)
                for blk in msg.get("content", []):
                    if blk.get("type") == "tool_result":
                        tr = {
                            "tool_use_id": blk["tool_use_id"],
                            "content": blk["content"],
                            "is_error": blk.get("is_error", False),
                        }
                        session.tool_results.append(tr)
                        await _maybe_await(on_tool_result, tr)
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

            # ------------------------ RESULT -----------------------------------
            elif typ == "result":
                session.result = obj.get("result", "").strip()
                session.usage = obj.get("usage", {})
                session.total_cost_usd = obj.get("total_cost_usd")
                session.num_turns = obj.get("num_turns")
                session.duration_ms = obj.get("duration_ms")
                session.duration_api_ms = obj.get("duration_api_ms")
                session.is_error = obj.get("is_error", False)

            # ------------------------ DONE -------------------------------------
            elif typ == "done":
                break
    finally:
        await stream.aclose()

    await _maybe_await(on_final, session)
    if request.verbose_output:
        _pp_final(session, theme)

    yield session

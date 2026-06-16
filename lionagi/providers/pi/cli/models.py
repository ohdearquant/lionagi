# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pi coding agent CLI integration — request model, NDJSON stream, session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from dataclasses import field as datafield
from functools import partial
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from lionagi import ln
from lionagi.libs.path_safety import (
    check_paths_safe,
)
from lionagi.libs.path_safety import (
    contain_paths_in_root as contain_paths_in_repo,
)
from lionagi.libs.schema.as_readable import as_readable
from lionagi.ln.concurrency.utils import maybe_await
from lionagi.providers._cli_subprocess import (
    _INHERIT_STDIN,
    build_declarative_cli_args,
    ndjson_from_cli,
)

HAS_PI_CLI = False
PI_CLI = None

if (c := (shutil.which("pi") or "pi")) and shutil.which(c):
    HAS_PI_CLI = True
    PI_CLI = c

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pi-cli")


# --------------------------------------------------------------------------- types

PiThinkingLevel = Literal[
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
]

__all__ = (
    "PiChunk",
    "PiCodeRequest",
    "PiSession",
    "stream_pi_cli",
)

# Model name prefix → pi --provider value.
# Longest prefixes first to avoid false matches.
# Model prefix → pi --provider. Only unambiguous prefixes where the
# model name uniquely identifies the provider. strip=True removes the
# prefix from the model (needed for openrouter/ routing).
# Ambiguous names (llama, gemma, mistral — available on multiple
# providers) are omitted; set provider explicitly or let pi resolve.
_PI_MODEL_PROVIDER_MAP: list[tuple[str, str, bool]] = [
    ("openrouter/", "openrouter", True),
    ("deepseek-", "deepseek", False),
    ("claude-", "anthropic", False),
    ("gpt-", "openai", False),
    ("o1", "openai", False),
    ("o3", "openai", False),
    ("o4", "openai", False),
]


# --------------------------------------------------------------------------- flag metadata


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


class PiCodeRequest(BaseModel):
    """Configuration + prompt for a Pi coding agent CLI invocation."""

    # ── prompt (always required) ──────────────────────────────────
    prompt: str = Field(description="The prompt for Pi CLI")

    # ── provider & model (order 10–19) ────────────────────────────
    provider: str | None = Field(
        default=None,
        description="API provider (google, anthropic, openai, deepseek, etc.)",
        json_schema_extra=_cli("--provider", 10),
    )
    model: str | None = Field(
        default=None,
        description="Model pattern or ID",
        json_schema_extra=_cli("--model", 11),
    )
    api_key: str | None = Field(
        default=None,
        description="API key override (passed via env, not CLI args)",
    )
    thinking: PiThinkingLevel | None = Field(
        default=None,
        description="Reasoning depth level",
        json_schema_extra=_cli("--thinking", 13),
    )

    # ── session (order 20–29) ─────────────────────────────────────
    no_session: bool = Field(
        default=True,
        description="Don't save session (ephemeral)",
        json_schema_extra=_cli("--no-session", 20, "bool"),
    )

    # ── tools (order 30–39) ───────────────────────────────────────
    tools: list[str] | None = Field(
        default=None,
        description="Comma-separated allowlist of tool names",
        json_schema_extra=_cli("--tools", 30, "repeat"),
    )
    no_tools: bool = Field(
        default=False,
        description="Disable all tools",
        json_schema_extra=_cli("--no-tools", 31, "bool"),
    )
    no_builtin_tools: bool = Field(
        default=False,
        description="Disable built-in tools but keep extensions",
        json_schema_extra=_cli("--no-builtin-tools", 32, "bool"),
    )

    # ── prompt control (order 40–49) ──────────────────────────────
    system_prompt: str | None = Field(
        default=None,
        description="Override default system prompt",
        json_schema_extra=_cli("--system-prompt", 40),
    )
    append_system_prompt: list[str] | None = Field(
        default=None,
        description="Append text/file to system prompt",
        json_schema_extra=_cli("--append-system-prompt", 41, "repeat"),
    )
    no_context_files: bool = Field(
        default=False,
        description="Disable AGENTS.md/CLAUDE.md loading",
        json_schema_extra=_cli("--no-context-files", 42, "bool"),
    )

    # ── extensions & skills (order 50–59) ─────────────────────────
    extension: list[str] | None = Field(
        default=None,
        description="Load extension file(s)",
        json_schema_extra=_cli("--extension", 50, "repeat"),
    )
    skill: list[str] | None = Field(
        default=None,
        description="Load skill file or directory",
        json_schema_extra=_cli("--skill", 51, "repeat"),
    )
    no_extensions: bool = Field(
        default=False,
        description="Disable extension discovery",
        json_schema_extra=_cli("--no-extensions", 52, "bool"),
    )
    no_skills: bool = Field(
        default=False,
        description="Disable skill discovery",
        json_schema_extra=_cli("--no-skills", 53, "bool"),
    )

    # ── workspace (not a CLI flag, used for cwd) ──────────────────
    repo: Path = Field(default_factory=Path.cwd, exclude=True)

    # ── file references (@ prefixed) ─────────────────────────────
    file_args: list[str] = Field(
        default_factory=list,
        description="File paths to include (will be @-prefixed)",
    )

    # ── lionagi internal (no CLI flags) ───────────────────────────
    verbose_output: bool = Field(default=False, exclude=True)
    cli_display_theme: Literal["light", "dark"] = Field(default="light", exclude=True)
    cli_include_summary: bool = Field(default=False, exclude=True)

    # ── validators ────────────────────────────────────────────────

    @field_validator("tools", mode="before")
    def _norm_tools(cls, v):
        if isinstance(v, str):
            return [v]
        return v

    @field_validator("file_args", mode="before")
    @classmethod
    def _validate_file_args(cls, v: list) -> list:
        return check_paths_safe(list(v), "file_args", strip_at=True)

    @field_validator("extension", "skill", mode="before")
    @classmethod
    def _validate_path_fields(cls, v):
        if v is None:
            return v
        items = [v] if isinstance(v, str) else list(v)
        return check_paths_safe(items, "extension/skill")

    @model_validator(mode="after")
    def _contain_file_args_in_repo(self) -> PiCodeRequest:
        repo_root = self.repo.resolve()
        contain_paths_in_repo(self.file_args, repo_root, "file_args", strip_at=True)
        if self.extension:
            contain_paths_in_repo(self.extension, repo_root, "extension")
        if self.skill:
            contain_paths_in_repo(self.skill, repo_root, "skill")
        return self

    @model_validator(mode="before")
    @classmethod
    def _infer_provider_from_model(cls, data):
        if data.get("provider"):
            return data
        model = data.get("model") or ""
        for prefix, prov, strip in _PI_MODEL_PROVIDER_MAP:
            if model.startswith(prefix):
                data["provider"] = prov
                if strip:
                    data["model"] = model[len(prefix) :]
                break
        return data

    @model_validator(mode="before")
    @classmethod
    def _validate_message_prompt(cls, data):
        if data.get("prompt"):
            return data

        if not (msg := data.get("messages")):
            raise ValueError("messages or prompt required")

        prompts = []
        for message in msg:
            if message["role"] != "system":
                content = message["content"]
                if isinstance(content, dict | list):
                    prompts.append(ln.json_dumps(content))
                else:
                    prompts.append(content)
            elif message["role"] == "system" and not data.get("system_prompt"):
                data["system_prompt"] = message["content"]

        data["prompt"] = "\n".join(prompts)
        return data

    # ── CLI command builder ───────────────────────────────────────

    def as_cmd_args(self) -> list[str]:
        """Build argument list for ``pi`` invocation: ``-p --mode json [flags] [prompt] [@files...]``."""
        args: list[str] = ["-p", "--mode", "json"]

        # declarative flags
        args.extend(self._build_declarative_args())

        # file references before prompt
        for f in self.file_args:
            args.append(f"@{f}" if not f.startswith("@") else f)

        # Pi's arg parser has no -- terminator support; prompt is
        # positional. Prompts starting with - or @ may be misparsed
        # by Pi's CLI — callers should avoid leading dashes in prompts.
        args.append(self.prompt)

        return args

    # Pi's env var names per provider (from pi-ai/src/env-api-keys.ts)
    _PI_ENV_KEY_MAP: dict[str, str] = {
        "google": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "xai": "XAI_API_KEY",
    }

    def env(self) -> dict[str, str] | None:
        """Environment overrides for the subprocess (API key injection)."""
        if not self.api_key:
            return None
        provider = self.provider or "google"
        key = self._PI_ENV_KEY_MAP.get(provider, f"{provider.upper()}_API_KEY")
        return {key: self.api_key}

    def _build_declarative_args(self) -> list[str]:
        return build_declarative_cli_args(self)


# --------------------------------------------------------------------------- chunks & session


@dataclass
class PiChunk:
    raw: dict[str, Any]
    type: str
    text: str | None = None
    thinking: str | None = None
    tool_use: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None


@dataclass
class PiSession:
    session_id: str | None = None
    model: str | None = None
    chunks: list[PiChunk] = datafield(default_factory=list)
    messages: list[dict[str, Any]] = datafield(default_factory=list)
    tool_uses: list[dict[str, Any]] = datafield(default_factory=list)
    tool_results: list[dict[str, Any]] = datafield(default_factory=list)
    result: str = ""
    usage: dict[str, Any] = datafield(default_factory=dict)
    num_turns: int | None = None
    duration_ms: int | None = None
    is_error: bool = False
    summary: dict | None = None

    def populate_summary(self) -> None:
        self.summary = _extract_summary(self)


def _extract_summary(session: PiSession) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    key_actions: list[str] = []
    file_operations: dict[str, list[str]] = {
        "reads": [],
        "writes": [],
        "edits": [],
    }

    for tu in session.tool_uses:
        name = tu.get("name", "unknown")
        inp = tu.get("input", tu.get("args", {}))
        tool_counts[name] = tool_counts.get(name, 0) + 1

        if name in ("read", "Read", "read_file"):
            fp = inp.get("path", inp.get("file_path", "unknown"))
            file_operations["reads"].append(fp)
            key_actions.append(f"Read {fp}")
        elif name in ("write", "Write", "write_file", "create_file"):
            fp = inp.get("path", inp.get("file_path", "unknown"))
            file_operations["writes"].append(fp)
            key_actions.append(f"Wrote {fp}")
        elif name in ("edit", "Edit", "edit_file", "patch"):
            fp = inp.get("path", inp.get("file_path", "unknown"))
            file_operations["edits"].append(fp)
            key_actions.append(f"Edited {fp}")
        elif name in ("bash", "Bash", "shell"):
            cmd = inp.get("command", inp.get("cmd", ""))
            cmd_short = cmd[:50] + "..." if len(cmd) > 50 else cmd
            key_actions.append(f"Ran: {cmd_short}")
        else:
            key_actions.append(f"Used {name}")

    for op_type in file_operations:
        file_operations[op_type] = list(dict.fromkeys(file_operations[op_type]))

    result_summary = (session.result[:200] + "...") if len(session.result) > 200 else session.result

    return {
        "tool_counts": tool_counts,
        "file_operations": file_operations,
        "key_actions": list(dict.fromkeys(key_actions)) or ["No specific actions"],
        "total_tool_calls": sum(tool_counts.values()),
        "result_summary": result_summary,
        "usage_stats": {
            "num_turns": session.num_turns,
            "duration_ms": session.duration_ms,
            **session.usage,
        },
    }


# --------------------------------------------------------------------------- NDJSON stream


# TODO(#1043 Phase 2): migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: PiCodeRequest):
    if PI_CLI is None:
        raise RuntimeError("Pi CLI not found. Install with: npm i -g @mariozechner/pi-coding-agent")
    env = {**os.environ, **request.env()} if request.env() else None
    cmd = [PI_CLI, *request.as_cmd_args()]
    # Old Pi subprocess did not set stdin; pass _INHERIT_STDIN to preserve that.
    async with contextlib.aclosing(
        ndjson_from_cli(cmd, cwd=request.repo, env=env, stdin=_INHERIT_STDIN)
    ) as stream:
        async for obj in stream:
            yield obj


async def stream_pi_cli_events(request: PiCodeRequest):
    """Stream events from Pi CLI."""
    if not PI_CLI:
        raise RuntimeError("Pi CLI not found (npm i -g @mariozechner/pi-coding-agent)")
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj
    yield {"type": "done"}


print_readable = partial(as_readable, md=True, display_str=True)


def _pp_text(text: str, theme: str = "light") -> None:
    print_readable(f"\n    > Pi:\n    {text}\n", theme=theme)


def _pp_tool_use(tu: dict[str, Any], theme: str = "light") -> None:
    preview = shorten(str(tu.get("input", tu.get("args", {}))).replace("\n", " "), 130)
    print_readable(
        f"- Tool Use — {tu.get('name', tu.get('toolName', 'unknown'))}: {preview}",
        border=False,
        panel=False,
        theme=theme,
    )


def _pp_tool_result(tr: dict[str, Any], theme: str = "light") -> None:
    body = shorten(str(tr.get("result", tr.get("content", ""))).replace("\n", " "), 130)
    status = "ERR" if tr.get("isError", tr.get("is_error")) else "OK"
    print_readable(
        f"- Tool Result — {status}: {body}",
        border=False,
        panel=False,
        theme=theme,
    )


# --------------------------------------------------------------------------- main parser


def _assistant_message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(part for part in parts if part)
    return ""


def _remember_assistant_message(
    session: PiSession,
    message: dict[str, Any] | None,
) -> None:
    if not isinstance(message, dict):
        return

    if model := message.get("model"):
        session.model = model
    if usage := message.get("usage"):
        if isinstance(usage, dict):
            session.usage = usage
    if text := _assistant_message_text(message):
        session.result = text


def _tool_call_from_event(event: dict[str, Any]) -> dict[str, Any]:
    tc = event.get("toolCall", event)
    args = tc.get("arguments", tc.get("args", tc.get("input", {})))
    if isinstance(args, str):
        with contextlib.suppress(json.JSONDecodeError):
            args = json.loads(args)
    return {
        "id": tc.get("id", tc.get("toolCallId", "")),
        "name": tc.get("name", tc.get("toolName", "")),
        "input": args,
    }


def _error_message_from_event(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return (
            error.get("errorMessage")
            or error.get("message")
            or _assistant_message_text(error)
            or str(error)
        )
    return (
        event.get("errorMessage")
        or event.get("message")
        or (str(error) if error is not None else str(event))
    )


async def stream_pi_cli(
    request: PiCodeRequest,
    session: PiSession | None = None,
    *,
    on_text: Callable[[str], None] | None = None,
    on_tool_use: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    on_final: Callable[[PiSession], None] | None = None,
) -> AsyncIterator[PiChunk | dict | PiSession]:
    """Consume JSONL stream from Pi CLI and return a populated PiSession."""
    if session is None:
        session = PiSession()
    theme = request.cli_display_theme or "light"
    _start = asyncio.get_running_loop().time()

    stream = stream_pi_cli_events(request)
    try:
        async for obj in stream:
            typ = obj.get("type", "unknown")
            chunk = PiChunk(raw=obj, type=typ)
            session.chunks.append(chunk)

            if typ == "agent_start":
                yield obj

            elif typ == "agent_end":
                msgs = obj.get("messages", [])
                if msgs:
                    _remember_assistant_message(session, msgs[-1])
                yield chunk

            elif typ == "turn_start":
                yield chunk

            elif typ == "turn_end":
                session.num_turns = (session.num_turns or 0) + 1
                _remember_assistant_message(session, obj.get("message"))
                yield chunk

            elif typ == "message_start":
                yield chunk

            elif typ == "message_update":
                event = obj.get("assistantMessageEvent", {})
                etype = event.get("type", "")

                if etype == "start":
                    _remember_assistant_message(session, event.get("partial"))

                elif etype == "text_delta":
                    text = event.get("delta", "")
                    if text:
                        chunk.text = text
                        if on_text:
                            await maybe_await(on_text(text))
                        if request.verbose_output:
                            _pp_text(text, theme)

                elif etype == "text_end":
                    # Field name verified against pi_cli_events.jsonl fixture:
                    # assistantMessageEvent.content holds the accumulated text.
                    if text := event.get("content", ""):
                        session.result = text

                elif etype == "text_start":
                    pass

                elif etype == "thinking_delta":
                    if text := event.get("delta", ""):
                        chunk.thinking = text

                elif etype == "thinking_end":
                    if text := event.get("content", ""):
                        chunk.thinking = text

                elif etype == "thinking_start":
                    pass

                elif etype == "done":
                    _remember_assistant_message(session, event.get("message"))

                elif etype == "error":
                    session.is_error = True
                    session.result = _error_message_from_event(event)
                    yield chunk
                    continue

                elif etype in ("toolcall_start", "toolcall_delta", "toolcall_end"):
                    if etype == "toolcall_end":
                        # Payload structure verified against pi_cli_events.jsonl fixture:
                        # event.toolCall.{id, name, arguments} (nested under "toolCall" key).
                        tu = _tool_call_from_event(event)
                        chunk.tool_use = tu
                        session.tool_uses.append(tu)
                        if on_tool_use:
                            await maybe_await(on_tool_use(tu))
                        if request.verbose_output:
                            _pp_tool_use(tu, theme)

                yield chunk

            elif typ == "message_end":
                msg = obj.get("message", {})
                if msg:
                    session.messages.append(msg)
                    _remember_assistant_message(session, msg)
                yield chunk

            elif typ == "tool_execution_start":
                tu = {
                    "id": obj.get("toolCallId", ""),
                    "name": obj.get("toolName", ""),
                    "input": obj.get("args", {}),
                }
                chunk.tool_use = tu
                if request.verbose_output:
                    _pp_tool_use(tu, theme)
                yield chunk

            elif typ == "tool_execution_end":
                tr = {
                    "tool_use_id": obj.get("toolCallId", ""),
                    "name": obj.get("toolName", ""),
                    "content": obj.get("result", ""),
                    "is_error": obj.get("isError", False),
                }
                chunk.tool_result = tr
                session.tool_results.append(tr)
                if on_tool_result:
                    await maybe_await(on_tool_result(tr))
                if request.verbose_output:
                    _pp_tool_result(tr, theme)
                yield chunk

            elif typ == "tool_execution_update":
                yield chunk

            elif typ == "start":
                # Top-level AssistantMessageEvent start: carries partial assistant
                # message with initial model/usage info.
                _remember_assistant_message(session, obj.get("partial"))
                yield chunk

            elif typ == "done":
                # Top-level done: may carry final message with model/usage.
                # Both AgentEvent.done (end-of-stream) and a top-level
                # AssistantMessageEvent.done use this type.
                _remember_assistant_message(session, obj.get("message"))
                break

            elif typ == "error":
                session.is_error = True
                session.result = _error_message_from_event(obj)
                yield chunk

            else:
                yield chunk

    finally:
        await stream.aclose()

    if not session.result:
        parts = [c.text for c in session.chunks if c.text is not None]
        if parts:
            session.result = "\n".join(parts)
    if session.num_turns is None and session.messages:
        session.num_turns = len(session.messages)
    if session.duration_ms is None:
        session.duration_ms = int((asyncio.get_running_loop().time() - _start) * 1000)

    if on_final:
        await maybe_await(on_final(session))

    yield session

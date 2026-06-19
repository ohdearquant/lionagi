# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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

from lionagi.libs.path_safety import check_paths_safe, contain_and_resolve
from lionagi.libs.path_safety import contain_paths_in_root as contain_paths_in_repo
from lionagi.libs.schema.as_readable import as_readable
from lionagi.ln.concurrency.utils import maybe_await
from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers._cli_subprocess import (
    _INHERIT_STDIN,
    ndjson_from_cli,
    validate_message_prompt,
)
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from ._config import GeminiCodeConfigs

HAS_GEMINI_CLI = False
GEMINI_CLI = None

if (g := (shutil.which("gemini") or "gemini")) and shutil.which(g):
    HAS_GEMINI_CLI = True
    GEMINI_CLI = g

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gemini-cli")

__all__ = (
    "GeminiChunk",
    "GeminiCodeRequest",
    "GeminiSession",
    "stream_gemini_cli",
)


class GeminiCodeRequest(BaseModel):
    """Configuration + prompt for a Gemini CLI invocation."""

    # -- conversational bits -------------------------------------------------
    prompt: str = Field(description="The prompt for Gemini CLI")
    system_prompt: str | None = None

    # -- repo / workspace ----------------------------------------------------
    repo: Path = Field(default_factory=Path.cwd, exclude=True)
    ws: str | None = None  # sub-directory under repo
    include_directories: list[str] = Field(default_factory=list)

    # -- runtime & safety ----------------------------------------------------
    model: str | None = Field(
        default="gemini-3-flash-preview",
        description=(
            "Gemini model to use. OAuth (gemini-cli) supports: "
            "gemini-3-flash-preview, gemini-3-pro-preview, "
            "gemini-2.5-flash, gemini-2.5-pro. "
            "Note: gemini-3.5-flash and gemini-3.5-flash-preview are not valid "
            "OAuth model IDs and will return a 404."
        ),
    )
    yolo: bool = Field(
        default=False,
        description="Auto-approve all actions without confirmation (--yolo flag)",
    )
    approval_mode: Literal["suggest", "auto_edit", "full_auto"] | None = None
    debug: bool = False
    sandbox: bool = Field(
        default=True,
        description="Run in sandbox mode for safety",
    )

    # -- MCP integration -----------------------------------------------------
    mcp_tools: list[str] = Field(default_factory=list)

    # -- internal use --------------------------------------------------------
    verbose_output: bool = Field(default=False)
    cli_display_theme: Literal["light", "dark"] = Field(default="light")
    cli_include_summary: bool = Field(default=False)

    @model_validator(mode="before")
    @classmethod
    def _validate_message_prompt(cls, data):
        return validate_message_prompt(data)

    @field_validator("include_directories", mode="after")
    @classmethod
    def _validate_include_directories(cls, v):
        return check_paths_safe(v, "include_directories")

    @model_validator(mode="after")
    def _contain_directories_in_repo(self):
        if self.include_directories:
            repo_root = self.repo.resolve()
            contain_paths_in_repo(self.include_directories, repo_root, "include_directories")
        return self

    @model_validator(mode="after")
    def _warn_dangerous_settings(self):
        if self.yolo:
            warnings.warn(
                "GeminiCodeRequest: yolo=True enables auto-approval of ALL actions "
                "without confirmation. This bypasses safety prompts and may allow "
                "unintended file modifications, command execution, or data access. "
                "Only use in trusted, isolated environments.",
                UserWarning,
                stacklevel=4,
            )

        if not self.sandbox:
            warnings.warn(
                "GeminiCodeRequest: sandbox=False disables sandbox protection. "
                "The Gemini CLI will have unrestricted access to the file system "
                "and can execute arbitrary commands. This significantly increases "
                "security risk. Only disable sandbox in controlled environments.",
                UserWarning,
                stacklevel=4,
            )

        return self

    def cwd(self) -> Path:
        if not self.ws:
            return self.repo

        ws_path = Path(self.ws)

        if ws_path.is_absolute():
            raise ValueError(f"Workspace path must be relative, got absolute: {self.ws}")

        if ".." in ws_path.parts:
            raise ValueError(f"Directory traversal detected in workspace path: {self.ws}")

        return contain_and_resolve(ws_path, self.repo)

    def as_cmd_args(self) -> list[str]:
        args: list[str] = ["-p", self.prompt, "--output-format", "stream-json"]

        if self.model:
            args += ["-m", self.model]

        if self.yolo:
            args.append("--yolo")

        if self.approval_mode:
            args += ["--approval-mode", self.approval_mode]

        if self.debug:
            args.append("--debug")

        if not self.sandbox:
            args.append("--no-sandbox")

        for directory in self.include_directories:
            args += ["--include-directories", directory]

        return args


@dataclass
class GeminiChunk:
    raw: dict[str, Any]
    type: str
    # convenience views
    text: str | None = None
    tool_use: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    is_delta: bool = False


@dataclass
class GeminiSession:
    session_id: str | None = None
    model: str | None = None

    # chronological log
    chunks: list[GeminiChunk] = datafield(default_factory=list)

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


def _extract_summary(session: GeminiSession) -> dict[str, Any]:
    tool_counts: dict[str, int] = {}
    tool_details: list[dict[str, Any]] = []
    file_operations: dict[str, list[str]] = {
        "reads": [],
        "writes": [],
        "edits": [],
    }
    key_actions = []

    for tool_use in session.tool_uses:
        tool_name = tool_use.get("name", "unknown")
        tool_input = tool_use.get("input", {})
        tool_id = tool_use.get("id", "")

        tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        tool_details.append({"tool": tool_name, "id": tool_id, "input": tool_input})

        if tool_name in ["read_file", "Read"]:
            file_path = tool_input.get("path", tool_input.get("file_path", "unknown"))
            file_operations["reads"].append(file_path)
            key_actions.append(f"Read {file_path}")

        elif tool_name in ["write_file", "Write"]:
            file_path = tool_input.get("path", tool_input.get("file_path", "unknown"))
            file_operations["writes"].append(file_path)
            key_actions.append(f"Wrote {file_path}")

        elif tool_name in ["edit_file", "Edit"]:
            file_path = tool_input.get("path", tool_input.get("file_path", "unknown"))
            file_operations["edits"].append(file_path)
            key_actions.append(f"Edited {file_path}")

        elif tool_name in ["run_shell_command", "shell", "Bash"]:
            command = tool_input.get("command", "")
            command_summary = command[:50] + "..." if len(command) > 50 else command
            key_actions.append(f"Ran: {command_summary}")

        elif tool_name.startswith("mcp_"):
            operation = tool_name.replace("mcp_", "")
            key_actions.append(f"MCP {operation}")

        else:
            key_actions.append(f"Used {tool_name}")

    key_actions = list(dict.fromkeys(key_actions)) if key_actions else ["No specific actions"]

    for op_type in file_operations:
        file_operations[op_type] = list(dict.fromkeys(file_operations[op_type]))

    result_summary = (session.result[:200] + "...") if len(session.result) > 200 else session.result

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


# TODO(#1043 Phase 2): migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: GeminiCodeRequest):
    if GEMINI_CLI is None:
        raise RuntimeError("Gemini CLI not found. Please install the gemini CLI tool.")
    workspace = request.cwd()
    workspace.mkdir(parents=True, exist_ok=True)
    cmd = [GEMINI_CLI, *request.as_cmd_args()]
    # Gemini CLI 0.46+ refuses to run headless in untrusted directories unless
    # GEMINI_CLI_TRUST_WORKSPACE=true is set in the subprocess environment.
    # Without it the process exits nonzero and stderr carries:
    #   "Gemini CLI is not running in a trusted directory. To proceed, either
    #    use --skip-trust, set the GEMINI_CLI_TRUST_WORKSPACE=true environment
    #    variable, or trust this directory in interactive mode."
    # We prefer the env-var approach over --skip-trust because the flag may not
    # exist on older CLI versions. Inherit the full parent env so that OAuth
    # credentials (~/.config/gemini/) remain accessible to the subprocess.
    env = {**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
    # Old Gemini subprocess did not set stdin; pass _INHERIT_STDIN to preserve that.
    async with contextlib.aclosing(
        ndjson_from_cli(cmd, cwd=workspace, env=env, stdin=_INHERIT_STDIN)
    ) as stream:
        async for obj in stream:
            yield obj


async def stream_gemini_cli_events(request: GeminiCodeRequest):
    """Stream events from Gemini CLI."""
    if not GEMINI_CLI:
        raise RuntimeError("Gemini CLI not found (npm i -g @google/gemini-cli)")
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj
    yield {"type": "done"}


print_readable = partial(as_readable, md=True, display_str=True)


def _pp_text(text: str, theme: str = "light") -> None:
    txt = f"""
    > 🔷 Gemini:
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


def _pp_final(sess: GeminiSession, theme: str = "light") -> None:
    usage = sess.usage or {}
    txt = (
        f"\n### Gemini Session complete\n"
        f"**Result:** {sess.result or ''}\n"
        f"- turns: {sess.num_turns}\n"
        f"- duration: {sess.duration_ms} ms\n"
        f"- tokens: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
    )
    print_readable(txt, theme=theme)


async def stream_gemini_cli(
    request: GeminiCodeRequest,
    session: GeminiSession | None = None,
    *,
    on_text: Callable[[str], None] | None = None,
    on_tool_use: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    on_final: Callable[[GeminiSession], None] | None = None,
) -> AsyncIterator[GeminiChunk | dict | GeminiSession]:
    """Consume the ND-JSON stream from Gemini CLI and return a populated GeminiSession."""
    if session is None:
        session = GeminiSession()
    theme = request.cli_display_theme or "light"
    _start_monotonic = asyncio.get_running_loop().time()

    stream = stream_gemini_cli_events(request)
    try:
        async for obj in stream:
            typ = obj.get("type", "unknown")
            chunk = GeminiChunk(raw=obj, type=typ)
            session.chunks.append(chunk)

            if typ in ("system", "init"):
                session.session_id = obj.get("session_id", obj.get("id"))
                session.model = obj.get("model")
                yield obj

            elif typ in ("message", "assistant"):
                msg = obj.get("message", obj)
                chunk.is_delta = bool(obj.get("delta"))

                # The gemini CLI echoes the user prompt as a role=user message
                # event before emitting the assistant reply.  Skip it so the echo
                # does not pollute session.messages or the result accumulation.
                # Note: this treats all role=user events as prompt echoes, which
                # matches the current single-user stream shape of the gemini CLI.
                role = msg.get("role", "assistant")
                if role == "user":
                    yield chunk
                    continue

                session.messages.append(msg)

                content = msg.get("content", "")
                if isinstance(content, str):
                    chunk.text = content
                    if on_text:
                        await maybe_await(on_text(content))
                    if request.verbose_output:
                        _pp_text(content, theme)
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict):
                            btype = blk.get("type")
                            if btype == "text":
                                text = blk.get("text", "")
                                chunk.text = text
                                if on_text:
                                    await maybe_await(on_text(text))
                                if request.verbose_output:
                                    _pp_text(text, theme)
                            elif btype in ("tool_use", "tool_call"):
                                tu = {
                                    "id": blk.get(
                                        "tool_id",
                                        blk.get("tool_use_id", blk.get("id", "")),
                                    ),
                                    "name": blk.get("tool_name", blk.get("name", "")),
                                    "input": blk.get(
                                        "parameters",
                                        blk.get("input", blk.get("args", {})),
                                    ),
                                }
                                chunk.tool_use = tu
                                session.tool_uses.append(tu)
                                if on_tool_use:
                                    await maybe_await(on_tool_use(tu))
                                if request.verbose_output:
                                    _pp_tool_use(tu, theme)
                yield chunk

            elif typ in ("tool_call", "tool_use"):
                # Real gemini CLI event keys (observed from --output-format stream-json):
                #   id    → "tool_id"   (not "id" or "tool_use_id")
                #   name  → "tool_name" (not "name")
                #   args  → "parameters" (not "input" or "args")
                tu = {
                    "id": obj.get("tool_id", obj.get("tool_use_id", obj.get("id", ""))),
                    "name": obj.get("tool_name", obj.get("name", "")),
                    "input": obj.get("parameters", obj.get("input", obj.get("args", {}))),
                }
                chunk.tool_use = tu
                session.tool_uses.append(tu)
                if on_tool_use:
                    await maybe_await(on_tool_use(tu))
                if request.verbose_output:
                    _pp_tool_use(tu, theme)
                yield chunk

            elif typ == "tool_result":
                # Real gemini CLI event keys (observed from --output-format stream-json):
                #   tool_use_id → "tool_id"   (not "tool_use_id" or "id")
                #   content     → "output"    (not "content" or "result")
                #   is_error    → status != "success" OR explicit is_error flag
                #   Note: any status other than "success" is treated as an error;
                #   this is correct for current CLI versions which emit only
                #   "success" or "error" in the status field of tool_result events.
                _status = obj.get("status", "")
                tr = {
                    "tool_use_id": obj.get("tool_id", obj.get("tool_use_id", obj.get("id", ""))),
                    "content": obj.get("output", obj.get("content", obj.get("result", ""))),
                    "is_error": obj.get("is_error", _status not in ("", "success")),
                }
                chunk.tool_result = tr
                session.tool_results.append(tr)
                if on_tool_result:
                    await maybe_await(on_tool_result(tr))
                if request.verbose_output:
                    _pp_tool_result(tr, theme)
                yield chunk

            elif typ in ("result", "response"):
                session.result = obj.get("result", obj.get("response", "")).strip()
                stats = obj.get("stats") or {}
                session.usage = obj.get("usage", stats)
                session.total_cost_usd = obj.get("total_cost_usd", obj.get("cost"))
                session.num_turns = obj.get("num_turns", obj.get("turns"))
                # Gemini CLI nests duration inside stats; prefer it over the
                # top-level lookup so we capture the actual inference time
                # rather than the Python monotonic wall-clock fallback.
                session.duration_ms = (
                    obj.get("duration_ms")
                    or obj.get("duration")
                    or stats.get("duration_ms")
                    or stats.get("duration")
                )
                session.is_error = obj.get("is_error", obj.get("error") is not None)

            elif typ == "error":
                session.is_error = True
                session.result = obj.get("message", obj.get("error", "Unknown error"))

            elif typ == "done":
                break
    finally:
        await stream.aclose()

    # Populate session.result from streamed chunks when the CLI didn't emit a
    # dedicated "result" event (common for Gemini). Deltas accumulate into one
    # piece; non-delta chunks are kept as separate parts.
    if not session.result:
        parts: list[str] = []
        current_delta: list[str] = []
        for c in session.chunks:
            if c.text is None:
                continue
            if c.is_delta:
                current_delta.append(c.text)
            else:
                if current_delta:
                    parts.append("".join(current_delta))
                    current_delta = []
                parts.append(c.text)
        if current_delta:
            parts.append("".join(current_delta))
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


gemini_log = log


CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-2.5": 1_048_576,
    "gemini-2.0": 1_048_576,
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
}

_GEMINI_HANDLER_PARAMS = (
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_final",
)


@GeminiCodeConfigs.CLI.register
class GeminiCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _GEMINI_HANDLER_PARAMS
    _handler_params = _GEMINI_HANDLER_PARAMS
    _handler_kwarg = "gemini_handlers"
    _request_model = GeminiCodeRequest
    _filter_model_fields = False

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("gemini_handlers", None)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers)

    @property
    def gemini_handlers(self):
        return self._handlers

    @gemini_handlers.setter
    def gemini_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_gemini_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, GeminiSession):
                    continue
                if isinstance(item, dict):
                    typ = item.get("type", "")
                    if typ == "result":
                        yield StreamChunk(
                            type="result",
                            content=item.get("result", ""),
                            metadata=item,
                        )
                    continue
                if isinstance(item, GeminiChunk):
                    if item.text is not None:
                        yield StreamChunk(
                            type="text",
                            content=item.text,
                            is_delta=item.is_delta,
                        )
                    if item.tool_use is not None:
                        tu = item.tool_use
                        yield StreamChunk(
                            type="tool_use",
                            tool_name=tu.get("name"),
                            tool_id=tu.get("id"),
                            tool_input=tu.get("input"),
                        )
                    if item.tool_result is not None:
                        tr = item.tool_result
                        yield StreamChunk(
                            type="tool_result",
                            tool_id=tr.get("tool_use_id"),
                            tool_output=tr.get("content"),
                            is_error=tr.get("is_error", False),
                        )
                    if (
                        item.text is None
                        and item.tool_use is None
                        and item.tool_result is None
                        and item.type == "result"
                    ):
                        yield StreamChunk(
                            type="result",
                            content=item.raw.get("result", ""),
                            metadata=item.raw,
                        )

    async def _call(
        self,
        payload: dict,
        headers: dict,
        **kwargs,
    ):
        responses = []
        request: GeminiCodeRequest = payload["request"]
        session: GeminiSession = GeminiSession()
        handlers = self._runtime_handlers(kwargs)

        async with contextlib.aclosing(stream_gemini_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "done":
                        break
                responses.append(chunk)

        gemini_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")

        parts = []
        current_delta: list[str] = []
        for i in session.chunks:
            if i.text is not None:
                if i.is_delta:
                    current_delta.append(i.text)
                else:
                    if current_delta:
                        parts.append("".join(current_delta))
                        current_delta = []
                    parts.append(i.text)
        if current_delta:
            parts.append("".join(current_delta))

        if parts:
            session.result = "\n".join(parts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)

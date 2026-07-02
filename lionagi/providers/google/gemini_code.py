# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Antigravity CLI (`agy`) backend for the `gemini-code` / `gemini-cli` provider.

Google folded the standalone Gemini Code Assist CLI into the Antigravity suite
(`agy`), so this provider now drives `agy` in headless print mode with
``--output-format json``. That mode emits one terminal JSON object
(``conversation_id``, ``status``, ``response``, ``usage``), which is exactly one
NDJSON record, so the shared ``ndjson_from_cli`` subprocess plumbing consumes it
unchanged. ``conversation_id`` is stored as ``session.session_id`` so it rides
the normal AssistantResponse -> branch -> state.db persistence path and native
resume works via ``--conversation``. The public names, module path, and
provider aliases (``gemini-code`` / ``gemini-cli`` / ``gemini_cli``) are kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from lionagi import ln
from lionagi.libs.path_safety import check_paths_safe, contain_paths_in_root
from lionagi.ln.concurrency.utils import maybe_await
from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers._cli_subprocess import (
    discover_cli,
    ndjson_from_cli,
    print_readable,
    resolve_cli_workspace,
    validate_message_prompt,
)
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from ._config import GeminiCodeConfigs


def _resolve_agy_binary() -> str | None:
    found, path = discover_cli("agy")
    if found:
        return path
    # agy installs to ~/.local/bin, which service contexts (launchd, cron)
    # often lack on PATH even though login shells have it.
    fallback = os.path.expanduser("~/.local/bin/agy")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


AGY_CLI = _resolve_agy_binary()
HAS_AGY = AGY_CLI is not None

log = logging.getLogger("antigravity-cli")

__all__ = (
    "GeminiChunk",
    "GeminiCodeRequest",
    "GeminiSession",
    "stream_gemini_cli",
    "stream_gemini_cli_events",
    "GeminiCLIEndpoint",
    "resolve_agy_model",
)

# agy models expose ~1M-token context (verified against `agy models`).
CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.1-pro": 1_048_576,
    "gemini-3-flash-preview": 1_048_576,
    "gemini-3-pro-preview": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 2_097_152,
}


# --------------------------------------------------------------------------- model mapping

# `agy --model` takes a human-readable name plus an effort qualifier (verified
# against `agy models`). lionagi callers pass legacy Gemini CLI model strings
# (`gemini-3-flash-preview`, `gemini-3-pro-preview`) or bare family names; map
# those onto the exact strings agy understands. Unknown values pass through so
# agy surfaces a clear error rather than us silently forcing a default.
_AGY_MODELS: frozenset[str] = frozenset(
    {
        "Gemini 3.5 Flash (Medium)",
        "Gemini 3.5 Flash (High)",
        "Gemini 3.5 Flash (Low)",
        "Gemini 3.1 Pro (Low)",
        "Gemini 3.1 Pro (High)",
        "Claude Sonnet 4.6 (Thinking)",
        "Claude Opus 4.6 (Thinking)",
        "GPT-OSS 120B (Medium)",
    }
)

_MODEL_ALIASES: dict[str, str] = {
    # flash family -> default medium effort
    "gemini-3-flash-preview": "Gemini 3.5 Flash (Medium)",
    "gemini-3-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-3.5-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-2.5-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-1.5-flash": "Gemini 3.5 Flash (Medium)",
    "gemini-flash": "Gemini 3.5 Flash (Medium)",
    "flash": "Gemini 3.5 Flash (Medium)",
    # pro family -> strong (High): pro is chosen when the caller wants depth
    "gemini-3-pro-preview": "Gemini 3.1 Pro (High)",
    "gemini-3-pro": "Gemini 3.1 Pro (High)",
    "gemini-3.1-pro": "Gemini 3.1 Pro (High)",
    "gemini-2.5-pro": "Gemini 3.1 Pro (High)",
    "gemini-1.5-pro": "Gemini 3.1 Pro (High)",
    "gemini-pro": "Gemini 3.1 Pro (High)",
    "pro": "Gemini 3.1 Pro (High)",
    # cross-family models agy can also route to
    "claude-opus": "Claude Opus 4.6 (Thinking)",
    "opus": "Claude Opus 4.6 (Thinking)",
    "claude-sonnet": "Claude Sonnet 4.6 (Thinking)",
    "sonnet": "Claude Sonnet 4.6 (Thinking)",
    "gpt-oss": "GPT-OSS 120B (Medium)",
    "gpt-oss-120b": "GPT-OSS 120B (Medium)",
}


def resolve_agy_model(model: str | None) -> str:
    """Map a lionagi model spec onto an exact `agy --model` name."""
    if not model:
        return "Gemini 3.5 Flash (Medium)"
    if model in _AGY_MODELS:
        return model
    key = model.strip().lower()
    if key in _MODEL_ALIASES:
        return _MODEL_ALIASES[key]

    # Heuristic fallback: derive (family, effort) from a free-form string so
    # e.g. "gemini-3-pro-low" or "flash high" still resolve.
    is_pro = "pro" in key
    if "low" in key:
        effort = "Low"
    elif "high" in key or "xhigh" in key or "max" in key:
        effort = "High"
    else:
        effort = "Low" if is_pro else "Medium"
    if is_pro:
        return f"Gemini 3.1 Pro ({'High' if effort == 'Medium' else effort})"
    if "flash" in key or "gemini" in key:
        return f"Gemini 3.5 Flash ({effort})"

    # Not recognizable — pass through; agy rejects an invalid name clearly.
    return model


# --------------------------------------------------------------------------- request model


class GeminiCodeRequest(BaseModel):
    """Configuration + prompt for an Antigravity CLI (`agy`) invocation."""

    # -- conversational bits -------------------------------------------------
    prompt: str = Field(description="The prompt for the Antigravity CLI")
    system_prompt: str | None = Field(
        default=None,
        description="Prepended to the prompt — agy print mode has no separate system flag",
    )

    # -- repo / workspace ----------------------------------------------------
    repo: Path = Field(default_factory=Path.cwd, exclude=True)
    ws: str | None = None  # sub-directory under repo
    include_directories: list[str] = Field(default_factory=list)

    # -- runtime & safety ----------------------------------------------------
    model: str | None = Field(
        default="gemini-3.5-flash",
        description=(
            "Model spec; mapped onto an `agy --model` name by resolve_agy_model. "
            "Accepts gemini-3.5-flash, gemini-3.1-pro, legacy Gemini CLI names "
            "(gemini-3-flash-preview, gemini-3-pro-preview), bare family names "
            "(flash/pro), or an exact agy display name."
        ),
    )
    yolo: bool = Field(
        default=False,
        description="Auto-approve all tool permission requests (--dangerously-skip-permissions)",
    )
    sandbox: bool = Field(
        default=False,
        description="Run agy with terminal restrictions enabled (--sandbox)",
    )
    print_timeout: str | None = Field(
        default=None,
        description="Go-duration cap for print-mode wait, e.g. '10m' (--print-timeout; agy default 5m)",
    )

    # -- conversation resume -------------------------------------------------
    resume: str | None = Field(
        default=None,
        description="Resume a previous agy conversation by id (--conversation)",
    )
    continue_recent: bool = Field(
        default=False,
        description="Continue the most recent agy conversation (--continue)",
    )

    # -- internal use --------------------------------------------------------
    verbose_output: bool = Field(default=False)
    cli_display_theme: Literal["light", "dark"] = Field(default="light")
    cli_include_summary: bool = Field(default=False)

    @model_validator(mode="before")
    @classmethod
    def _validate_message_prompt(cls, data):
        if data.get("prompt"):
            return data
        if (data.get("resume") or data.get("continue_recent")) and data.get("messages"):
            data = dict(data)
            content = data["messages"][-1]["content"]
            data["prompt"] = ln.json_dumps(content) if isinstance(content, dict | list) else content
            return data
        return validate_message_prompt(data)

    @field_validator("include_directories", mode="after")
    @classmethod
    def _validate_include_directories(cls, v):
        return check_paths_safe(v, "include_directories")

    @model_validator(mode="after")
    def _contain_directories_in_repo(self):
        if self.include_directories:
            contain_paths_in_root(
                self.include_directories, self.repo.resolve(), "include_directories"
            )
        return self

    def cwd(self) -> Path:
        return resolve_cli_workspace(self.repo, self.ws)

    def full_prompt(self) -> str:
        if self.system_prompt:
            return f"{self.system_prompt}\n\n{self.prompt}"
        return self.prompt

    def as_cmd_args(self) -> list[str]:
        """Build the argv for a headless `agy` print-mode invocation."""
        args: list[str] = [
            "-p",
            self.full_prompt(),
            "--output-format",
            "json",
            "--model",
            resolve_agy_model(self.model),
        ]
        for directory in self.include_directories:
            args += ["--add-dir", str(directory)]
        if self.sandbox:
            args.append("--sandbox")
        if self.yolo:
            args.append("--dangerously-skip-permissions")
        if self.resume:
            args += ["--conversation", self.resume]
        elif self.continue_recent:
            args.append("--continue")
        if self.print_timeout:
            args += ["--print-timeout", self.print_timeout]
        return args


# Public type aliases (preserve the historical import surface).
GeminiSession = CLISession
GeminiChunk = StreamChunk


# --------------------------------------------------------------------------- subprocess seam


async def _ndjson_from_cli(request: GeminiCodeRequest):
    if AGY_CLI is None:
        raise RuntimeError(
            "Antigravity CLI 'agy' not found. Install it and sign in "
            "(it installs to ~/.local/bin/agy); run `agy` once to authenticate."
        )
    cmd = [AGY_CLI, *request.as_cmd_args()]
    # agy resolves relative --add-dir entries against the process cwd and has no
    # '-C'-style flag, so pass the resolved workspace as cwd. Default stdin is
    # DEVNULL — print mode reads nothing from stdin.
    async with contextlib.aclosing(ndjson_from_cli(cmd, cwd=request.cwd())) as stream:
        async for obj in stream:
            yield obj


async def stream_gemini_cli_events(request: GeminiCodeRequest):
    """Stream the raw agy result object(s) from the CLI."""
    if not AGY_CLI:
        raise RuntimeError("Antigravity CLI 'agy' not found (expected at ~/.local/bin/agy).")
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj


# --------------------------------------------------------------------------- pretty-print


def _pp_text(text: str, theme: str = "light") -> None:
    print_readable(f"\n> ✦ Gemini:\n{text}", theme=theme)


def _pp_final(sess: CLISession, theme: str = "light") -> None:
    usage = sess.usage or {}
    txt = (
        f"\n### Antigravity session complete\n"
        f"- conversation: {sess.session_id or 'N/A'}\n"
        f"- turns: {sess.num_turns}\n"
        f"- duration: {sess.duration_ms} ms\n"
        f"- tokens: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)} "
        f"(thinking {usage.get('thinking_tokens', 0)})"
    )
    print_readable(txt, theme=theme)


# --------------------------------------------------------------------------- main parser


async def stream_gemini_cli(
    request: GeminiCodeRequest,
    session: CLISession | None = None,
    *,
    on_text: Callable[[str], None] | None = None,
    on_tool_use: Callable[[dict[str, Any]], None] | None = None,
    on_tool_result: Callable[[dict[str, Any]], None] | None = None,
    on_final: Callable[[CLISession], None] | None = None,
) -> AsyncIterator[StreamChunk | CLISession]:
    """Run agy in json print mode and project its result object into StreamChunks.

    agy's json format surfaces no per-tool events on stdout (they live only in
    the per-session transcript), so ``on_tool_use`` / ``on_tool_result`` are
    accepted for interface parity but do not fire in this transport.
    """
    if session is None:
        session = CLISession()
    theme = request.cli_display_theme or "light"
    _start = asyncio.get_running_loop().time()

    saw_object = False
    async with contextlib.aclosing(stream_gemini_cli_events(request)) as stream:
        async for obj in stream:
            if not isinstance(obj, dict):
                continue
            saw_object = True
            status = str(obj.get("status", "")).upper()
            response = (obj.get("response") or "").strip()

            session.session_id = obj.get("conversation_id") or session.session_id
            session.model = resolve_agy_model(request.model)
            session.result = response
            session.usage = obj.get("usage", {}) or {}
            session.num_turns = obj.get("num_turns")
            duration = obj.get("duration_seconds")
            if duration is not None:
                session.duration_ms = int(float(duration) * 1000)
            session.is_error = status not in ("SUCCESS", "")

            # Session id must be captured before the error branch — a failed
            # turn can still report a live conversation id to resume into.
            if session.session_id:
                sys_sc = StreamChunk(
                    type="system",
                    metadata={"session_id": session.session_id, "model": session.model},
                )
                session.chunks.append(sys_sc)
                yield sys_sc

            if session.is_error:
                # The error chunk must never impersonate the response: a
                # degraded termination (e.g. timeout after a complete final
                # message) would otherwise surface the delivered content AS
                # the error. Lead with the status; keep a bounded detail so
                # quota/auth patterns still classify.
                detail = f": {response[:500]}" if response else ""
                msg = f"agy returned status={status or 'UNKNOWN'}{detail}"
                sc = StreamChunk(type="error", content=msg, is_error=True, metadata=obj)
                session.chunks.append(sc)
                yield sc
            else:
                if on_text and response:
                    await maybe_await(on_text(response))
                if request.verbose_output and response:
                    _pp_text(response, theme)
                sc = StreamChunk(type="text", content=response, metadata=obj)
                session.chunks.append(sc)
                yield sc

                # Terminal usage/turns/duration — the only channel run.py reads
                # provider-reported usage from (persisted onto model_response).
                result_meta: dict[str, Any] = {
                    "model": session.model,
                    "conversation_id": session.session_id,
                    "status": status or "SUCCESS",
                }
                if session.usage:
                    result_meta["usage"] = session.usage
                if session.num_turns is not None:
                    result_meta["num_turns"] = session.num_turns
                if session.duration_ms is not None:
                    result_meta["duration_ms"] = session.duration_ms
                result_sc = StreamChunk(type="result", metadata=result_meta)
                session.chunks.append(result_sc)
                yield result_sc

    if not saw_object and not session.result:
        # rc==0 but nothing parseable (e.g. agy printed a plain-text error line).
        session.is_error = True
        session.result = "agy produced no parseable json response"
        sc = StreamChunk(type="error", content=session.result, is_error=True)
        session.chunks.append(sc)
        yield sc

    if session.num_turns is None:
        session.num_turns = 1
    if session.duration_ms is None:
        session.duration_ms = int((asyncio.get_running_loop().time() - _start) * 1000)

    if on_final:
        await maybe_await(on_final(session))
    if request.verbose_output:
        _pp_final(session, theme)

    yield session


# --------------------------------------------------------------------------- endpoint

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
                if isinstance(item, CLISession):
                    if item.is_error and not any(c.type == "error" for c in item.chunks):
                        yield StreamChunk(
                            type="error",
                            content=item.result or "Antigravity session failed",
                            is_error=True,
                        )
                    continue
                yield item

    async def _call(self, payload: dict, headers: dict, **kwargs):
        request: GeminiCodeRequest = payload["request"]
        session = CLISession()
        handlers = self._runtime_handlers(kwargs)
        responses: list[Any] = []

        async with contextlib.aclosing(stream_gemini_cli(request, session, **handlers)) as gen:
            async for chunk in gen:
                responses.append(chunk)

        log.info("Antigravity session %s finished (%d chunks)", session.session_id, len(responses))
        if not session.result:
            texts = [c.content for c in session.chunks if c.type == "text" and c.content]
            session.result = "\n".join(texts)
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)

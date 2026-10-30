# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from lionagi import ln
from lionagi.libs.path_safety import check_add_dirs_safe as check_add_dir_entries_safe
from lionagi.libs.path_safety import check_path_safe
from lionagi.libs.path_safety import contain_paths_in_root as contain_paths_in_repo
from lionagi.ln.concurrency.utils import maybe_await
from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers._cli_subprocess import (
    Redacted,
    SpawnedProcess,
    build_declarative_cli_args,
    discover_cli,
    env_as_the_spawn_encodes_it,
    ndjson_from_cli,
    print_readable,
    raise_if_env_is_not_a_string_map,
    redact_runtime_fields_in_place,
    resolve_cli_workspace,
)
from lionagi.providers._cli_subprocess import (
    make_cli_flag as _cli,
)
from lionagi.providers._provider_errors import (
    ProviderError,
    classify_provider_error,
)
from lionagi.providers._subagent_admission import DEFAULT_WORKLOAD, SubagentWorkload
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from ._config import ClaudeCodeConfigs

try:
    from json_repair import repair_json as _repair_json

    def _claude_tail_repair(buf: str) -> dict | None:
        fixed = _repair_json(buf)
        return json.loads(fixed) if fixed else None

except ImportError:
    _claude_tail_repair = None

HAS_CLAUDE_CODE_CLI, CLAUDE_CLI = discover_cli("claude")


def resolve_claude_cli() -> str | None:
    """The claude binary to spawn, resolved now rather than at import.

    The module-level constants freeze whatever was true when this module was
    first imported. A long-lived process (an MCP bridge, a Studio daemon) that
    imported while the CLI was missing — or before PATH/HOME were corrected —
    would otherwise refuse every request for its whole lifetime. A successful
    result is cached back onto the module constants so the spawn path stays a
    plain lookup once it has resolved.

    This function is the seam, and ``CLAUDE_CLI`` is only its cache: a test
    that needs the CLI to look absent must patch THIS, because setting the
    constant to None is precisely what triggers re-discovery.

    A cached hit is NOT re-validated. If the binary is deleted or moved after
    a successful resolve, the spawn fails loudly with the path in the error and
    clears on restart — unlike the miss this function exists to fix, which was
    silent and permanent. Re-validating every call would mean a stat (or a PATH
    walk) per spawn and, more importantly, would break the cross-adapter
    contract that stubs a nonexistent binary path onto each provider module
    (``tests/providers/test_cli_adapter_error_conformance.py``): claude alone
    would then need the real CLI installed to run those tests.

    Two scope limits follow from that, and both are deliberate:

    ``PATH`` wins over the fallback dirs *within one discovery*, not against a
    warm cache. Once this has resolved, a later ``PATH`` change does not move
    the answer for the life of the process — see the paragraph above for why
    re-validation is not free.

    Discovery reads the PARENT process environment, never a request's. A
    ``ClaudeCodeRequest`` carries the complete environment for the CHILD, but
    resolving the executable against a request-supplied ``PATH`` would let the
    contents of a request decide which binary this process executes. The child
    gets its environment; choosing the program does not belong to it.
    """
    global HAS_CLAUDE_CODE_CLI, CLAUDE_CLI
    if CLAUDE_CLI:
        return CLAUDE_CLI
    HAS_CLAUDE_CODE_CLI, CLAUDE_CLI = discover_cli("claude")
    return CLAUDE_CLI


def claude_cli_search_locations() -> str:
    """Where the claude lookup actually looked, for an unavailability message.

    Shared by every caller that reports the CLI missing. Installs differ per
    machine, so an error naming only "PATH and the fallback dirs" sends the
    reader to check the wrong PATH — the one in their shell rather than the one
    this process inherited.
    """
    from lionagi.providers._cli_subprocess import cli_fallback_bin_dirs

    return (
        f"not on PATH ({os.environ.get('PATH', '')!r}) and not in the fallback "
        f"dirs ({', '.join(str(d) for d in cli_fallback_bin_dirs())})"
    )


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


__all__ = ("ClaudeCodeRequest", "stream_claude_code_cli", "ClaudeCodeCLIEndpoint")


# --------------------------------------------------------------------------- flag metadata
# json_schema_extra-driven CLI flag protocol — see docs/internals/runtime.md.


# --------------------------------------------------------------------------- request model
#: The tools a ``light`` Claude Code agent runs without: every built-in that writes a file.
LIGHT_DISALLOWED_TOOLS = ("Edit", "Write", "NotebookEdit")


class ClaudeCodeRequest(BaseModel):
    """Configuration + prompt for a Claude Code CLI invocation."""

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
    # Bare "sonnet" is pinned to Sonnet 5.5 — see _pin_sonnet_alias.
    model: Literal["sonnet", "opus", "haiku"] | str | None = Field(
        default="claude-sonnet-5-5",
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
    # Legacy: if set and mcp_config is absent, serialised to --mcp-config JSON.
    # None-vs-{} is a deliberate invariant — see docs/internals/runtime.md.
    mcp_servers: dict[str, Any] | None = Field(default=None, exclude=True)

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

    # ── process (runtime-only, never rendered as CLI args) ─────────
    # Runtime-only, and kept out of the generated JSON schema as well as out
    # of serialization: this model's schema describes the request payload, and
    # a callable has no JSON schema at all — asking for one raises.
    env: SkipJsonSchema[dict[str, str] | None] = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Complete environment for the CLI process. None inherits this "
            "process's environment; a mapping REPLACES it wholesale, so a "
            "caller setting one variable supplies the rest itself."
        ),
    )
    on_spawn: SkipJsonSchema[Callable[[SpawnedProcess], None | Awaitable[None]] | None] = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Called once with a SpawnedProcess as soon as the CLI process "
            "exists, for a caller that must record the identity of a process "
            "it did not spawn itself. May be a coroutine function."
        ),
    )

    @field_validator("env", mode="before")
    @classmethod
    def _env_is_a_string_map(cls, value):
        """Reject a malformed environment without printing anything from it.

        TypeError is the escape from a pydantic ValidationError, which quotes
        the whole rejected input — and a child environment routinely holds
        credentials. Unwrapped here (from the ``Redacted`` a model-level
        validator applied) since this is the code that needs to look at it;
        see docs/internals/providers.md#cli-subprocess-lifecycle.
        """
        if isinstance(value, Redacted):
            value = value.reveal()
        if value is None:
            return value
        if not isinstance(value, Mapping):
            # Not "leave it for pydantic": pydantic quotes the rejected value,
            # and for a wrongly shaped env the value IS the credential.
            raise TypeError(
                f"env must be a mapping of strings to strings, got {type(value).__name__}"
            )
        # Checked and reduced on the plain copy that is returned: a mapping whose iteration
        # and storage disagree would otherwise pass the check with one set of keys and hand
        # pydantic another, and a ``str`` subclass left in place is rejected by pydantic —
        # which runs strict UTF-8 on a subclass and quotes it, a credential among others, into
        # the validation error. The copy is read exactly as the spawn encodes it, so the text
        # returned here is the text the child receives.
        value = dict(value)
        raise_if_env_is_not_a_string_map(value)
        return env_as_the_spawn_encodes_it(value)

    @field_validator("on_spawn", mode="before")
    @classmethod
    def _unwrap_on_spawn(cls, value):
        """Undo the ``Redacted`` wrapping a model-level validator applied.

        A bound callback carries its receiver into its own ``repr``, so a
        supervisor holding a credential would print it from an unrelated
        field's error if left wrapped past this point.
        """
        if isinstance(value, Redacted):
            value = value.reveal()
        if value is None or callable(value):
            return value
        # Rejected HERE rather than handed back for pydantic to reject, because
        # unwrapping is what re-exposes the value: pydantic renders a failing
        # field's input, and an object with a credential-bearing __repr__ is
        # exactly the shape this carrier exists to keep out of an error. A
        # TypeError names the type and never the object.
        raise TypeError(f"on_spawn must be callable, got {type(value).__name__}")

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
    workload: SubagentWorkload = Field(
        default=DEFAULT_WORKLOAD,
        exclude=True,
        description=(
            "'light' removes the Edit, Write and NotebookEdit tools and counts against the "
            "total and claude agent caps; Bash stays, and Claude Code has no filesystem "
            "sandbox, so this is a tool-list boundary only: a command run through Bash can "
            "still write anywhere or start other agents. 'heavy' (the default) keeps the "
            "configured tools and also counts against the heavy cap."
        ),
    )

    # Legacy fields (kept for backward compat, unused in CLI args)
    mcp_tools: list[str] = Field(default_factory=list, exclude=True)
    max_thinking_tokens: int | None = Field(default=None, exclude=True)

    # ── validators ────────────────────────────────────────────────

    @field_validator("model", mode="before")
    def _pin_sonnet_alias(cls, v):
        return "claude-sonnet-5-5" if v == "sonnet" else v

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
        if v is None:
            return v
        check_path_safe(str(v), "system_prompt_file/append_system_prompt_file/mcp_config/settings")
        return v

    @model_validator(mode="before")
    def _validate_message_prompt(cls, data):
        # First, before anything here can raise: this validator holds the whole
        # raw input, and pydantic quotes a failing validator's input verbatim.
        redact_runtime_fields_in_place(data)
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

        # Repo-containment on write-target path fields; add_dir is excluded
        # (read-only grant, validated separately) — see docs/internals/runtime.md.
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
        return resolve_cli_workspace(self.repo, self.ws)

    # ── CLI command builder ───────────────────────────────────────

    def as_cmd_args(self) -> list[str]:
        """Build argument list for the Claude Code CLI."""
        args: list[str] = [
            "-p",
            self.prompt,
            "--output-format",
            self.output_format,
        ]

        source = self
        if self.workload == "light":
            # Read once and rendered as one list: the declarative renderer emits the flag a
            # single time, and a second --disallowedTools would depend on how the CLI merges.
            denied = list(self.disallowed_tools or [])
            denied += [t for t in LIGHT_DISALLOWED_TOOLS if t not in denied]
            source = self.model_copy(update={"disallowed_tools": denied})
        args.extend(source._build_declarative_args())

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

        # `is not None` (not truthiness) — an explicit {} must still force
        # zero MCP servers rather than falling back to CLI discovery.
        if self.mcp_servers is not None and not self.mcp_config:
            args.extend(
                [
                    "--mcp-config",
                    json.dumps({"mcpServers": self.mcp_servers}),
                ]
            )

        # model default – always emit
        if "--model" not in args:
            args.extend(["--model", self.model or "claude-sonnet-5-5"])

        # always verbose for structured stream output
        args.append("--verbose")
        return args

    def _build_declarative_args(self) -> list[str]:
        return build_declarative_cli_args(self)


# --------------------------------------------------------------------------- chunks & session


ClaudeSession = CLISession


# --------------------------------------------------------------------------- NDJSON stream


# TODO: migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: ClaudeCodeRequest):
    workspace = request.cwd()
    workspace.mkdir(parents=True, exist_ok=True)
    cmd = [resolve_claude_cli(), *request.as_cmd_args()]
    # The CLI states a refusal (a session limit, say) on stdout as an error result and then exits
    # nonzero with nothing on stderr; the exit alone would read as a crash, so the result it gave
    # rides the raised error, classified.
    refusal: dict | None = None
    try:
        # tail_repair recovers a malformed-but-repairable final JSON object instead of dropping it.
        async with contextlib.aclosing(
            ndjson_from_cli(
                cmd,
                cwd=workspace,
                env=request.env,
                tail_repair=_claude_tail_repair,
                on_spawn=request.on_spawn,
                engine="claude",
                workload=request.workload,
            )
        ) as stream:
            async for obj in stream:
                if isinstance(obj, dict) and obj.get("type") == "result":
                    refusal = obj if obj.get("is_error") else None
                yield obj
    except RuntimeError as exc:
        if refusal is None or isinstance(exc, ProviderError):
            raise
        said = str(refusal.get("result") or "").strip() or "an error result with no text"
        status = refusal.get("api_error_status")
        if status is not None:
            said = f"{said} (api_error_status {status})"
        message = f"Claude Code said: {said}; {exc}"
        # The status is the result's own field and decides quota; the text never does beside it.
        raise classify_provider_error(message, status=status) from exc


# --------------------------------------------------------------------------- SSE route
async def stream_cc_cli_events(request: ClaudeCodeRequest):
    if not resolve_claude_cli():
        # Name where we actually looked: installs differ per machine, so a
        # package-manager prescription here invites installing a second copy.
        raise RuntimeError(
            f"Claude CLI binary 'claude' {claude_cli_search_locations()}. "
            "Install the Claude Code CLI or start this process with a PATH "
            "that reaches the existing install."
        )
    async with contextlib.aclosing(_ndjson_from_cli(request)) as stream:
        async for obj in stream:
            yield obj
    yield {"type": "done"}


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
        f"### ✅ Session complete - {ln.now_utc().isoformat(timespec='seconds')} UTC\n"
        f"**Result:**\n\n{sess.result or ''}\n\n"
        f"- cost: **{cost_str}**  \n"
        f"- turns: **{sess.num_turns}**  \n"
        f"- duration: **{sess.duration_ms} ms** (API {sess.duration_api_ms} ms)  \n"
        f"- tokens in/out: {usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
    )
    print_readable(txt, theme=theme)


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
    """Consume ND-JSON from the Claude Code CLI, yield StreamChunks and populate a CLISession."""
    if session is None:
        session = CLISession()
    theme = request.cli_display_theme or "light"
    seen_system_ids: set[str] = set()
    # Tracks ids whose partial deltas were already forwarded, so the later
    # complete ``assistant`` envelope can populate session.messages without
    # duplicating content into the StreamChunk stream.
    partial_message_ids: set[str] = set()
    current_partial_message_id: str | None = None
    partial_without_message_id = False

    stream = stream_cc_cli_events(request)
    try:
        async for obj in stream:
            typ = obj.get("type", "unknown")

            # ------------------------ SYSTEM -----------------------------------
            if typ == "system":
                session.session_id = obj.get("session_id", session.session_id)
                session.model = obj.get("model", session.model)
                if on_system:
                    await maybe_await(on_system(obj))
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

            # ------------------------ PARTIAL ASSISTANT ------------------------
            elif typ == "stream_event":
                event = obj.get("event") or {}
                event_type = event.get("type")

                if event_type == "message_start":
                    message = event.get("message") or {}
                    current_partial_message_id = message.get("id")
                    continue

                if event_type == "message_stop":
                    current_partial_message_id = None
                    continue

                if event_type != "content_block_delta":
                    continue

                delta = event.get("delta") or {}
                delta_type = delta.get("type")
                content: str | None = None
                chunk_type: str | None = None
                callback = None

                if delta_type == "text_delta":
                    content = delta.get("text")
                    chunk_type = "text"
                    callback = on_text
                elif delta_type == "thinking_delta":
                    content = delta.get("thinking")
                    chunk_type = "thinking"
                    callback = on_thinking

                if not content or chunk_type is None:
                    continue

                message_id = current_partial_message_id
                if message_id:
                    partial_message_ids.add(message_id)
                else:
                    partial_without_message_id = True

                if callback:
                    await maybe_await(callback(content))
                sc = StreamChunk(
                    type=chunk_type,
                    content=content,
                    is_delta=True,
                    metadata=obj,
                )
                session.chunks.append(sc)
                yield sc

            # ------------------------ ASSISTANT --------------------------------
            elif typ == "assistant":
                msg = obj["message"]
                session.messages.append(msg)
                msg_id = msg.get("id")
                has_forwarded_partials = bool(
                    (msg_id and msg_id in partial_message_ids)
                    or (not msg_id and partial_without_message_id)
                )

                for blk in msg.get("content", []):
                    btype = blk.get("type")
                    if btype == "thinking":
                        thought = blk.get("thinking", "").strip()
                        session.thinking_log.append(thought)
                        if has_forwarded_partials:
                            continue
                        if on_thinking:
                            await maybe_await(on_thinking(thought))
                        if request.verbose_output:
                            _pp_thinking(thought, theme)
                        sc = StreamChunk(type="thinking", content=thought, metadata=obj)
                        session.chunks.append(sc)
                        yield sc

                    elif btype == "text":
                        text = blk.get("text", "")
                        if has_forwarded_partials:
                            continue
                        if on_text:
                            await maybe_await(on_text(text))
                        if request.verbose_output:
                            _pp_assistant_text(text, theme)
                        sc = StreamChunk(type="text", content=text, metadata=obj)
                        session.chunks.append(sc)
                        yield sc

                    elif btype == "tool_use":
                        tu = {"id": blk["id"], "name": blk["name"], "input": blk["input"]}
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

                    elif btype == "tool_result":
                        tr = {
                            "tool_use_id": blk["tool_use_id"],
                            "content": blk["content"],
                            "is_error": blk.get("is_error", False),
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

            # ------------------------ RESULT -----------------------------------
            elif typ == "result":
                result = obj.get("result")
                session.result = result.strip() if isinstance(result, str) else ""
                session.usage = obj.get("usage", {})
                # modelUsage is whole-agent-tree (includes Task-tool subagent
                # spawns); the flat `usage` above is top-level-loop-only.
                session.model_usage = obj.get("modelUsage") or {}
                session.total_cost_usd = obj.get("total_cost_usd")
                session.num_turns = obj.get("num_turns")
                session.duration_ms = obj.get("duration_ms")
                session.duration_api_ms = obj.get("duration_api_ms")
                session.is_error = obj.get("is_error", False)
                session.error_status = obj.get("api_error_status")
                # An error result names its failure in ``errors``; its ``result``,
                # when it has one, can be the turn's own text, so the errors the
                # event lists are the result whenever any says something.
                errors = obj.get("errors")
                if session.is_error and isinstance(errors, list):
                    said = [e.get("message") if isinstance(e, dict) else e for e in errors]
                    said = [e.strip() for e in said if isinstance(e, str) and e.strip()]
                    if said:
                        session.result = "; ".join(dict.fromkeys(said))

                # Terminal usage/cost/turns/duration — the only channel run.py
                # reads provider-reported usage from.
                result_meta: dict[str, Any] = {}
                if session.usage:
                    result_meta["usage"] = session.usage
                if session.model_usage:
                    result_meta["model_usage"] = session.model_usage
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

            # ------------------------ DONE -------------------------------------
            elif typ == "done":
                break
    finally:
        await stream.aclose()

    if on_final:
        await maybe_await(on_final(session))
    if request.verbose_output:
        _pp_final(session, theme)

    yield session


cc_log = log


CONTEXT_WINDOWS: dict[str, int] = {
    "fable-5-1": 1_000_000,
    "opus-5-5": 1_000_000,
    "sonnet-5-5": 1_000_000,
    "haiku-5-5": 1_000_000,
    "opus-4-7": 1_000_000,
    "opus-4-6": 1_000_000,
    "opus": 1_000_000,
    "sonnet-5": 1_000_000,
    "sonnet-4-6": 1_000_000,
    "sonnet-4-5": 200_000,
    "sonnet": 1_000_000,
    "haiku-4-5": 200_000,
    "haiku": 200_000,
    "fable-5": 1_000_000,
    "fable": 1_000_000,
}

_CLAUDE_HANDLER_PARAMS = (
    "on_thinking",
    "on_text",
    "on_tool_use",
    "on_tool_result",
    "on_system",
    "on_final",
)


@ClaudeCodeConfigs.CLI.register
class ClaudeCodeCLIEndpoint(AgenticHandlersMixin, AgenticEndpoint):
    transport_arg_keys = _CLAUDE_HANDLER_PARAMS
    _handler_params = _CLAUDE_HANDLER_PARAMS
    _handler_kwarg = "claude_handlers"
    _request_model = ClaudeCodeRequest
    _runtime_state_fields = ("env", "on_spawn")
    # Claude Code streams a "system" event as soon as the CLI session starts,
    # well before the run completes — see stream_cc_cli() above.
    streams_first_output_early = True

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("claude_handlers", None)
        # Before super(), which deep-copies a supplied config: see
        # take_supplied_runtime_state for what that copy does to a bound method.
        supplied = self.take_supplied_runtime_state(config, kwargs)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers, supplied=supplied)

    @property
    def claude_handlers(self):
        return self._handlers

    @claude_handlers.setter
    def claude_handlers(self, value: dict):
        self._set_handlers(value)

    async def stream(self, request, **kwargs) -> AsyncIterator[StreamChunk]:
        handlers = self._runtime_handlers(kwargs)
        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]
        async with contextlib.aclosing(stream_claude_code_cli(request_obj, **handlers)) as gen:
            async for item in gen:
                if isinstance(item, CLISession):
                    # Turns the session's terminal verdict into a chunk — see
                    # docs/internals/providers.md#a-session-terminal-error-must-be-turned-back-into-a-stream-chunk.
                    if item.is_error and not any(c.type == "error" for c in item.chunks):
                        yield StreamChunk(
                            type="error",
                            content=item.result or "Claude Code session failed",
                            is_error=True,
                            # the run classifies by this, ahead of the text (classify_provider_error)
                            metadata=(
                                {"error_status": item.error_status}
                                if item.error_status is not None
                                else {}
                            ),
                        )
                    continue
                yield item

    async def _call(
        self,
        payload: dict,
        headers: dict,  # type: ignore[unused-argument]
        **kwargs,
    ):
        responses = []
        request: ClaudeCodeRequest = payload["request"]
        session: CLISession = CLISession()
        system_meta: dict | None = None
        _cancelled = False
        handlers = self._runtime_handlers(kwargs)

        try:
            async with contextlib.aclosing(
                stream_claude_code_cli(request, session, **handlers)
            ) as gen:
                async for chunk in gen:
                    if isinstance(chunk, StreamChunk) and chunk.type == "system":
                        system_meta = chunk.metadata
                    responses.append(chunk)
        except BaseException:
            _cancelled = True
            raise

        if (
            not _cancelled
            and request.auto_finish
            and responses
            and not isinstance(responses[-1], CLISession)
        ):
            # Deep copy, then put env/on_spawn back BY IDENTITY: a deep-copied
            # bound method rebinds to a copied receiver, so the continuation's
            # supervisor callback would otherwise silently stop being the
            # original one that holds the durable process accounting.
            req2 = request.model_copy(deep=True)
            for _runtime_field in ("env", "on_spawn"):
                object.__setattr__(req2, _runtime_field, getattr(request, _runtime_field))
            req2.prompt = "Please provide a the final result message only"
            req2.max_turns = 1
            req2.continue_conversation = True
            if system_meta:
                req2.resume = system_meta.get("session_id")

            async with contextlib.aclosing(stream_claude_code_cli(req2, session)) as gen2:
                async for chunk in gen2:
                    responses.append(chunk)
                    if isinstance(chunk, CLISession):
                        break
        cc_log.info(f"Session {session.session_id} finished with {len(responses)} chunks")
        if session.is_error:
            # Classified on the result event's own text: the assistant text joined
            # below can name a different failure than the one that ended the run.
            if session.session_id:
                self.session_id = session.session_id
            raise classify_provider_error(
                session.result or "Claude Code session failed",
                status=session.error_status,
            )
        texts = []
        for sc in session.chunks:
            if sc.type == "text" and sc.content is not None:
                texts.append(sc.content)

        if session.result and (not texts or session.result.strip() != texts[-1].strip()):
            texts.append(session.result)

        session.result = "\n".join(texts)
        # Unconditional: the bounded activity record carries no tool inputs,
        # so it needs no redaction and costs a few keys. The full summary,
        # which does carry inputs, stays opt-in.
        session.populate_activity()
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)

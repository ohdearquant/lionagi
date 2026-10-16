# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import unicodedata
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from textwrap import shorten
from typing import Any, Literal

import toml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from lionagi._errors import CLIInputRefusedError
from lionagi.libs.path_safety import check_add_dirs_safe as check_add_dir_entries_safe
from lionagi.libs.path_safety import check_path_safe, check_paths_safe
from lionagi.libs.path_safety import contain_paths_in_root as contain_paths_in_repo
from lionagi.ln.concurrency.utils import maybe_await
from lionagi.providers._agentic_handlers import AgenticHandlersMixin
from lionagi.providers._cli_subprocess import (
    Redacted,
    SpawnedProcess,
    build_declarative_cli_args,
    discover_cli,
    ndjson_from_cli,
    print_readable,
    raise_if_env_is_not_a_string_map,
    redact_runtime_fields_in_place,
    resolve_cli_workspace,
    reveal_redacted,
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

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10: the declared `tomli`, a TOML 1.0 parser like tomllib
    # (`toml` 0.10 does not normalize whitespace around dotted key parts)
    import tomli as _tomllib  # type: ignore[no-redef]

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

# The effort words lionagi itself produces. Deliberately a tuple, not a
# Literal on the request fields: codex reaches non-OpenAI models through
# `model_providers` in ~/.codex/config.toml, each with its own effort
# vocabulary, and the value is emitted verbatim as `-c reasoning_effort=<val>`
# for codex to interpret — a closed set here would reject valid configs.
CODEX_REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

__all__ = ("CodexCodeRequest", "stream_codex_cli", "CodexCLIEndpoint")

# The integrations Codex attaches from the signed-in account rather than from
# a config file: its app connectors and its plugins. They come with the auth
# store, so no `mcp_servers` override reaches them, and an app connector acts
# as the account (a GitHub merge, a sent mail, a record in a hosted store).
_ACCOUNT_INTEGRATION_FEATURES = ("apps", "plugins")


# --------------------------------------------------------------------------- -c value serialization
# codex's `-c key=value` parses `value` as TOML; a JSON-style dump is not
# valid TOML and either mis-parses or silently produces the wrong value. See
# docs/internals/providers.md#codex-c-override-toml-serialization.
# A bare TOML key. Matched with `fullmatch`: `match` with `$` accepts a key ending in
# a newline (`"abc\n"`), which renders bare and which TOML refuses.
_TOML_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

# The CLI's mid-stream retry announcement, e.g. "Reconnecting... 1/5 (stream
# disconnected before completion: ...)". A non-matching message falls through
# to the terminal-error branch (fail-closed on a CLI wording change). The
# trailing "(" is load-bearing: it excludes terminal messages that merely
# begin with the retry prefix, e.g. "Reconnecting... 5/5 failed".
_RECONNECT_NOTICE_RE = re.compile(r"^\s*Reconnecting\.\.\.\s*\d+/\d+\s*\(")

#: The longest turn input the Codex CLI takes, in characters, as its own refusal states it
#: (``turn/start`` answers ``input_too_large`` with ``max_chars``). Past it the CLI refuses only
#: after the process has started; the request refuses first.
CODEX_MAX_TURN_INPUT_CHARS = 1_048_576

# The system prompt and each `-c` override reach the CLI as single command-line arguments.
# macOS refuses an argument list, environment included, past ARG_MAX (1,048,576 bytes from
# `getconf ARG_MAX`); Linux refuses one argument past MAX_ARG_STRLEN, 32 pages (131,072 bytes
# with 4 KiB pages). So an argument of this many bytes does not start the CLI on macOS or on
# Linux with 4 KiB pages. A shorter one can still be refused there when the process starts,
# with the operating system's error. The bound is the same on every platform, so it also
# refuses an argument Linux with larger pages would take.
_CLI_ARGUMENT_MAX_BYTES = 1_048_576


def _refuse_oversized_argument(argument: bytes, what: str) -> None:
    size = len(argument)
    if size >= _CLI_ARGUMENT_MAX_BYTES:
        raise ValueError(
            f"{what} reaches the Codex CLI as one command-line argument of {size:,} bytes, "
            f"and an argument of {_CLI_ARGUMENT_MAX_BYTES:,} bytes or more does not start the "
            "CLI on macOS or on Linux with 4 KiB pages."
        )


def _refuse_oversized_command(cmd: list[str]) -> None:
    """Refuse ``cmd`` when any argument after the program holds a NUL or is
    ``_CLI_ARGUMENT_MAX_BYTES`` bytes or more."""
    for position, argument in enumerate(cmd[1:], start=1):
        flag = cmd[position - 1] if position > 1 else ""
        if flag == "-c":
            what = f"The -c override {argument.split('=', 1)[0][:80]!r}"
        elif flag.startswith("-"):
            what = f"The value of {flag}"
        else:
            what = f"Command-line argument {position}"
        # Measured in the bytes exec passes: os.fsencode is how an argument is encoded for
        # exec, so a path holding a byte that is not UTF-8 counts that byte once.
        encoded = os.fsencode(argument)
        if b"\x00" in encoded:
            raise ValueError(_nul_refusal_message(what))
        _refuse_oversized_argument(encoded, what)


def _contain_override(key: str, value: Any) -> tuple[bool, Any]:
    """Whether account-integration containment keeps the override ``key=value``, and the
    value it keeps: an override under `features.apps` or `features.plugins` is dropped, and
    those keys leave an inline `features` table, which is dropped when nothing is left."""
    path = _override_key_path(key)
    if path[0] == "features":
        if len(path) >= 2 and path[1] in _ACCOUNT_INTEGRATION_FEATURES:
            return False, value
        if len(path) == 1 and isinstance(value, Mapping):
            value = {k: v for k, v in value.items() if k not in _ACCOUNT_INTEGRATION_FEATURES}
            if not value:
                return False, value
    return True, value


def _refuse_text_strict_utf8_cannot_encode(what: str) -> ValueError:
    """The refusal for text holding a character strict UTF-8 cannot encode — a lone
    surrogate, which a child process cannot decode. Returned rather than raised so the
    caller names the input at the point it encodes it."""
    return ValueError(
        f"{what} holds a character strict UTF-8 cannot encode: a lone surrogate cannot "
        "be passed to the Codex CLI."
    )


def _strict_utf8_failure(text: str, what: str) -> ValueError | None:
    """The refusal for ``text`` when strict UTF-8 cannot encode it, else None.

    Returned rather than raised: the ``UnicodeEncodeError`` the encode raises holds the
    whole text in its ``.object``, and a ``raise`` inside that handler would chain the
    refusal to it, so the caller raises the returned error after the handler has ended.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return _refuse_text_strict_utf8_cannot_encode(what)
    return None


def _nul_refusal_message(what: str) -> str:
    return f"{what} holds a NUL character, which no command-line argument can hold."


def _argument_text_failure(text: str, what: str) -> ValueError | None:
    """The refusal for ``text`` that reaches the CLI as a command-line argument, else
    None: strict UTF-8 must encode it, and it may not hold a NUL, which exec refuses."""
    failure = _strict_utf8_failure(text, what)
    if failure is None and "\x00" in text:
        failure = ValueError(_nul_refusal_message(what))
    return failure


def _redact_codex_runtime_fields_in_place(data) -> None:
    """Wrap ``env``, ``on_spawn`` and ``config_overrides`` in a raw request mapping.

    ``config_overrides`` joins the runtime-only fields: codex reads it as ``-c
    key=value`` pairs, and a caller's overrides carry the same kind of secret a child
    environment does. Called at the top of every model-level ``mode="before"`` validator
    of ``CodexCodeRequest``, the one place that holds pydantic's whole raw input on a
    failing validation. The shared helper keeps its behavior for the other providers,
    whose request models render no overrides. See
    docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    redact_runtime_fields_in_place(data)
    if not isinstance(data, Mapping) or "config_overrides" not in data:
        return
    overrides = data["config_overrides"]
    if overrides is None or isinstance(overrides, Redacted):
        return
    try:
        data["config_overrides"] = Redacted(overrides, "config_overrides")
    except TypeError:
        raise TypeError(
            f"{type(data).__name__} is read-only, so config_overrides cannot be replaced "
            "before validation. It carries `-c` overrides, and a validation error would "
            "render the mapping verbatim. Pass a mutable mapping."
        ) from None


# U+0000-U+001F and U+007F: the control characters a TOML basic string may not hold
# raw, and the ones the `toml` package renders wrongly (or refuses with IndexError).
_TOML_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _toml_string(value: str) -> str:
    r"""Render ``value`` as a single-line TOML basic string.

    Rendered here rather than through the ``toml`` package: that encoder renders a
    control character wrongly (``"a\x01b"`` comes back as ``"ax01b"``, a different value)
    and raises IndexError on a leading one, which would escape where containment drops
    the override. Every control character is written as a ``\uXXXX`` escape, which TOML
    reads back as the same character; every other character is kept as it is.
    """
    # Strict UTF-8 first: a lone surrogate has no encoding a child process can decode,
    # so it is refused here rather than reaching the CLI as a raw byte. Escapes are
    # added after the quote and backslash escapes so the escape they introduce survives.
    value.encode("utf-8")
    body = value.replace("\\", "\\\\").replace('"', '\\"')
    body = _TOML_CONTROL_CHAR_RE.sub(lambda match: f"\\u{ord(match.group()):04X}", body)
    return f'"{body}"'


def _refuse_oversized_cli_inputs(
    prompt: str,
    system_prompt: str | None = None,
    config_overrides: dict[str, Any] | None = None,
    *,
    text_fields: Sequence[tuple[str, str]] | None = None,
    contained: bool = False,
) -> None:
    """Refuse an input the Codex CLI cannot take, before any process starts.

    The turn input in characters and each argument in the bytes exec passes, plus the
    characters strict UTF-8 cannot carry at all. ``text_fields`` pairs each field's name
    with the text ``as_cmd_args`` renders into an argument for the fields whose argument
    is not built here, so a lone surrogate in one of them is refused by name and every
    entry of a repeated field is measured. Each refusal
    is raised after the handler that produced it ends: the ``UnicodeEncodeError`` an
    encode raises holds the whole text in its ``.object``, and raising inside the
    handler would chain the refusal to it.
    """
    failure: ValueError | None = None
    measured: list[tuple[str, bytes]] = []
    if len(prompt) > CODEX_MAX_TURN_INPUT_CHARS:
        raise ValueError(
            f"the Codex turn input is {len(prompt):,} characters, past the CLI's limit of "
            f"{CODEX_MAX_TURN_INPUT_CHARS:,}. A long branch sends its rendered history as "
            "the input: start a new session and hand it the earlier result as a file."
        )
    # The turn input travels over the child's stdin, where it is encoded strictly; a
    # lone surrogate is refused here rather than after the process has started.
    failure = _strict_utf8_failure(prompt, "The turn prompt")
    # The system prompt's and each override's argument, measured as as_cmd_args builds
    # them, so the refusal names them; every argument is measured again where the
    # process starts.
    if failure is None and system_prompt:
        failure = _strict_utf8_failure(system_prompt, "The system prompt")
        if failure is None:
            argument = f"developer_instructions={toml_override_value(system_prompt)}"
            measured.append(("The system prompt", argument.encode("utf-8")))
    for index, (key, value) in enumerate((config_overrides or {}).items()):
        if failure is not None:
            break
        if not isinstance(key, str):
            # Reached only by a request built without validation, whose field validator
            # refuses such a key; named by position and type, never by the key.
            failure = ValueError(
                f"The config override entry {index} has a key that is not a string "
                f"({type(key).__name__})."
            )
            break
        # Encoded before containment reads the key, so a value the encoder refuses
        # refuses the request whether or not containment would drop it.
        try:
            encoded = toml_override_value(value)
        except TypeError:
            continue  # as_cmd_args refuses the value itself
        except UnicodeEncodeError:
            # A str value with a lone surrogate: named here rather than left to the
            # codec's own message, which names neither the override nor the request.
            failure = _refuse_text_strict_utf8_cannot_encode(
                f"The value of the config override {key[:80]!r}"
            )
            break
        if contained:
            try:
                kept, kept_value = _contain_override(key, value)
            except ValueError:
                continue  # as_cmd_args refuses the key itself
            if not kept:
                continue
            if kept_value is not value:
                try:
                    encoded = toml_override_value(kept_value)
                except TypeError:
                    continue  # as_cmd_args refuses the value itself
                except UnicodeEncodeError:
                    failure = _refuse_text_strict_utf8_cannot_encode(
                        f"The value of the config override {key[:80]!r}"
                    )
                    break
        argument = f"{key}={encoded}"
        # The value is rendered with every control character escaped, so a NUL left in
        # the argument is the key's.
        failure = _argument_text_failure(argument, f"The config override key {key[:80]!r}")
        if failure is None:
            measured.append((f"The config override {key[:80]!r}", argument.encode("utf-8")))
    # The text fields that reach the CLI as an argument of their own, each encoded
    # strictly and checked for a NUL under its own name.
    for what, text in text_fields or ():
        if failure is not None:
            break
        failure = _argument_text_failure(text, what)
        if failure is None:
            measured.append((what, text.encode("utf-8")))
    if failure is not None:
        raise failure
    for what, argument in measured:
        _refuse_oversized_argument(argument, what)


def _toml_scalar(value: str | int | float | bool) -> str:
    """Render a single TOML scalar (or its quoted-string form): a str through
    ``_toml_string``, numbers through the ``toml`` encoder so its literal stays
    spec-correct."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    # toml.dumps({"x": value}) always renders "x = <literal>\n" for scalars;
    # strip the synthetic key so only the literal remains.
    dumped = toml.dumps({"x": value})
    return dumped[len("x = ") :].rstrip("\n")


def _override_key_path(key: str) -> list[str]:
    """The parts of a `-c key=value` override's key, read as the TOML dotted
    key the CLI reads it as: a quoted part loses its quotes and its escapes, so
    `features."\\u0061pps"` names the same switch as `features.apps`. A key
    TOML cannot read is refused here, by name, rather than handed to the CLI."""
    try:
        table: Any = _tomllib.loads(f"{key} = 1")
        path: list[str] = []
        while isinstance(table, dict):
            ((part, table),) = table.items()
            path.append(part)
    except Exception as e:
        raise ValueError(f"config override key {key!r} is not a TOML key: {e}") from None
    return path


def _toml_key(key: str) -> str:
    return key if _TOML_BARE_KEY_RE.fullmatch(key) else _toml_scalar(key)


def toml_override_value(value: Any) -> str:
    """Serialize ``value`` as a TOML value literal suitable for the
    right-hand side of a codex `-c key=value` override (an inline table for
    dicts, a TOML array for lists, spec-correct scalars otherwise)."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        pairs = ", ".join(f"{_toml_key(k)} = {toml_override_value(v)}" for k, v in value.items())
        return "{ " + pairs + " }"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_override_value(v) for v in value) + "]"
    if isinstance(value, str | int | float | bool):
        return _toml_scalar(value)
    raise TypeError(f"Unsupported codex `-c` override value type: {type(value)!r}")


def _linked_worktree_git_writable_roots(cwd: Path) -> tuple[Path, ...]:
    """Return the narrow Git write roots needed by a linked worktree."""
    resolved_cwd = cwd.resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],  # noqa: S607
            cwd=resolved_cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    paths = result.stdout.splitlines()
    if result.returncode != 0 or len(paths) != 2 or not all(paths):
        return ()

    def _resolve(value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (resolved_cwd / path).resolve()

    git_dir, common_dir = map(_resolve, paths)
    if git_dir == common_dir:
        return ()

    common_roots = (common_dir / "objects", common_dir / "refs", common_dir / "logs")
    return (git_dir, *(path for path in common_roots if path.is_dir()))


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
        raise_if_env_is_not_a_string_map(value)
        return dict(value)

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
        # Rejected here, not handed back to pydantic: pydantic renders a
        # failing field's input, which would re-expose the unwrapped value.
        raise TypeError(f"on_spawn must be callable, got {type(value).__name__}")

    @field_validator("config_overrides", mode="before")
    @classmethod
    def _unwrap_config_overrides(cls, value):
        """Undo the ``Redacted`` wrapping a model-level validator applied.

        A `-c` override routinely holds a token the CLI authenticates with, so
        the wrapper keeps it out of a validation error's rendering of the
        whole input; this is the field validator that then reads it. A value
        that is not a mapping, or a key that is not a string, is refused
        without printing any of it.
        """
        if isinstance(value, Redacted):
            value = value.reveal()
        if not isinstance(value, Mapping):
            # TypeError, not ValueError: pydantic quotes a ValueError's rejected
            # input verbatim into the validation error.
            raise TypeError(f"config_overrides must be a mapping, got {type(value).__name__}")
        # Refused by position and type, as env's keys are: pydantic would quote a
        # key it cannot validate as a string, and a key can hold a secret as a
        # value can. A bytes key is refused too rather than decoded.
        unnamed = [
            f"entry {index} (key is {type(key).__name__})"
            for index, key in enumerate(value)
            if not isinstance(key, str)
        ]
        if unnamed:
            raise TypeError(
                "config_overrides keys must be strings; these entries are not: "
                + ", ".join(unnamed)
            )
        return value

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
    contain_account_integrations: bool = Field(
        default=False,
        description=(
            "Turn the signed-in account's `apps` and `plugins` features off "
            "(`--disable apps --disable plugins`) and drop either from "
            "`enable_features` and from `features` entries in `config_overrides`. "
            "Off by default: the request reaches whatever the account and the "
            "config give it. Lifted by naming `account_connectors`."
        ),
    )
    account_connectors: list[str] | None = Field(
        default=None,
        description=(
            "The signed-in account's app connectors this request is for. A grant: "
            "never written down with the endpoint's config, so a restored branch "
            "runs without it until its caller hands it again. Codex has no switch "
            "for one connector, so naming any leaves both features as the account "
            "and config set them, even under `contain_account_integrations`: the "
            "request then reaches every connector the account has, and the names "
            "record what it is for. A blank or non-string name is refused."
        ),
    )

    # ── reasoning (order 75, emitted as -c overrides) ───────────
    reasoning_effort: str | None = Field(
        default=None,
        description=(
            "Reasoning effort level (emitted as -c reasoning_effort=<val>). "
            f"lionagi produces one of {', '.join(CODEX_REASONING_EFFORTS)}, but any "
            "string is accepted so that a model configured behind codex can be "
            "given the effort vocabulary it expects."
        ),
    )
    plan_mode_reasoning_effort: str | None = Field(
        default=None,
        description=(
            "Plan-mode reasoning effort (emitted as -c plan_mode_reasoning_effort=<val>). "
            "Open to any string for the same reason as reasoning_effort."
        ),
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

    @classmethod
    def _resolve_config_profile(cls, values):
        """Turn a model that names a codex config profile into the profile.

        Idempotent by construction: a real model id carries dots or a slash,
        which the bare-name check rejects, so a CLI-resolved model passes
        through unchanged. Not a validator of its own — called explicitly
        from the validator below, before the effort clamp, since the clamp's
        ceilings are keyed on the model. See
        docs/internals/providers.md#codex-config-profile-resolution-and-effort-clamping.

        The lookup is done against the CHILD's environment: when a request
        carries a replacement `env`, the profile that decides which model runs
        lives in the codex home that env names. Resolving against ours would
        read a profile file the child never opens and put a different model on
        the command line.
        """
        from ._codex_profile import resolve_codex_config_profile

        model = values.get("model")
        if not isinstance(model, str) or not model:
            return values
        env = reveal_redacted(values.get("env"))
        resolved = resolve_codex_config_profile(model, env if isinstance(env, Mapping) else None)
        if resolved is None:
            return values
        profile_model, profile_overrides = resolved
        values["model"] = profile_model
        if profile_overrides:
            merged = dict(profile_overrides)
            caller_overrides = reveal_redacted(values.get("config_overrides")) or {}
            merged.update(caller_overrides)
            # Wrapped again: `values` is the raw input a later validator, or a
            # validation error, holds, and the merged mapping carries both the
            # profile's settings and the caller's.
            values["config_overrides"] = Redacted(merged, "config_overrides")
        return values

    @model_validator(mode="before")
    @classmethod
    def _resolve_profile_then_clamp_effort(cls, values):
        """Resolve a config-profile model, THEN clamp effort against it.

        One validator running two steps in a written order rather than relying
        on pydantic's (reverse-definition) validator ordering — see
        docs/internals/providers.md#codex-config-profile-resolution-and-effort-clamping.
        `values` omits `model` when the caller relies on the field default
        (Pydantic v2 applies defaults after `mode="before"` validators), so
        fall back to the field's own default.

        An UNSET `reasoning_effort` is resolved here too, and this is the only
        place that closes it for every caller: omitting the flag does not mean
        "no effort", it means the codex CLI reads `model_reasoning_effort` from
        the global config — a value the user tuned for whichever model they run
        by hand, applied to a model whose ceiling may be lower. Every other
        construction seam (CLI builders, agent factory, Studio operator, a bare
        `Branch()`) reaches the CLI through this request model, so the fix
        belongs here rather than at each of them.
        """
        from lionagi.service.providers import (
            _clamp_codex_effort,
            codex_unset_effort_default,
        )

        _redact_codex_runtime_fields_in_place(values)
        values = cls._resolve_config_profile(values)

        model = values.get("model", cls.model_fields["model"].default)
        for key in ("reasoning_effort", "plan_mode_reasoning_effort"):
            effort = values.get(key)
            if isinstance(effort, str):
                values[key] = _clamp_codex_effort(effort, model)

        # `is None`, not `not isinstance(..., str)`: only an ABSENT or null
        # effort is unset. A non-string is malformed input; it falls through
        # to the field's own `str | None` validation, which rejects it, rather
        # than being replaced by the derived default.
        if values.get("reasoning_effort") is None:
            # The child's own environment decides which config codex reads, and
            # `env` replaces that environment wholesale when it is set. By this
            # point an earlier before-validator has wrapped it for redaction,
            # so it must be revealed to be read at all.
            env = reveal_redacted(values.get("env"))
            derived = codex_unset_effort_default(
                model,
                reveal_redacted(values.get("config_overrides")),
                env if isinstance(env, Mapping) else None,
            )
            if derived is not None:
                values["reasoning_effort"] = derived
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

    @field_validator("account_connectors", mode="before")
    @classmethod
    def _validate_account_connectors(cls, v):
        # Any name at all lifts the containment, so a value that does not read
        # as names is refused here, as the agent profile refuses it, rather than
        # taken as a grant.
        if v is None:
            return None
        if not isinstance(v, list | tuple) or any(
            not isinstance(name, str) or not name.strip() for name in v
        ):
            raise ValueError("account_connectors must be a list of connector names")
        return list(dict.fromkeys(name.strip() for name in v))

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
        _redact_codex_runtime_fields_in_place(data)
        return validate_message_prompt(data)

    @model_validator(mode="after")
    def _refuse_a_turn_input_past_the_cli_limit(self):
        """Refuse, at construction, any input the Codex CLI cannot take.

        Every text field ``as_cmd_args`` renders into an argument is measured here, so a
        refusal names the field rather than arriving from the spawn as a codec error or a
        raw byte. Raised OUTSIDE the handler that caught the ValueError: pydantic wraps a
        ValueError from a validator in a ValidationError whose `.json()` and `.errors()`
        carry the whole input, so the refusal is a CLIInputRefusedError, which is not a
        ValueError and passes through unwrapped. Raising it after the handler ends leaves
        `__context__` None, so the chain the ValueError itself carried is not reachable
        through the refusal either.
        """
        refusal: CLIInputRefusedError | None = None
        try:
            _refuse_oversized_cli_inputs(
                self.prompt,
                self.system_prompt,
                self.config_overrides,
                text_fields=self._argument_text_fields(),
                contained=self._contains_account_integrations(),
            )
        except ValueError as exc:
            refusal = CLIInputRefusedError(str(exc))
        if refusal is not None:
            raise refusal
        return self

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

    @model_validator(mode="after")
    def _reject_ambiguous_approval_policy(self):
        if (
            self.ask_for_approval
            and not self.bypass_approvals
            and not self.full_auto
            and any(
                key == "approval_policy" or key.startswith("approval_policy.")
                for key in self.config_overrides
            )
        ):
            raise ValueError(
                "ask_for_approval cannot be combined with config_overrides entries "
                "for approval_policy; choose one approval policy source"
            )
        return self

    # ── workspace path ────────────────────────────────────────────

    def cwd(self) -> Path:
        return resolve_cli_workspace(self.repo, self.ws)

    # ── CLI command builder ───────────────────────────────────────

    def as_cmd_args(self) -> list[str]:
        """Build argument list for ``codex exec`` subcommand."""
        args: list[str] = ["exec", "--json"]
        cwd = self.cwd()

        args.extend(self._build_declarative_args())

        for git_root in _linked_worktree_git_writable_roots(cwd):
            if str(git_root) not in (self.add_dir or []):
                args.extend(["--add-dir", str(git_root)])

        # Approval & sandbox: mutually exclusive hierarchy
        if self.bypass_approvals:
            args.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.full_auto:
            # Codex 0.147.0 removed `--full-auto`, turning the earlier deprecation
            # warning into a hard argv error. Its deprecation notice prescribes
            # `--sandbox workspace-write`, which keeps the same sandbox posture.
            args.extend(["--sandbox", "workspace-write"])
        else:
            if self.ask_for_approval:
                # The config key has the same mode vocabulary as the former CLI
                # flag and remains available in builds that no longer expose it.
                args.extend(
                    [
                        "-c",
                        f"approval_policy={toml_override_value(self.ask_for_approval)}",
                    ]
                )
            if self.sandbox:
                args.extend(["-s", self.sandbox])

        # Web search: old `--search` flag was removed upstream (2026-04);
        # express intent via the `tool_search` feature flag instead.
        if self.search:
            args.extend(["--enable", "tool_search"])
        else:
            args.extend(["--disable", "tool_search"])

        # Codex CLI has no --system-prompt flag; uses -c developer_instructions. The
        # value is read as TOML like every other `-c` value, so it is written as a TOML
        # string: written raw, a prompt that parses as TOML ("true", "42", '"quoted"')
        # would reach codex as that value instead of as its own text.
        if self.system_prompt:
            args.extend(["-c", f"developer_instructions={toml_override_value(self.system_prompt)}"])

        # Encoded like every other `-c` override below. While these fields were
        # a closed Literal of eight bare words, emitting them unencoded was
        # safe by construction; now that any string is accepted, what keeps an
        # arbitrary value inside its own override is codex's own leniency about
        # the right-hand side, which is not a contract lionagi owns.
        if self.reasoning_effort:
            args.extend(["-c", f"reasoning_effort={toml_override_value(self.reasoning_effort)}"])
        if self.plan_mode_reasoning_effort:
            args.extend(
                [
                    "-c",
                    "plan_mode_reasoning_effort="
                    f"{toml_override_value(self.plan_mode_reasoning_effort)}",
                ]
            )

        if self.fast_mode:
            args.extend(["-c", "service_tier=fast"])

        for image in self.images:
            args.extend(["-i", image])

        containing = self._contains_account_integrations()
        for key, value in self.config_overrides.items():
            # Containment removes overrides under `features.apps` and
            # `features.plugins`, plus those keys from an inline `features`
            # table. Config profiles can contribute either form alongside the
            # caller. Drop them here rather than relying on flag order.
            if containing:
                kept, value = _contain_override(key, value)
                if not kept:
                    continue
            args.extend(["-c", f"{key}={toml_override_value(value)}"])

        # Working directory (always emit)
        args.extend(["-C", str(cwd)])

        # Prompt goes to stdin, not argv (a whole conversation can exceed the
        # OS arg-length limit). `-` is codex's stdin marker; it stays after
        # `--` so it's parsed as the prompt argument, never as a flag.
        args.extend(["--", "-"])

        return args

    def _contains_account_integrations(self) -> bool:
        return self.contain_account_integrations and not self.account_connectors

    def _argument_text_fields(self) -> list[tuple[str, str]]:
        """The fields ``as_cmd_args`` renders into an argument of their own, by name.

        Read by the construction and spawn checks so a lone surrogate in any of them is
        refused by name rather than reaching the CLI as a raw byte. Path fields are left
        out: a path may legitimately hold a byte that is not UTF-8, and ``os.fsencode``
        is what measures those at the process boundary.
        """
        fields: list[tuple[str, str]] = []
        for name in (
            "model",
            "profile",
            "sandbox",
            "ask_for_approval",
            "color",
            "reasoning_effort",
            "plan_mode_reasoning_effort",
        ):
            text = getattr(self, name)
            if isinstance(text, str):
                fields.append((name, text))
        for name in ("enable_features", "disable_features"):
            for feature in getattr(self, name) or ():
                if isinstance(feature, str):
                    fields.append((name, feature))
        return fields

    def _build_declarative_args(self) -> list[str]:
        if not self._contains_account_integrations():
            return build_declarative_cli_args(self)
        disabled = list(self.disable_features or ())
        settled = self.model_copy(
            update={
                "enable_features": [
                    f for f in self.enable_features or () if f not in _ACCOUNT_INTEGRATION_FEATURES
                ]
                or None,
                "disable_features": disabled
                + [f for f in _ACCOUNT_INTEGRATION_FEATURES if f not in disabled],
            }
        )
        return build_declarative_cli_args(settled)


CodexSession = CLISession


# --------------------------------------------------------------------------- NDJSON stream


# TODO: migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: CodexCodeRequest):
    if CODEX_CLI is None:
        raise RuntimeError("Codex CLI not found. Install with: npm i -g @openai/codex")
    # model_copy(update=...) skips validation, so check again where inputs reach the process.
    _refuse_oversized_cli_inputs(
        request.prompt,
        request.system_prompt,
        request.config_overrides,
        text_fields=request._argument_text_fields(),
        contained=request._contains_account_integrations(),
    )
    cmd = [CODEX_CLI, *request.as_cmd_args()]
    _refuse_oversized_command(cmd)
    # Do NOT pass cwd here — Codex CLI already gets the workspace via -C <repo>;
    # setting both double-resolves to 'repo/repo'. See docs/internals/runtime.md.
    async with contextlib.aclosing(
        ndjson_from_cli(
            cmd,
            env=request.env,
            stdin_data=request.prompt,
            on_spawn=request.on_spawn,
        )
    ) as stream:
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


#: The most of one tool result this provider keeps, and of a command, a file change or a
#: function call's arguments it echoes as the call's input, in characters. No Codex thread is
#: resumed here: each turn renders the branch's history into its prompt, and the tool calls
#: and results recorded here can be part of it, so one broad search recorded whole could fill
#: a later prompt on its own. The bound is per record and on the recorded text: a turn with
#: many results still renders all of them, and the rendering can be longer than the text it
#: renders: up to ten times where a control character or a lone surrogate makes it escape the
#: text (a character outside the Basic Multilingual Plane is then written as ten), and a string
#: of many lines nested deep repeats its indentation on every line, so it grows with the number
#: of lines times the depth.
CODEX_TOOL_OUTPUT_MAX_CHARS = 20_000

#: How far a cut moves to keep a character with its marks before it gives up and cuts where
#: the bound falls.
_CUT_BACKOFF_CHARS = 32


def _continues_previous(text: str, i: int) -> bool:
    """Whether the character at ``i`` belongs with the one before it: a character with a
    nonzero combining class, a zero-width joiner, the text or emoji variation selector
    (U+FE0E, U+FE0F), a skin-tone modifier, or anything after a joiner."""
    ch = text[i]
    return (
        unicodedata.combining(ch) != 0
        or ch in "\u200d\ufe0e\ufe0f"
        or "\U0001f3fb" <= ch <= "\U0001f3ff"
        or (i > 0 and text[i - 1] == "\u200d")
    )


def _escaped_length(text: str) -> int:
    """The length of ``text`` as JSON writes it inside a string, escapes included."""
    return len(json.dumps(text, ensure_ascii=False)) - 2


def _longest_within(text: str, budget: int, *, from_end: bool) -> int:
    """How many characters from the start (or the end) of ``text`` fit in ``budget`` as JSON
    writes them. An escape is never shorter than its character, so the answer is at most
    ``budget``; that is tried first, and text with nothing to escape needs no search."""

    def fits(count: int) -> bool:
        part = text[len(text) - count :] if from_end else text[:count]
        return _escaped_length(part) <= budget

    lo, hi = 0, min(budget, len(text))
    if fits(hi):
        return hi
    hi -= 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _cut_text(text: str, limit: int, *, escaped: bool = False) -> str:
    """``text`` when its length is at most ``limit``; otherwise its head and tail, half of
    ``limit`` each, with a line between them naming how many characters were cut. With
    ``escaped``, a length is that of the text as JSON writes it inside a string, so a
    character JSON escapes counts as its escape does (a quote or a newline two, a control
    character six).

    Each cut moves up to ``_CUT_BACKOFF_CHARS`` characters so as not to separate a
    character from the ones ``_continues_previous`` joins to it, and cuts where the bound
    falls when the run is longer than that. Other clusters (spacing marks, Hangul jamo,
    tag sequences, flag pairs) can be split."""
    if len(text) <= limit and (not escaped or _escaped_length(text) <= limit):
        return text
    half = max(limit, 0) // 2
    if escaped:
        head_len = _longest_within(text, half, from_end=False)
        tail_len = _longest_within(text, half, from_end=True)
    else:
        head_len = tail_len = half
    head_end = head_len
    while (
        head_end > 0
        and head_len - head_end < _CUT_BACKOFF_CHARS
        and _continues_previous(text, head_end)
    ):
        head_end -= 1
    if _continues_previous(text, head_end):
        head_end = head_len
    tail_start = len(text) - tail_len
    while (
        tail_start < len(text)
        and tail_start - (len(text) - tail_len) < _CUT_BACKOFF_CHARS
        and _continues_previous(text, tail_start)
    ):
        tail_start += 1
    if tail_start < len(text) and _continues_previous(text, tail_start):
        tail_start = len(text) - tail_len
    return (
        f"{text[:head_end]}\n[output truncated: {tail_start - head_end:,} of {len(text):,} "
        f"characters omitted here]\n{text[tail_start:]}"
    )


def _too_deep(value: Any) -> str:
    return f"[{type(value).__name__} nested too deep to show]"


def _bounded_tool_output(output: Any) -> Any:
    """Keep the head and tail of an oversized result and say what was cut, so the model
    reading it on the next turn knows the result is partial and can narrow the command.

    Text is cut by ``_cut_text`` at ``CODEX_TOOL_OUTPUT_MAX_CHARS``. A result that is not
    text is measured as its JSON text and, when that is over the bound, replaced by the cut
    JSON text; one nested too deep to encode is replaced by a line saying so.

    The cut text is what the caller, the callbacks and the branch's record receive. The
    chunk's ``metadata`` still holds the CLI's whole event: it is not written to a run's
    buffer file, but a non-streaming call returns its chunks with it, so a logger that
    records call results keeps the event whole.
    """
    text = output
    if not isinstance(output, str):
        if output is None:
            return output
        try:
            text = json.dumps(output, ensure_ascii=False, default=str)
        except RecursionError:
            return _too_deep(output)
    if len(text) <= CODEX_TOOL_OUTPUT_MAX_CHARS:
        return output
    return _cut_text(text, CODEX_TOOL_OUTPUT_MAX_CHARS)


#: Room an entry cut to fit keeps inside its share of the bound for the line or entry
#: that says what was cut from it.
_CUT_NOTE_CHARS = 96

#: The least room worth cutting an entry into; with less left, the entry is left out.
_MIN_CUT_CHARS = 256


def _key_text(key: Any) -> str:
    """The string a mapping key becomes in JSON: a string key as it is, and None, a bool or a
    number as ``json.dumps`` spells it."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return json.dumps(key)
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def _json_size(value: Any, sizes: dict[int, int]) -> int:
    """``len(json.dumps(value, ensure_ascii=False, default=str))``, summed from the parts so
    that each mapping and list is measured once however deep it sits; ``sizes`` keeps
    their lengths by id while the value is alive."""
    if not isinstance(value, (dict, list)):
        return len(json.dumps(value, ensure_ascii=False, default=str))
    size = sizes.get(id(value))
    if size is None:
        # Plain loops, not comprehensions, so a level of nesting costs one frame.
        size = 2  # the brackets, and ", " after every entry but the last
        if isinstance(value, dict):
            for key, item in value.items():
                size += _json_size(_key_text(key), sizes) + 2 + _json_size(item, sizes) + 2
        else:
            for item in value:
                size += _json_size(item, sizes) + 2
        if value:
            size -= 2
        sizes[id(value)] = size
    return size


def _bounded_leaves(
    value: Any,
    limit: int = CODEX_TOOL_OUTPUT_MAX_CHARS,
    sizes: dict[int, int] | None = None,
) -> Any:
    """``value`` with its JSON text brought within about ``limit`` characters, keeping its
    shape so a reader that expects a mapping or a list still gets one. A value within the
    bound is returned as it came, and a string over it is cut by ``_cut_text``, measured
    as JSON writes it, escapes included.

    A list keeps its items in order up to the first that does not fit. When the room left
    is worth it, that item is cut to the room less the share its note keeps, and an item
    naming how many were left out comes last. A mapping keeps every entry that fits whole,
    in order; then, while the room left is worth it, the entries that did not fit are cut,
    in order, each to that room less its key and its note's share, and an
    ``"[omitted]"`` entry names how many entries were left out. A key is never cut, since a
    reader can classify an entry by its key (a redactor looking for a credential name in
    it): an entry whose key, as JSON writes it, takes more than half the room left is left
    out whole. An entry cut to fit keeps its own note inside the room it was given, so only
    the outermost note and a cut line inside it come on top of ``limit``. Each level keeps
    that room for its own note, so the leaf of a chain of one-entry mappings more than about
    190 levels deep (fewer with longer keys), or of one-item lists more than about 200, is
    left out, with a note saying so.
    """
    if sizes is None:
        sizes = {}
    if isinstance(value, str):
        return _cut_text(value, max(limit - 2, 0), escaped=True)  # less the quotes
    if not isinstance(value, (dict, list)):
        return value
    if _json_size(value, sizes) <= limit:
        return value
    if isinstance(value, list):
        kept_items: list[Any] = []
        used = 2  # the brackets
        for item in value:
            sep = 2 if kept_items else 0
            size = _json_size(item, sizes)
            if used + sep + size <= limit:
                kept_items.append(item)
                used += sep + size
                continue
            room = limit - used - sep
            if room >= _MIN_CUT_CHARS:
                kept_items.append(_bounded_leaves(item, room - _CUT_NOTE_CHARS, sizes))
            break
        if len(kept_items) < len(value):
            kept_items.append(
                f"[{len(value) - len(kept_items):,} of {len(value):,} items not shown]"
            )
        return kept_items
    whole: set[Any] = set()
    used = 2  # the braces
    for key, item in value.items():
        size = _json_size(_key_text(key), sizes) + 2 + _json_size(item, sizes)
        sep = 2 if whole else 0
        if used + sep + size <= limit:
            whole.add(key)
            used += sep + size
    cut: dict[Any, Any] = {}
    for key, item in value.items():
        if key in whole:
            continue
        room = limit - used - 2
        if room < _MIN_CUT_CHARS:
            break
        key_size = _json_size(_key_text(key), sizes) + 2
        if key_size > room // 2:
            continue
        item = _bounded_leaves(item, room - key_size - _CUT_NOTE_CHARS, sizes)
        cut[key] = item
        used += 2 + key_size + _json_size(item, sizes)
    kept: dict[Any, Any] = {}
    for key, item in value.items():
        if key in whole:
            kept[key] = item
        elif key in cut:
            kept[key] = cut[key]
    left_out = len(value) - len(kept)
    if left_out:
        note = "[omitted]"
        while note in kept:
            note = f"[{note}]"
        kept[note] = f"{left_out:,} of {len(value):,} keys not shown"
    return kept


def _bounded_arguments(arguments: Any) -> Any:
    """A function call's arguments as echoed for the call's input, bounded as
    ``_bounded_leaves`` bounds a value. JSON text naming an object or an array is decoded,
    bounded and encoded again, so it still parses, though a value that was cut reads
    differently and the spelling can differ (escapes, numbers, a repeated key keeps its
    last value); a mapping or a list is bounded the same way; any other text, and JSON
    nested too deep to decode, is cut as a result is, and a mapping or a list nested too
    deep to measure is bounded as a result is."""
    if not isinstance(arguments, str):
        try:
            return _bounded_leaves(arguments)
        except RecursionError:
            return _bounded_tool_output(arguments)
    if len(arguments) <= CODEX_TOOL_OUTPUT_MAX_CHARS:
        return arguments
    try:
        decoded = json.loads(arguments)
        if isinstance(decoded, (dict, list)):
            return json.dumps(_bounded_leaves(decoded), ensure_ascii=False)
    except (ValueError, RecursionError):
        pass
    return _bounded_tool_output(arguments)


def _bounded_changes(changes: Any) -> Any:
    """A file change's ``changes`` as echoed for the call's input. A list over the bound
    keeps its entries in order up to the first that does not fit whole, so a reader of the
    list still finds each kept entry's ``path``; when the first entry alone is over, it is
    cut as ``_bounded_leaves`` cuts a value. An entry naming how many were left out comes
    last.
    Anything else is bounded as ``_bounded_leaves`` bounds a value, and a value nested too
    deep to measure is bounded as a result is."""
    try:
        if not isinstance(changes, list):
            return _bounded_leaves(changes)
        sizes: dict[int, int] = {}
        if _json_size(changes, sizes) <= CODEX_TOOL_OUTPUT_MAX_CHARS:
            return changes
        kept: list[Any] = []
        used = 2  # the brackets
        for entry in changes:
            size = _json_size(entry, sizes) + 2  # and a separator
            if used + size > CODEX_TOOL_OUTPUT_MAX_CHARS:
                if not kept:
                    room = CODEX_TOOL_OUTPUT_MAX_CHARS - used - _CUT_NOTE_CHARS
                    kept.append(_bounded_leaves(entry, room, sizes))
                break
            kept.append(entry)
            used += size
    except RecursionError:
        return _bounded_tool_output(changes)
    if len(kept) < len(changes):
        kept.append(
            {"omitted": f"{len(changes) - len(kept):,} of {len(changes):,} changes not shown"}
        )
    return kept


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
    # per-turn delta; track the last-seen cumulative values so only the
    # marginal delta is ever exposed per event. See
    # docs/internals/providers.md#codex-turn-completed-usage-delta.
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
                        "input": _bounded_arguments(
                            item.get(
                                "arguments",
                                item.get("input", item.get("args", {})),
                            )
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
                    output = _bounded_tool_output(item.get("aggregated_output", ""))
                    exit_code = item.get("exit_code")
                    status = item.get("status", "")
                    is_error = status == "failed" or (exit_code is not None and exit_code != 0)

                    command = _bounded_tool_output(command)
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
                    echoed = _bounded_changes(changes)

                    tu = {"id": item_id, "name": "Edit", "input": {"changes": echoed}}
                    session.tool_uses.append(tu)
                    if on_tool_use:
                        await maybe_await(on_tool_use(tu))
                    if request.verbose_output:
                        _pp_tool_use(tu, theme)
                    sc = StreamChunk(
                        type="tool_use",
                        tool_name="Edit",
                        tool_id=item_id,
                        tool_input={"changes": echoed},
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
                        "content": _bounded_tool_output("; ".join(summary_parts) or status),
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
                        "content": _bounded_tool_output(
                            item.get("output", item.get("content", ""))
                        ),
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

                # Emit only the marginal delta since the previous event, clamped
                # at 0 — see docs/internals/providers.md#codex-turn-completed-usage-delta.
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
                # Unlike usage/cost, num_turns is already a per-event delta (incremented
                # locally above, not read off the event) — always safe to emit as 1.
                result_meta["num_turns"] = 1
                rsc = StreamChunk(type="result", metadata=result_meta)
                session.chunks.append(rsc)
                yield rsc

            # -- turn.failed / error --
            elif typ in ("turn.failed", "error"):
                err = obj.get("error", {})
                # Error message location varies by event type; capture the raw
                # value pre-normalization for the benign-EOS check below.
                _raw_err = err
                if err is None:
                    err = {}
                _err_message = (
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
                # The CLI announces its OWN retry of a dropped provider stream
                # as an error-typed event and keeps going; surfaced as a
                # non-error chunk so the leg isn't killed before that retry
                # runs. An unrecognized notice text falls through to the
                # terminal branch below (fails toward losing the leg, not
                # hanging it).
                if (
                    typ == "error"
                    and isinstance(_err_message, str)
                    and _RECONNECT_NOTICE_RE.match(_err_message)
                ):
                    if request.verbose_output:
                        log.warning("Codex reconnecting mid-stream: %s", _err_message)
                    sc = StreamChunk(
                        type="error",
                        content=_err_message,
                        is_error=False,
                        metadata={**obj, "reconnect_notice": True},
                    )
                    session.chunks.append(sc)
                    yield sc
                    continue

                session.is_error = True
                session.result = _err_message
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
                sc = StreamChunk(
                    type="error",
                    content=session.result,
                    is_error=not _is_benign_eos,
                    metadata=chunk_meta,
                )
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
                                "input": _bounded_arguments(
                                    blk.get("input", blk.get("arguments", {}))
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
    "gpt-6.1-sol": 1_050_000,
    "gpt-6-astra": 1_050_000,
    "gpt-6-sol": 1_050_000,
    "gpt-6-luna": 1_050_000,
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
    _runtime_state_fields = ("env", "on_spawn")
    # Codex streams a "thread.started"/"system" event right after spawn —
    # see stream_codex_cli() above.
    streams_first_output_early = True

    def __init__(self, config: EndpointConfig = None, **kwargs):
        handlers = kwargs.pop("codex_handlers", None)
        # Before super(), which deep-copies a supplied config: see
        # take_supplied_runtime_state for what that copy does to a bound method.
        supplied = self.take_supplied_runtime_state(config, kwargs)
        super().__init__(config=config, **kwargs)
        self._init_handlers(handlers, supplied=supplied)

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
                    # Turns the session's terminal verdict into a chunk — see
                    # docs/internals/providers.md#a-session-terminal-error-must-be-turned-back-into-a-stream-chunk.
                    if item.is_error and not any(c.type == "error" for c in item.chunks):
                        yield StreamChunk(
                            type="error",
                            content=item.result or "Codex session failed",
                            is_error=True,
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
        # Unconditional: the bounded activity record carries no tool inputs,
        # so it needs no redaction and costs a few keys. The full summary,
        # which does carry inputs, stays opt-in.
        session.populate_activity()
        if request.cli_include_summary:
            session.populate_summary()

        return to_dict(session, recursive=True)

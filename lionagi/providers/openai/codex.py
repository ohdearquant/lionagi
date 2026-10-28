# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import functools
import html.entities
import json
import logging
import os
import re
import subprocess
import unicodedata
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from textwrap import shorten
from typing import TYPE_CHECKING, Any, Literal, get_args

import toml
from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from lionagi._errors import CLIInputRefusedError, ConfigurationError, ContainmentRefusedError
from lionagi.libs.path_safety import check_add_dirs_safe as check_add_dir_entries_safe
from lionagi.libs.path_safety import check_path_safe, check_paths_safe
from lionagi.libs.path_safety import contain_paths_in_root as contain_paths_in_repo
from lionagi.ln import copy
from lionagi.ln.concurrency import current_time
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
    reveal_redacted,
    validate_message_prompt,
)
from lionagi.providers._cli_subprocess import (
    make_cli_flag as _cli,
)
from lionagi.providers._provider_errors import classify_provider_error
from lionagi.providers._subagent_admission import DEFAULT_WORKLOAD, SubagentWorkload
from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.types.cli_session import CLISession
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.utils import to_dict

from ._codex_profile import (
    _read_codex_config,
    codex_config_defining_profile,
    codex_config_setting_default_permissions,
    codex_home_for_env,
    configured_codex_profile,
    is_generated_codex_profile,
    project_codex_profile,
)
from ._codex_project_layers import declaring_project_layer, parse_failure, project_layers
from ._config import CodexConfigs

if TYPE_CHECKING:
    from markdown_it import MarkdownIt
    from markdown_it.token import Token

try:
    import tomllib as _tomllib
except ModuleNotFoundError:  # Python 3.10: the declared `tomli`; the locked 2.4.1 reads TOML 1.1
    # (`toml` 0.10 does not normalize whitespace around dotted key parts)
    import tomli as _tomllib  # type: ignore[no-redef]

HAS_CODEX_CLI, CODEX_CLI = discover_cli("codex")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("codex-cli")

_BOOL_ADAPTER = TypeAdapter(bool)


def _as_declared_bool(value: Any) -> bool:
    """Read a pre-validation boolean the way the field's own coercion will.

    A model-level ``mode="before"`` validator sees raw input, where ``bool("false")`` is
    True: a request carrying ``ignore_user_config="false"`` would take the hermetic branch
    while validating to a request that reads the user config. A value the field rejects
    fails validation moments later, so it is read as False here.
    """
    try:
        return _BOOL_ADAPTER.validate_python(value)
    except ValidationError:
        return False


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

__all__ = (
    "CodexCodeRequest",
    "stream_codex_cli",
    "CodexCLIEndpoint",
    "refuse_declaring_project_layer",
)

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
# How an error event whose payload holds no value is reported; a message from
# another event of the same failure says more.
_EMPTY_ERROR_PAYLOAD = "CLI failure (empty error payload"

# Models served through the CLI can write a tool call into their reply as text
# in their own call markup instead of emitting a real call. The CLI takes that
# text as the turn's final message and ends the turn normally; the session is
# marked failed (``is_error``, with a result naming the cause) and an error
# chunk is emitted, read when a turn completes or the stream ends, over the
# turn's last non-empty agent text outside code. Each entry matches markup tags
# only — never the word DSML or a bare "|" in prose — so a new model's markup
# can be supported by adding one compiled pattern here.
_TOOL_CALL_MARKUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"</?\s*[\uff5c|]+\s*DSML\s*[\uff5c|]+\s*"
        r"(?:function_calls|parameter|invoke|calls)(?:\s[^<>\0]*)?/?>"
    ),
)

# The markup check reads the text as a Markdown reader sees it. The text is
# parsed as CommonMark (markdown-it-py), and the markup is looked for only in
# what a reader sees as prose: code spans, fenced code blocks (a fence nothing
# closes included, which runs to the end of its container) and indented code
# blocks are left out, wherever quotes and lists put them, so markup quoted as
# code never fails a turn. Link destinations and titles and link reference
# definitions are not shown as text, so they are not read either. Each block is
# read on its own, and a code span stands between the text on either side of it,
# so no tag is pieced together across a block or a span.
#
# One choice goes past CommonMark and errs toward failing a turn that was fine:
# raw HTML is read as prose, the text of a `<pre>` or `<code>` element included.
#
# Two err the other way, toward completing the turn as it completed before this
# check. Text at the parser's nesting limit is not read: the limit is 20 levels,
# and a list and its item count as one each, so text inside 20 nested quotes or
# 10 nested lists is dropped. And a message longer than
# `_MARKUP_CHECK_MAX_CHARS` is not read at all, which bounds the parse: the
# slowest text found for the parser is a paragraph of unclosed inline HTML
# openers (`<?`, `<!--`, `<![CDATA[`), whose time grows with the square of its
# length, and at that length it takes about half a second.
#
# The parser decodes character references in prose itself. In a raw HTML block
# one is decoded only where it ends with a semicolon and names an entry of the
# HTML entity table (or is numeric); a bare `&lt` is literal text to a reader,
# so it names no tag.
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{0,30});")
_MARKUP_CHECK_MAX_CHARS = 16_000

# Completed items that are a call the CLI ran, or a call's result. One of these
# after a message means the turn went on past that message, so the message is
# no longer the turn's last text. Any other item (a plan update, a notice, a
# type not named here) leaves the turn's last text as it was.
_CALL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "function_call",
        "tool_call",
        "function_call_output",
    }
)

# The failure recorded when the turn's last text carries call markup.
_TOOL_CALL_MARKUP_FAILURE = (
    "The model wrote a tool call as text in its final message "
    "instead of making the call; the run stopped before the call ran."
)


def _decode_references(text: str) -> str:
    """Decode the HTML character references a reader sees as a character.

    Only a reference that ends with a semicolon and names an entry of the HTML
    entity table (or a numeric reference) is decoded; a bare ``&lt`` stays
    literal text.
    """
    if "&" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        body = match.group(0)[1:-1]
        if body[:1] == "#":
            try:
                code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
            except ValueError:
                return match.group(0)
            if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
                return chr(code)
            return match.group(0)
        value = html.entities.html5.get(body + ";")
        return match.group(0) if value is None else value

    return _ENTITY_RE.sub(_replace, text)


def _inline_text(children: Sequence[Token], parts: list[str]) -> None:
    """Append the text a reader sees in ``children``.

    A code span becomes a NUL, which no markup pattern matches across, so a tag
    is never pieced together from the prose on either side of one.
    """
    for child in children:
        if child.type == "code_inline":
            parts.append("\0")
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif child.type == "image":
            _inline_text(child.children or (), parts)
        else:
            parts.append(child.content)


@functools.cache
def _markdown_parser() -> MarkdownIt:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark")


def _visible_blocks(text: str) -> list[str]:
    """The prose of ``text`` a Markdown reader sees, one string per block.

    Each block is read on its own, so a tag is never pieced together from the
    end of one block and the start of the next.
    """
    blocks: list[str] = []
    for token in _markdown_parser().parse(text):
        if token.type == "inline":
            parts: list[str] = []
            _inline_text(token.children or (), parts)
            blocks.append("".join(parts))
        elif token.type == "html_block":
            blocks.append(_decode_references(token.content))
    return blocks


def _has_tool_call_markup(text: object) -> bool:
    """True when ``text`` carries a tool-call markup tag written as plain text.

    The visible text is read as its reader sees it, so an HTML entity that
    spells a tag (`&lt;...&gt;`) names one too.
    """
    if not isinstance(text, str) or not text or len(text) > _MARKUP_CHECK_MAX_CHARS:
        return False
    return any(
        p.search(block) for block in _visible_blocks(text) for p in _TOOL_CALL_MARKUP_PATTERNS
    )


def _call_markup_error_chunk(session: CLISession, event: Mapping[str, Any]) -> StreamChunk:
    """Mark ``session`` failed for call markup written as text and build its chunk.

    The session is marked failed the way any failed turn marks it -- ``is_error``
    and a result naming the cause -- and the returned chunk says the same thing
    in its content, carries ``call_markup_in_text``, and is a real error chunk.
    A failure already recorded keeps its result; the chunk still names the
    markup.
    """
    if not session.is_error:
        session.result = _TOOL_CALL_MARKUP_FAILURE
    session.is_error = True
    sc = StreamChunk(
        type="error",
        content=_TOOL_CALL_MARKUP_FAILURE,
        is_error=True,
        metadata={"call_markup_in_text": True, **event},
    )
    session.chunks.append(sc)
    return sc


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
            # Named by position: an override's key can hold a secret as its value can.
            what = f"The -c override at argument {position}"
        elif flag.startswith("-"):
            what = f"The value of {flag}"
        else:
            what = f"Command-line argument {position}"
        # Measured in the bytes exec passes: os.fsencode is how an argument is encoded for
        # exec, so a path holding a byte that is not UTF-8 counts that byte once. The
        # argument is read with `str.__str__` first, so a caller's own `encode` is never
        # called here; a character the text cannot encode is refused after the handler,
        # naming the argument by its position or its flag, since the codec's error holds
        # the whole argument.
        if not issubclass(type(argument), str):
            raise TypeError(f"{what} must be text, not {type(argument).__name__}")
        try:
            encoded = os.fsencode(str.__str__(argument))
        except UnicodeEncodeError:
            encoded = None
        if encoded is None:
            raise _refuse_text_strict_utf8_cannot_encode(what)
        if b"\x00" in encoded:
            raise ValueError(_nul_refusal_message(what))
        _refuse_oversized_argument(encoded, what)


def _contain_override(key: str, value: Any, what: str) -> tuple[bool, Any]:
    """Whether account-integration containment keeps the override ``key=value``, and the
    value it keeps: an override under a top-level `apps` or `plugins` table, or under
    `features.apps` or `features.plugins`, is dropped, and those keys leave an inline
    `features` table, which is dropped when nothing is left. Reads the normalized snapshot:
    ``key`` is plain text and an inline table is a plain ``dict`` whose switch names are
    plain text, so the name this check compares is the name the table renders. ``what``
    names the key in a refusal, so the key itself is never printed."""
    path = _override_key_path(key, what)
    if path[0] in _ACCOUNT_INTEGRATION_FEATURES:
        return False, value
    if path[0] == "features":
        if len(path) >= 2 and path[1] in _ACCOUNT_INTEGRATION_FEATURES:
            return False, value
        if len(path) == 1 and isinstance(value, Mapping):
            kept = {
                name: item
                for name, item in value.items()
                if name not in _ACCOUNT_INTEGRATION_FEATURES
            }
            value = kept
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


def _equals_in_key_refusal(index: int) -> ValueError:
    # `-c key=value` is split at its first `=`, so a key holding one would set the key
    # before it to the rest of the argument: `model="x"#` reads as setting `model`. Named
    # by position: a key holding `=` may be a value written into it, a secret included.
    return ValueError(
        f"The key of the config override entry {index} holds '=', where the CLI ends a "
        "-c key, so it would set a different key."
    )


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
    the override. Every C0 control character and DEL is written as a ``\uXXXX`` escape,
    which TOML reads back as the same character, and ``"`` and ``\`` as ``\"`` and
    ``\\``; every other character, U+0085 and U+2028 among them, is kept as it is.
    """
    # Strict UTF-8 first: a lone surrogate has no encoding a child process can decode,
    # so it is refused here rather than reaching the CLI as a raw byte. Escapes are
    # added after the quote and backslash escapes so the escape they introduce survives.
    value.encode("utf-8")
    body = value.replace("\\", "\\\\").replace('"', '\\"')
    body = _TOML_CONTROL_CHAR_RE.sub(lambda match: f"\\u{ord(match.group()):04X}", body)
    return f'"{body}"'


# The text fields the command line renders into an argument of their own, by name.
# Read by the checks so a lone surrogate in any of them is refused by name rather
# than reaching the CLI as a raw byte. Path fields are left out of THIS list, not out of
# the checks: a path may legitimately hold a byte that is not UTF-8, and ``os.fsencode``
# is what measures those at the process boundary, so the path fields are reduced in the
# snapshot and measured where the built command is.
_ARGUMENT_TEXT_FIELDS: tuple[str, ...] = (
    "model",
    "profile",
    "sandbox",
    "ask_for_approval",
    "color",
    "reasoning_effort",
    "plan_mode_reasoning_effort",
)

# The repeated text fields the command line renders one argument per entry for.
_ARGUMENT_LIST_FIELDS: tuple[str, ...] = ("enable_features", "disable_features")


def _plain_text(item: Any) -> str:
    """``item`` as a plain ``str``, read with ``str.__str__``.

    A ``str`` subclass's own ``__str__``, ``__format__`` or ``__eq__`` can answer a
    containment check one way and the renderer another; ``str.__str__`` returns an exact
    copy of its characters without calling any of them. The caller establishes that the
    type is text before calling.
    """
    return str.__str__(item)


def _plain_entry_key(key: Any, index: int) -> str:
    """The key of the `-c` override at ``index`` as plain text, or a refusal by position
    and type.

    A key is text only when ``issubclass(type(key), str)`` holds: a type whose
    ``__class__`` merely answers ``str`` is not text, and is refused here rather than
    read as one value by a check and another by the command line. The key itself is
    never printed — it can hold a secret as a value can.
    """
    if not issubclass(type(key), str):
        raise TypeError(
            f"The config override entry {index} has a key that is not a string "
            f"({type(key).__name__})."
        )
    return _plain_text(key)


def _plain_path(item: Any, what: str) -> str:
    """One path field entry as a plain ``str`` read with ``str.__str__(os.fspath(x))``, or
    a refusal by name and type.

    ``os.fspath`` reads a path-like value through ``__fspath__`` and a ``str`` through
    itself, so a path-like object's own ``__str__``, and a ``str`` subclass's own
    ``__str__``, are each read once, here; the answer is reduced with ``str.__str__`` to
    an exact ``str``. The containment check and ``as_cmd_args`` then read that one plain
    text, so ``os.fsencode`` never calls a caller object's ``encode``. A value
    ``os.fspath`` cannot read, or whose answer is not text, is refused as ``what`` and its
    type, never by its text.
    """
    try:
        target = os.fspath(item)
    except TypeError:
        reason = "is not a path"
    else:
        reason = None
        if not issubclass(type(target), str):
            reason = "does not name a text path"
    if reason is not None:
        raise TypeError(f"{what} {reason} (it is a {type(item).__name__})")
    return str.__str__(target)


def _plain_paths(value: Any, name: str) -> list[str]:
    """Every entry of the repeated path field ``name`` reduced to plain ``str``."""
    if value is None:
        return []
    if not issubclass(type(value), (list, tuple)):
        raise TypeError(f"{name} must be a list of paths, not {type(value).__name__}")
    entries = list.__iter__(value) if issubclass(type(value), list) else tuple.__iter__(value)
    return [_plain_path(item, f"{name} entry {index}") for index, item in enumerate(entries)]


def _plain_optional_path(value: Any, name: str) -> str | None:
    """A single path field reduced to plain ``str``, or ``None`` when unset."""
    return None if value is None else _plain_path(value, name)


def _plain_path_fields(request: Any) -> dict[str, Any]:
    """The four path fields of ``request`` reduced to plain ``str``, read once and by name.

    ``output_schema``, ``output_last_message``, every ``images`` entry and every
    ``add_dir`` entry, each ``str.__str__(os.fspath(x))``. The containment check and
    ``_plain_request`` both read this, so the text contained is the text rendered. It
    touches only these fields, so a request whose environment the spawn would refuse is
    still contained by the path check first.
    """
    return {
        "output_schema": _plain_optional_path(request.output_schema, "output_schema"),
        "output_last_message": _plain_optional_path(
            request.output_last_message, "output_last_message"
        ),
        "images": _plain_paths(request.images, "images"),
        "add_dir": _plain_paths(request.add_dir, "add_dir"),
    }


def _plain_override_value(value: Any, index: int) -> Any:
    """``value`` reduced to plain built-in data, recursively, or a refusal by position
    and type.

    ``str``, ``int`` and ``float`` subclasses are read through the base type's own
    reader, so a value whose own ``__str__``, ``encode`` or ``__index__`` answers
    differently reaches every reader as the same built-in. A list or table is copied
    into a plain ``list``/``dict`` so no view of the caller's object is read twice; a
    table's keys are read as text by the same rule as an entry's key. ``None`` is kept
    so the command builder refuses it as it always has. Any other value is refused by
    its position in the entry and its type, never by its text.
    """
    kind = type(value)
    if value is None or kind is bool:
        return value
    if issubclass(kind, str):
        return _plain_text(value)
    if issubclass(kind, int):
        return int.__index__(value)
    if issubclass(kind, float):
        return float.__float__(value)
    if issubclass(kind, tuple):
        return [_plain_override_value(item, index) for item in tuple.__iter__(value)]
    if issubclass(kind, list):
        return [_plain_override_value(item, index) for item in list.__iter__(value)]
    if issubclass(kind, dict):
        return {
            _plain_entry_key(key, index): _plain_override_value(item, index)
            for key, item in dict.items(value)
        }
    raise TypeError(
        f"The config override entry {index} holds a value that is not plain data "
        f"({kind.__name__}); only text, numbers, booleans, lists and tables reach "
        "the command line."
    )


def _plain_pairs(pairs: Iterable[tuple[Any, Any]]) -> list[tuple[str, Any]]:
    """Each `-c` key/value pair reduced to plain built-in data, by position."""
    return [
        (_plain_entry_key(key, index), _plain_override_value(value, index))
        for index, (key, value) in enumerate(pairs)
    ]


def _override_pairs(overrides: Mapping | None) -> list[tuple[str, Any]]:
    """The `-c` overrides as one list of key/value pairs, read from ``items()`` once and
    reduced to plain built-in data at every depth.

    A containment decision and the rendered command line are two readers of one such
    list, so a mapping whose iteration and ``items()`` disagree, or whose ``items()``
    answers differently on each call, cannot answer a check and a renderer differently.
    Every key is text read with ``str.__str__`` and every value is reduced recursively,
    so a key or nested value whose own methods answer differently on each read reaches
    both readers as the same plain value, or is refused by position and type. Only
    ``None`` reads as no overrides: a mapping is read through ``items()`` whether or not
    its length agrees.
    """
    if overrides is None:
        return []
    return _plain_pairs(overrides.items())


@dataclass(frozen=True)
class _PlainRequest:
    """Every field a containment or refusal check reads, reduced to plain built-in data.

    ``as_cmd_args`` and the spawn path both read this snapshot and nothing else for
    these fields, so ``model_copy(update=...)`` — which skips validation — cannot hand a
    check one object and the command line another. The snapshot is taken again where the
    process would start. ``env`` holds the mapping the spawn encodes, each key and value
    read once as text; the four path fields and every entry of ``images`` and ``add_dir``
    are plain ``str`` read with ``str.__str__(os.fspath(x))``, so the containment check
    and the rendered argument are one text and ``os.fsencode`` never calls a caller
    object's ``encode``.
    """

    prompt: str
    system_prompt: str | None
    model: str | None
    profile: str | None
    sandbox: str | None
    ask_for_approval: str | None
    color: str | None
    reasoning_effort: str | None
    plan_mode_reasoning_effort: str | None
    enable_features: list[str]
    disable_features: list[str]
    overrides: list[tuple[str, Any]]
    bypass_approvals: bool
    full_auto: bool
    search: bool
    fast_mode: bool
    contain_account_integrations: bool
    ignore_user_config: bool
    account_connectors: list[str]
    output_schema: str | None
    output_last_message: str | None
    images: list[str]
    add_dir: list[str]
    env: dict[str, str] | None
    workload: str

    def contains_account_integrations(self) -> bool:
        # A light request is contained whatever it set: its sandbox keeps the network out,
        # and the account's apps and plugins reach past it.
        contained = self.contain_account_integrations or self.workload == "light"
        return contained and not self.account_connectors

    def updates(self) -> dict[str, Any]:
        """The snapshot's values for the fields the declarative renderer reads, for the
        copy it renders."""
        return {
            name: getattr(self, name)
            for name in (
                "model",
                "profile",
                "color",
                "enable_features",
                "disable_features",
                "output_schema",
                "output_last_message",
                "add_dir",
                "ignore_user_config",
            )
        }

    def override_keys(self) -> list[str]:
        return [key for key, _ in self.overrides]

    def argument_text_fields(self) -> list[tuple[str, str]]:
        """Each field the command line renders into an argument of its own, by name."""
        fields: list[tuple[str, str]] = []
        for name in _ARGUMENT_TEXT_FIELDS:
            text = getattr(self, name)
            if isinstance(text, str):
                fields.append((name, text))
        for name in _ARGUMENT_LIST_FIELDS:
            for feature in getattr(self, name):
                if isinstance(feature, str):
                    fields.append((name, feature))
        return fields


def _plain_request(
    request: CodexCodeRequest, overrides: Sequence[tuple[Any, Any]] | None = None
) -> _PlainRequest:
    """Reduce every field a check or the command line reads to plain built-in data.

    One function, so the containment checks and the renderer read one object: a text
    field is read with ``str.__str__``, a repeated field element by element, a boolean
    by its exact type, the four path fields and every ``images``/``add_dir`` entry with
    ``str.__str__(os.fspath(x))``, the environment with the encoder the spawn uses, and
    the overrides recursively, key by key and value by value. A field the reduction
    cannot read is refused by name (or, inside the overrides, by position) and its type,
    never by its text. ``overrides`` hands in an already-read list of pairs; ``None``
    reads ``request.config_overrides`` once.
    """

    def text(name: str) -> str | None:
        value = getattr(request, name)
        if value is None:
            return None
        if not issubclass(type(value), str):
            raise TypeError(f"{name} must be text, not {type(value).__name__}")
        return _plain_text(value)

    def texts(name: str) -> list[str]:
        value = getattr(request, name)
        if value is None:
            return []
        if not issubclass(type(value), (list, tuple)):
            raise TypeError(f"{name} must be a list of text, not {type(value).__name__}")
        entries = list.__iter__(value) if issubclass(type(value), list) else tuple.__iter__(value)
        plain: list[str] = []
        for index, item in enumerate(entries):
            if not issubclass(type(item), str):
                raise TypeError(f"{name} entry {index} must be text, not {type(item).__name__}")
            plain.append(_plain_text(item))
        return plain

    def flag(name: str) -> bool:
        value = getattr(request, name)
        if type(value) is not bool:
            raise TypeError(f"{name} must be a boolean, not {type(value).__name__}")
        return value

    paths = _plain_path_fields(request)
    env = request.env
    if env is not None and not issubclass(type(env), Mapping):
        raise TypeError(f"env must be a mapping, not {type(env).__name__}")

    pairs = (
        _override_pairs(request.config_overrides) if overrides is None else _plain_pairs(overrides)
    )
    return _PlainRequest(
        prompt=text("prompt") or "",
        system_prompt=text("system_prompt"),
        model=text("model"),
        profile=text("profile"),
        sandbox=text("sandbox"),
        ask_for_approval=text("ask_for_approval"),
        color=text("color"),
        reasoning_effort=text("reasoning_effort"),
        plan_mode_reasoning_effort=text("plan_mode_reasoning_effort"),
        enable_features=texts("enable_features"),
        disable_features=texts("disable_features"),
        overrides=pairs,
        bypass_approvals=flag("bypass_approvals"),
        full_auto=flag("full_auto"),
        search=flag("search"),
        fast_mode=flag("fast_mode"),
        contain_account_integrations=flag("contain_account_integrations"),
        ignore_user_config=flag("ignore_user_config"),
        account_connectors=texts("account_connectors"),
        output_schema=paths["output_schema"],
        output_last_message=paths["output_last_message"],
        images=paths["images"],
        add_dir=paths["add_dir"],
        env=None if env is None else env_as_the_spawn_encodes_it(env),
        workload=text("workload") or DEFAULT_WORKLOAD,
    )


#: The ``-c`` keys a light agent's sandbox is pinned by, and ``default_permissions``, which
#: names a permissions profile codex applies in place of ``sandbox_mode``, with the
#: ``permissions`` table that defines those profiles; a caller override of any of them could
#: widen what the agent may write or reach, so a light request refuses them, and the
#: ``profile``/``profiles`` keys, since a codex profile can set them too.
_LIGHT_PINNED_OVERRIDE_ROOTS = (
    "sandbox_mode",
    "sandbox_workspace_write",
    "default_permissions",
    "permissions",
)


def _refuse_light_escape(plain: _PlainRequest) -> None:
    """Refuse a ``light`` request whose settings would let it write outside its cwd.

    A light agent runs in codex's ``workspace-write`` sandbox (or ``read-only``),
    writable only in its own working directory and with no network. Each setting
    that would undo that is refused rather than dropped, so a caller learns the agent
    needs to be declared ``heavy`` instead of getting a sandbox it did not ask for.
    Run at construction and again where the process would start.
    """
    if plain.workload != "light":
        return
    reasons = []
    if plain.bypass_approvals:
        reasons.append("bypass_approvals turns the sandbox off")
    if plain.sandbox == "danger-full-access":
        reasons.append("sandbox='danger-full-access' turns the sandbox off")
    if plain.add_dir:
        reasons.append("add_dir makes directories outside the working directory writable")
    if plain.account_connectors:
        reasons.append("account_connectors turns the account's apps and plugins on")
    if plain.profile is not None and not is_generated_codex_profile(plain.profile):
        reasons.append(f"profile={plain.profile!r} can set the sandbox the light workload pins")
    widening: set[str] = set()
    for index, key in enumerate(plain.override_keys()):
        # A key that holds '=' is refused by position where as_cmd_args renders the overrides.
        if "=" in key:
            continue
        unreadable = None
        try:
            # Read as codex reads it, so `"sandbox_mode"` or `sandbox_workspace_write . x`
            # names the same setting as the bare dotted key.
            path = _override_key_path(key, f"The key of the config override entry {index}")
        except ValueError as exc:
            unreadable = str(exc)
        if unreadable is not None:
            # Raised after the handler, so the refusal keeps no error as its context.
            raise CLIInputRefusedError(unreadable)
        if path[0] in _LIGHT_PINNED_OVERRIDE_ROOTS or path[0] in ("profile", "profiles"):
            widening.add(".".join(path))
    if widening:
        reasons.append(
            f"config_overrides set {', '.join(sorted(widening))}, which the light sandbox pins"
        )
    if reasons:
        # Not a ValueError, so a refusal at construction is not wrapped with the whole input.
        raise CLIInputRefusedError(
            "workload 'light' runs codex in its workspace-write sandbox with no network; "
            + "; ".join(reasons)
            + ". Declare the agent's workload 'heavy' instead."
        )


def _refuse_ambiguous_approval(plain: _PlainRequest) -> None:
    """Refuse ``ask_for_approval`` beside an ``approval_policy`` override.

    Run on the normalized snapshot at construction and again where the process would
    start, so a request copied without validation cannot put both ``-c
    approval_policy=`` entries on the command line.
    """
    if (
        plain.ask_for_approval
        and not plain.bypass_approvals
        and not plain.full_auto
        and any(
            key == "approval_policy" or key.startswith("approval_policy.")
            for key in plain.override_keys()
        )
    ):
        raise ValueError(
            "ask_for_approval cannot be combined with config_overrides entries "
            "for approval_policy; choose one approval policy source"
        )


def _refuse_oversized_cli_inputs(
    prompt: str,
    system_prompt: str | None = None,
    override_pairs: Sequence[tuple[Any, Any]] | None = None,
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

    ``override_pairs`` are the key/value pairs of one normalized snapshot: a key is read
    as text only when ``issubclass(type(key), str)`` holds, and a key or value that is
    not text is refused by position and type, never by its text. Every caller passes the
    same pairs the command line is rendered from.
    """
    failure: ValueError | TypeError | None = None
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
    for index, (key, value) in enumerate(override_pairs or ()):
        if failure is not None:
            break
        if not issubclass(type(key), str):
            # A key is text only when its own type is a str subclass; a type whose
            # __class__ merely answers str is not text. Named by position and type,
            # never by the key, with the TypeError the field validator raises.
            failure = TypeError(
                f"The config override entry {index} has a key that is not a string "
                f"({type(key).__name__})."
            )
            break
        if "=" in key:
            failure = _equals_in_key_refusal(index)
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
                f"The value of the config override entry {index}"
            )
            break
        if contained:
            try:
                kept, kept_value = _contain_override(
                    key, value, f"The key of the config override entry {index}"
                )
            except ValueError as exc:
                # A key containment cannot read is refused here, by its position, so
                # construction refuses it as the spawn does, and with the type a
                # containment refusal carries, so a judge reads it as a refusal.
                failure = ContainmentRefusedError(str(exc))
                break
            if not kept:
                continue
            if kept_value is not value:
                try:
                    encoded = toml_override_value(kept_value)
                except TypeError:
                    continue  # as_cmd_args refuses the value itself
                except UnicodeEncodeError:
                    failure = _refuse_text_strict_utf8_cannot_encode(
                        f"The value of the config override entry {index}"
                    )
                    break
        argument = f"{key}={encoded}"
        # The value is rendered with every C0 control character escaped, so a NUL left in
        # the argument is the key's.
        failure = _argument_text_failure(argument, f"The key of the config override entry {index}")
        if failure is None:
            measured.append((f"The config override entry {index}", argument.encode("utf-8")))
    # The text fields that reach the CLI as an argument of their own, each encoded
    # strictly and checked for a NUL under its own name. The efforts and the approval
    # policy are written as TOML strings, where a NUL would be escaped rather than
    # refused. The approval policy is a closed choice no value with a NUL belongs to;
    # the efforts are open strings, but no effort name holds a NUL either. Other C0
    # control characters and DEL in an effort are escaped and sent, as are `"` and `\`,
    # and any other character is sent as it is; the per-model clamp ignores control,
    # format and line or paragraph separator characters when it reads the name.
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


def _override_key_path(key: str, what: str) -> list[str]:
    """The parts of a `-c key=value` override's key, read as the TOML dotted
    key the CLI reads it as: a quoted part loses its quotes and its escapes, so
    `features."\\u0061pps"` names the same switch as `features.apps`. A key
    TOML cannot read is refused here, as ``what``, rather than handed to the CLI;
    neither the key nor the parser's message, which can quote it, is printed or kept on
    the refusal."""
    path: list[str] | None = []
    try:
        table: Any = _tomllib.loads(f"{key} = 1")
        while isinstance(table, dict):
            ((part, table),) = table.items()
            path.append(part)
    except Exception:
        path = None
    if path is None:
        # Raised after the handler: one raised inside it, even `from None`, keeps the
        # parser's error as its __context__.
        raise ValueError(f"{what} is not a TOML key.")
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


def _rendered_override_value(value: Any, what: str) -> str:
    """``toml_override_value(value)`` for ``as_cmd_args``, which a copy reaches unchecked
    (``model_copy(update=...)`` skips validation): text strict UTF-8 cannot encode is
    refused as ``what`` after the handler, since the codec's error holds the whole text."""
    try:
        return toml_override_value(value)
    except UnicodeEncodeError:
        pass
    raise _refuse_text_strict_utf8_cannot_encode(what)


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
        # The keys are checked on the plain copy that is returned, as env's are, so a mapping
        # whose iteration and storage disagree cannot pass the check with one set of keys and
        # hand pydantic another. Refused by position and type: pydantic would quote a key it
        # cannot validate as a string, and a key can hold a secret as a value can. A bytes key
        # is refused too rather than decoded.
        value = dict(value)
        unnamed = [
            f"entry {index} (key is {type(key).__name__})"
            for index, key in enumerate(value)
            if not issubclass(type(key), str)
        ]
        if unnamed:
            raise TypeError(
                "config_overrides keys must be strings; these entries are not: "
                + ", ".join(unnamed)
            )
        # Each key is read once, through ``str.__str__``, into an exact ``str``: a ``str``
        # subclass key left in place is rejected by pydantic — which runs strict UTF-8 on a
        # subclass and quotes the key, which can hold a credential, into the validation error.
        # A key the spawn cannot encode then reaches the input check, which refuses the
        # request as an input the CLI cannot take rather than as a validation error.
        return {str.__str__(key): item for key, item in value.items()}

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
    ignore_user_config: bool = Field(
        default=False,
        description=(
            "Do not load CODEX_HOME/config.toml for this invocation, so the user's "
            "MCP servers and profiles stay out of the run. Codex keeps its normal "
            "authentication store. The flag does not cover project layers, so a run "
            "with this set is refused when one of them declares `mcp_servers`. The user "
            "config's `sandbox_mode` and `[sandbox_workspace_write]` settings are passed "
            "beside the flag unless the run's overrides set them, and the mode or a setting "
            "that widens the sandbox refuses the run where a project layer sets it otherwise. "
            "Its hook trust records and `[projects]` trust decisions are merged with any the "
            "overrides write, so the hooks it trusts in CODEX_HOME/hooks.json still run and "
            "codex derives a trusted project's sandbox and loads its layers as with the file. "
            "A user config that cannot be read, declares hooks, sets a root `profile` key or "
            "sets `default_permissions` is refused, since what it holds cannot ride beside the "
            "flag. A `profile`, or overrides that select one, are refused with the flag, since "
            "codex then loads no profile."
        ),
        json_schema_extra=_cli("--ignore-user-config", 64, "bool"),
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
            "A profile other than the one lionagi writes for MCP server secrets is "
            "refused, named by `profile`, by a `profile` or `profiles` override, or "
            "by a root `profile` key in the codex config or in a project "
            "`.codex/config.toml` from the working directory up to its git root, "
            "because codex before 0.135 applies a profile's features after `--disable`. "
            "A working directory those configs cannot be read from -- one that cannot "
            "be resolved or does not exist -- is refused too, since containment cannot "
            "be enforced without knowing which configs apply. "
            "Off by default: the request reaches whatever the account and the "
            "config give it. Lifted by naming `account_connectors`. A `light` "
            "request is contained whether or not this is set, and refuses "
            "`account_connectors`."
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
        from ._codex_profile import (
            refuse_codex_profile_run_settings,
            resolve_codex_config_profile,
        )

        model = values.get("model")
        if not isinstance(model, str) or not model:
            return values
        env = reveal_redacted(values.get("env"))
        resolved = resolve_codex_config_profile(model, env if isinstance(env, Mapping) else None)
        if resolved is None:
            return values
        profile_model, profile_overrides = resolved
        if values.get("ignore_user_config"):
            refuse_codex_profile_run_settings(model, profile_overrides)
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

        hermetic = _as_declared_bool(
            values.get("ignore_user_config", cls.model_fields["ignore_user_config"].default)
        )
        # `is None`, not `not isinstance(..., str)`: only an ABSENT or null
        # effort is unset. A non-string is malformed input; it falls through
        # to the field's own `str | None` validation, which rejects it, rather
        # than being replaced by the derived default. A request that ignores the
        # user config derives nothing from it: the child never reads that file,
        # so there is no configured value to clamp.
        if not hermetic and values.get("reasoning_effort") is None:
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
    workload: SubagentWorkload = Field(
        default=DEFAULT_WORKLOAD,
        exclude=True,
        description=(
            "'light' runs codex in its workspace-write sandbox (writable in its working "
            "directory and the temporary directories that sandbox keeps writable) with no "
            "network, with the account's apps and plugins turned off as "
            "`contain_account_integrations` turns them off, and counts against the total and "
            "codex agent caps. The MCP servers it is given run outside that sandbox. 'heavy' "
            "(the default) keeps the configured "
            "sandbox and also counts against the heavy cap."
        ),
    )

    # ── validators ────────────────────────────────────────────────

    @field_validator("reasoning_effort", "plan_mode_reasoning_effort", mode="before")
    @classmethod
    def _refuse_an_effort_given_as_bytes(cls, value, info: ValidationInfo):
        # Field validation reads bytes as text, after the clamp has passed them by. Refused
        # here, not in the model validator: a ValueError there puts the whole input,
        # prompt included, into the validation error, and here it holds only this field.
        if isinstance(value, bytes | bytearray):
            raise ValueError(f"{info.field_name} must be a string, not {type(value).__name__}")
        return value

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
        # Reduced and kept here, not only checked: the field then holds one plain text, so
        # every later read (the containment check, the command line, the spawn) is that
        # same text and a path-like whose own read answers differently later cannot pass
        # the check and render as another path.
        return _plain_paths(check_add_dir_entries_safe(v, "add_dir"), "add_dir")

    @field_validator("images", mode="after")
    @classmethod
    def _validate_images(cls, v):
        return _plain_paths(check_paths_safe(v, "images"), "images")

    @field_validator("output_schema", "output_last_message", mode="before")
    @classmethod
    def _validate_output_paths(cls, v):
        if v is None:
            return v
        # Read once as `str.__str__(os.fspath(v))` and kept as that text, so the value
        # this check judges is the value the field carries and the command line renders; an
        # object whose own read answers a safe path now and another later has no second
        # read to answer.
        target = _plain_path(v, "output_schema/output_last_message")
        check_path_safe(target, "output_schema/output_last_message")
        return target

    def __init__(self, **data: Any) -> None:
        # The working directory decides which codex configs apply, so under
        # containment it is checked here. Constructed directly, the refusal is
        # raised whole: a ContainmentRefusedError raised inside a validator
        # would be wrapped in pydantic's ValidationError. Through model_validate
        # it still arrives wrapped.
        super().__init__(**data)
        self._refuse_an_unusable_working_directory()

    def _refuse_an_unusable_working_directory(self) -> None:
        """Refuse a working directory containment cannot read the configs of.

        A directory the walk cannot resolve (a symlink loop), that does not
        exist, or that this process cannot list and enter leaves the walk with
        nothing it can read, so a caller cannot tell which codex configs apply
        and containment cannot be enforced. Each is refused before the request
        is used, naming the directory and the reason, chained from the error
        the filesystem raised.
        """
        if not self._contains_account_integrations():
            return
        repo = str(self.repo)
        try:
            self.repo.resolve()
        except (RuntimeError, OSError) as exc:
            raise ContainmentRefusedError(
                f"contain_account_integrations cannot use the working directory "
                f"{repo!r}: it cannot be resolved ({exc})"
            ) from exc
        # The same validation the spawn path uses, so the refusal names the
        # directory the same way and is chained from the value error it raises.
        try:
            resolve_cli_workspace(self.repo, None)
        except (ValueError, OSError) as exc:
            raise ContainmentRefusedError(
                f"contain_account_integrations cannot use the working directory: {exc}"
            ) from exc
        if not os.access(self.repo, os.R_OK | os.X_OK):
            raise ContainmentRefusedError(
                f"contain_account_integrations cannot use the working directory "
                f"{repo!r}: this process cannot list and enter it"
            )

    @model_validator(mode="after")
    def _contain_path_fields_in_repo(self):
        try:
            repo_root = self.repo.resolve()
        except RuntimeError:
            if self._contains_account_integrations():
                # Deferred to __init__, which raises the refusal outside
                # pydantic's validation and so keeps its own type.
                return self
            raise
        # Each path is read the way the snapshot reads it, `str.__str__(os.fspath(x))`, so
        # the text this check contains is the text the command line renders: a path-like
        # value whose own `__str__` answers differently on a later read cannot pass here
        # and escape at the render.
        paths = _plain_path_fields(self)
        if paths["images"]:
            contain_paths_in_repo(paths["images"], repo_root, "images")
        if paths["output_schema"] is not None:
            contain_paths_in_repo([paths["output_schema"]], repo_root, "output_schema")
        if paths["output_last_message"] is not None:
            contain_paths_in_repo([paths["output_last_message"]], repo_root, "output_last_message")
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
        contained_refusal = False
        try:
            plain = _plain_request(self)
            _refuse_oversized_cli_inputs(
                plain.prompt,
                plain.system_prompt,
                plain.overrides,
                text_fields=plain.argument_text_fields(),
                contained=plain.contains_account_integrations(),
            )
        except ValueError as exc:
            refusal = CLIInputRefusedError(str(exc))
            contained_refusal = issubclass(type(exc), ContainmentRefusedError)
        if refusal is not None:
            if contained_refusal:
                # A judge finds a containment refusal on the error's chain, so the refusal
                # carries a fresh one as its cause: a key containment cannot read refuses
                # the judge's engine at construction as it does at the spawn.
                raise refusal from ContainmentRefusedError(str(refusal))
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
        # The refusal runs on the normalized snapshot, and again where the process would
        # start, so a request copied without validation cannot put both an
        # `ask_for_approval` policy and an `approval_policy` override on the line.
        plain = _plain_request(self)
        _refuse_ambiguous_approval(plain)
        _refuse_light_escape(plain)
        return self

    # ── workspace path ────────────────────────────────────────────

    def cwd(self) -> Path:
        return resolve_cli_workspace(self.repo, self.ws)

    # ── CLI command builder ───────────────────────────────────────

    def _settled(self, plain: _PlainRequest, update: dict[str, Any] | None = None):
        """A copy of this request whose checked fields are the snapshot's plain values.

        The declarative renderer reads a model, so the snapshot's values for the fields it
        renders are written onto a copy here; a value the reduction refused never reaches
        it.
        """
        return self.model_copy(update={**plain.updates(), **(update or {})})

    def as_cmd_args(
        self,
        overrides: Sequence[tuple[Any, Any]] | None = None,
        *,
        plain: _PlainRequest | None = None,
    ) -> list[str]:
        """Build the argument list for ``codex exec`` from one normalized snapshot.

        ``overrides`` is one read of ``config_overrides``; ``None`` reads it here. A
        caller that already reduced the request (the spawn path) hands its snapshot in as
        ``plain``, so that check and this render cannot read the fields twice. Either way
        every check below and every rendered argument reads one plain snapshot, so a field
        whose own methods answer differently on each read cannot be judged as one value and
        rendered as another.
        """
        if plain is None:
            plain = _plain_request(self, overrides)
        _refuse_ambiguous_approval(plain)
        _refuse_light_escape(plain)
        light = plain.workload == "light"
        if light:
            self._refuse_a_config_profile_for_light(plain)
        args: list[str] = ["exec", "--json"]
        cwd = self.cwd()

        args.extend(self._build_declarative_args(plain))
        carried = _ignored_user_config_carry(plain, cwd) if plain.ignore_user_config else None

        # A light agent writes only its working directory, so a linked worktree's git
        # directory is not made writable for it.
        for git_root in () if light else _linked_worktree_git_writable_roots(cwd):
            if str(git_root) not in plain.add_dir:
                args.extend(["--add-dir", str(git_root)])

        # Approval & sandbox: mutually exclusive hierarchy
        if light:
            # The refusal above has ruled out every setting that turns the sandbox off.
            if plain.ask_for_approval:
                rendered = _rendered_override_value(plain.ask_for_approval, "ask_for_approval")
                args.extend(["-c", f"approval_policy={rendered}"])
            args.extend(
                ["--sandbox", "read-only" if plain.sandbox == "read-only" else "workspace-write"]
            )
        elif plain.bypass_approvals:
            args.append("--dangerously-bypass-approvals-and-sandbox")
        elif plain.full_auto:
            # Codex 0.147.0 removed `--full-auto`, turning the earlier deprecation
            # warning into a hard argv error. Its deprecation notice prescribes
            # `--sandbox workspace-write`, which keeps the same sandbox posture.
            args.extend(["--sandbox", "workspace-write"])
        else:
            if plain.ask_for_approval:
                # The config key has the same mode vocabulary as the former CLI
                # flag and remains available in builds that no longer expose it.
                rendered = _rendered_override_value(plain.ask_for_approval, "ask_for_approval")
                args.extend(["-c", f"approval_policy={rendered}"])
            if plain.sandbox:
                args.extend(["-s", plain.sandbox])
            elif carried is not None and carried.sandbox_mode:
                # Without the user config codex falls back to its built-in read-only
                # sandbox, so the mode the user config sets is handed over here.
                args.extend(["-s", carried.sandbox_mode])

        # Web search: old `--search` flag was removed upstream (2026-04);
        # express intent via the `tool_search` feature flag instead.
        if plain.search:
            args.extend(["--enable", "tool_search"])
        else:
            args.extend(["--disable", "tool_search"])

        # Codex CLI has no --system-prompt flag; uses -c developer_instructions. The
        # value is read as TOML like every other `-c` value, so it is written as a TOML
        # string: written raw, a prompt that parses as TOML ("true", "42", '"quoted"')
        # would reach codex as that value instead of as its own text.
        if plain.system_prompt:
            rendered = _rendered_override_value(plain.system_prompt, "The system prompt")
            args.extend(["-c", f"developer_instructions={rendered}"])

        # Encoded like every other `-c` override below. While these fields were
        # a closed Literal of eight bare words, emitting them unencoded was
        # safe by construction; now that any string is accepted, what keeps an
        # arbitrary value inside its own override is codex's own leniency about
        # the right-hand side, which is not a contract lionagi owns.
        if plain.reasoning_effort:
            rendered = _rendered_override_value(plain.reasoning_effort, "reasoning_effort")
            args.extend(["-c", f"reasoning_effort={rendered}"])
        if plain.plan_mode_reasoning_effort:
            rendered = _rendered_override_value(
                plain.plan_mode_reasoning_effort, "plan_mode_reasoning_effort"
            )
            args.extend(["-c", f"plan_mode_reasoning_effort={rendered}"])

        if plain.fast_mode:
            args.extend(["-c", "service_tier=fast"])

        for image in plain.images:
            args.extend(["-i", image])

        containing = plain.contains_account_integrations()
        for index, (key, value) in enumerate(plain.overrides):
            if "=" in key:
                raise _equals_in_key_refusal(index)
            # Containment removes overrides under `features.apps` and
            # `features.plugins`, plus those keys from an inline `features`
            # table. Config profiles can contribute either form alongside the
            # caller. Drop them here rather than relying on flag order.
            if containing:
                kept, value = _contain_override(
                    key, value, f"The key of the config override entry {index}"
                )
                if not kept:
                    continue
            rendered = _rendered_override_value(
                value, f"The value of the config override entry {index}"
            )
            args.extend(["-c", f"{key}={rendered}"])

        # `--ignore-user-config` drops the whole user config, and with it the
        # `[sandbox_workspace_write]` settings, the `[hooks.state]` trust records and the
        # `[projects]` trust decisions: under the flag alone a workspace-write child has
        # no network, no extra writable root and a writable /tmp, codex finds
        # CODEX_HOME/hooks.json but runs none of its hooks, and a trusted project gets
        # the read-only default and none of its project layers. The flag is about which
        # servers and profiles the run loads, so these ride beside it. The trust tables
        # come after the overrides above: they already hold any entry those write,
        # merged, and a later `-c` of a key replaces what an earlier one set. Each is one
        # inline table, since codex splits a dotted key at every `.`, a directory's
        # included.
        for name, value in carried.workspace_write if carried is not None else ():
            rendered = _rendered_override_value(value, f"The carried {name}")
            args.extend(["-c", f"sandbox_workspace_write.{name}={rendered}"])
        if carried is not None and carried.hook_trust:
            rendered = _rendered_override_value(carried.hook_trust, "The hook trust records")
            args.extend(["-c", f"hooks.state={rendered}"])
        if carried is not None and carried.project_trust:
            rendered = _rendered_override_value(
                carried.project_trust, "The project trust decisions"
            )
            args.extend(["-c", f"projects={rendered}"])
        if light:
            # Pinned against the user's config, read by codex or carried above, which may
            # grant extra writable roots or the network to every workspace-write session.
            # Emitted after every caller override and carried setting, so codex, which keeps
            # the last `-c` of a key, reads these whatever came before.
            args.extend(["-c", "sandbox_workspace_write.writable_roots=[]"])
            args.extend(["-c", "sandbox_workspace_write.network_access=false"])

        # Working directory (always emit)
        args.extend(["-C", str(cwd)])

        # Prompt goes to stdin, not argv (a whole conversation can exceed the
        # OS arg-length limit). `-` is codex's stdin marker; it stays after
        # `--` so it's parsed as the prompt argument, never as a flag.
        args.extend(["--", "-"])

        return args

    def _contains_account_integrations(self) -> bool:
        return _plain_request(self).contains_account_integrations()

    def _argument_text_fields(self) -> list[tuple[str, str]]:
        """The fields ``as_cmd_args`` renders into an argument of their own, by name.

        Read by the construction and spawn checks so a lone surrogate in any of them is
        refused by name rather than reaching the CLI as a raw byte. The text comes from
        the normalized snapshot, so the check names the argument the command line builds.
        Path fields are left out of this list, not out of the checks: a path may
        legitimately hold a byte that is not UTF-8, and ``os.fsencode`` is what measures
        those at the process boundary, so the path fields are reduced in the snapshot and
        measured where the built command is.
        """
        return _plain_request(self).argument_text_fields()

    def _refuse_a_profile_under_containment(self, plain: _PlainRequest) -> None:
        # `--disable` sets the features in the root config. Codex before 0.135 (read at
        # the 0.120.0 and 0.130.0 tags) applies the active profile's `[features]` after
        # it, so a profile can turn either one back on; the active profile is `-p`, else a
        # root `profile` key in the user config or a trusted project's `.codex/config.toml`.
        # From 0.135 a profile is a config file layered under `-c`, and codex refuses a
        # root `profile` key itself. The profile lionagi writes for MCP server secrets
        # holds only `mcp_servers`.
        #
        # Every read below is of the normalized snapshot: `plain.profile` is plain text
        # and each override key is plain text, so the profile this check judges is the one
        # the command line renders.
        if plain.profile is not None and not is_generated_codex_profile(plain.profile):
            raise ValueError(
                f"contain_account_integrations cannot be combined with profile={plain.profile!r}: "
                "codex before 0.135 applies a profile's features after --disable, so the "
                "profile could turn apps or plugins back on"
            )
        for index, (key, _value) in enumerate(plain.overrides):
            # A key that holds '=' is refused by position where as_cmd_args renders the
            # overrides, after this check.
            if "=" in key:
                continue
            unreadable = None
            try:
                path = _override_key_path(key, f"The key of the config override entry {index}")
            except ValueError as exc:
                unreadable = str(exc)
            if unreadable is not None:
                # Raised after the handler, so the refusal keeps no error as its context.
                raise ContainmentRefusedError(unreadable)
            if path[0] in ("profile", "profiles"):
                raise ValueError(
                    "contain_account_integrations cannot be combined with the config "
                    f"override entry {index}: it selects or changes a profile, and codex "
                    "before 0.135 applies a profile's features after --disable"
                )
        if plain.profile is not None:
            # The profile lionagi writes is a file of its own, but codex before 0.135 reads `-p`
            # as a `[profiles.<name>]` table in the configs it merges, and one defined there under
            # this name could turn either feature back on.
            defined = codex_config_defining_profile(plain.profile, plain.env, self.cwd())
            if defined is not None:
                raise ValueError(
                    f"contain_account_integrations cannot be combined with profile={plain.profile!r} "
                    f"while {str(defined)!r} defines a profile of that name: codex before 0.135 "
                    "applies its features after --disable"
                )
        else:
            sources = [("the codex config", configured_codex_profile(plain.env))]
            project = project_codex_profile(self.cwd())
            if project is not None:
                sources.append((f"the project codex config {str(project[0])!r}", project[1]))
            for where, value in sources:
                if value is None:
                    continue
                # A key that is not a string is named by its type: its content is not echoed.
                if isinstance(value, str):
                    named = f"profile = {value!r}"
                else:
                    named = f"a root profile key of type {type(value).__name__}"
                raise ValueError(
                    f"contain_account_integrations cannot be combined with {named} in {where}: "
                    "with no -p, codex before 0.135 makes it the active profile and applies "
                    "its features after --disable (from 0.135 codex refuses the key itself)"
                )

    def _refuse_a_config_profile_for_light(self, plain: _PlainRequest) -> None:
        # A codex profile can set `sandbox_mode` and `sandbox_workspace_write`, and codex
        # before 0.135 applies the active one over the root config the light pins write to.
        # A config's `default_permissions` names a permissions profile codex applies in
        # place of the mode, so a user or project config that sets it is refused as well.
        # `_refuse_light_escape` has already refused a `-p` lionagi did not generate and every
        # override that names a profile; what is left is a profile chosen by the configs
        # codex reads, which only this process's view of the cwd and the child's env reach.
        # A config that cannot be read is refused too: it cannot be ruled out.
        refusal = None
        try:
            if plain.profile is not None:
                defined = codex_config_defining_profile(plain.profile, plain.env, self.cwd())
                if defined is not None:
                    refusal = (
                        f"{str(defined)!r} defines a profile named {plain.profile!r}, "
                        "which codex before 0.135 applies over the light sandbox"
                    )
            else:
                sources = [("the codex config", configured_codex_profile(plain.env))]
                project = project_codex_profile(self.cwd())
                if project is not None:
                    sources.append((f"the project codex config {str(project[0])!r}", project[1]))
                for where, value in sources:
                    if value is not None:
                        refusal = (
                            f"{where} sets a root profile key, whose profile codex before "
                            "0.135 makes active over the light sandbox"
                        )
                        break
            if refusal is None:
                found = codex_config_setting_default_permissions(plain.env, self.cwd())
                if found is not None:
                    refusal = (
                        f"{str(found)!r} sets `default_permissions`, a permissions profile "
                        "codex applies in place of the light sandbox"
                    )
        except (ValueError, OSError) as exc:
            refusal = str(exc)
        if refusal is not None:
            raise CLIInputRefusedError(
                "workload 'light' runs codex in its workspace-write sandbox with no network; "
                f"{refusal}. Declare the agent's workload 'heavy' instead."
            )

    def _build_declarative_args(self, plain: _PlainRequest) -> list[str]:
        if not plain.contains_account_integrations():
            return build_declarative_cli_args(self._settled(plain))
        try:
            self._refuse_a_profile_under_containment(plain)
        except ContainmentRefusedError:
            raise
        except (ValueError, OSError) as exc:
            # A combination that could let a profile turn a feature back on, and an override
            # key or a codex config the check cannot read to rule that out, are both refused.
            raise ContainmentRefusedError(str(exc)) from exc
        disabled = list(plain.disable_features)
        return build_declarative_cli_args(
            self._settled(
                plain,
                update={
                    "enable_features": [
                        f for f in plain.enable_features if f not in _ACCOUNT_INTEGRATION_FEATURES
                    ]
                    or None,
                    "disable_features": disabled
                    + [f for f in _ACCOUNT_INTEGRATION_FEATURES if f not in disabled],
                },
            )
        )


CodexSession = CLISession


@dataclass(frozen=True)
class _UserConfigCarry:
    """The settings of an ignored user config that ride beside `--ignore-user-config`."""

    sandbox_mode: str | None = None
    workspace_write: tuple[tuple[str, Any], ...] = ()
    hook_trust: dict[str, dict[str, Any]] | None = None
    project_trust: dict[str, dict[str, Any]] | None = None


_SANDBOX_MODES = frozenset(get_args(CodexSandboxMode))
# The events a config's `[hooks]` table declares, by the names codex reads (hook_config.rs);
# any other key there is ignored, `state` included.
_CODEX_HOOK_EVENTS = frozenset(
    {
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "Interrupt",
    }
)
# What decides the sandbox mode: `default_permissions` names a permissions profile that
# replaces `sandbox_mode` (config_toml.rs).
_SANDBOX_MODE_KEYS = (("sandbox_mode",), ("default_permissions",))
# The `[sandbox_workspace_write]` settings codex reads. A carried value beats a project
# layer's, so the first two, which widen the sandbox, ride only where no project layer sets
# them to another value; the last two narrow it, and ride whatever a project layer says.
_WIDENING_WORKSPACE_KEYS = ("writable_roots", "network_access")
_NARROWING_WORKSPACE_KEYS = ("exclude_tmpdir_env_var", "exclude_slash_tmp")
# The values of a `[projects."<dir>"]` entry's `trust_level` (config_types.rs `TrustLevel`).
_TRUST_LEVELS = frozenset({"trusted", "untrusted"})
# What Rust's `str::trim` strips, which codex applies to override keys and hook state keys:
# the Unicode White_Space set. Python's `str.strip` also strips U+001C to U+001F.
_TRIMMED = (
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def _ignored_user_config_carry(plain: _PlainRequest, cwd: Path) -> _UserConfigCarry:
    """Read the user config a run ignores for the settings that ride beside the flag.

    Some settings of CODEX_HOME/config.toml are about the run's sandbox and hooks rather
    than about which servers and profiles it loads: `sandbox_mode`, the
    `[sandbox_workspace_write]` settings, the `[hooks.state]` trust records and the
    `[projects]` trust decisions. Codex still finds CODEX_HOME/hooks.json under the flag,
    but runs a hook from it only when its trust record is in the user config or on the
    command line. With no sandbox mode set, codex gives a directory it has a trust
    decision for workspace-write, and it loads a project layer only when trusted.

    The sandbox mode and the settings that widen the sandbox ride unless the run's
    overrides set them, and only where no project layer sets them to another value
    (`_refuse_project_layer_disagreement`): codex applies a project layer only when it
    trusts the project, and a value on the command line beats the layer's. The settings
    that narrow the sandbox ride unless the overrides set them. The hook trust records and
    the project trust decisions ride merged with any the overrides write, field by field
    and theirs winning, as codex merges the user config's with the command line's; codex
    then matches the run's directory against them itself.

    What cannot ride refuses the run: hook events declared in the file itself, whose
    trust records name a file the run does not load; a root `profile` key, which codex
    0.130.0, given no `-p`, makes the active profile, whose sandbox settings replace the root
    ones (read at that tag: `get_config_profile` falls back to the key, and the profile's
    `sandbox_mode` is taken before the root's), and which codex 0.162.1 still declares in its
    config schema; `default_permissions`, a permissions profile codex applies in place
    of `sandbox_mode`; and a file that exists but cannot be read or parsed (this Python's TOML
    parser may refuse TOML 1.1 forms codex reads), since the hooks it declares, if any, would be
    dropped unseen. So does a profile the run itself names, on the request or in its overrides:
    codex loads no user config under the flag, and a profile is user config.
    """
    if plain.profile is not None:
        raise ConfigurationError(
            f"Codex cannot start with --ignore-user-config and profile={plain.profile!r}: "
            "under the flag codex loads no user config, and the profile is user config (its "
            "file in CODEX_HOME, or its table in config.toml), so none of its settings would "
            "apply. Set those settings in the run's config overrides and drop the profile, or "
            "run without ignore_user_config."
        )
    flags = _command_line_layer(plain)
    if _sets(flags, ("profile",)):
        # A value that is not a string is named by its type: its content is not echoed.
        selected = flags["profile"]
        named = (
            f"profile = {selected!r}, which selects a profile"
            if isinstance(selected, str)
            else f"a profile key of type {type(selected).__name__}"
        )
        raise ConfigurationError(
            f"Codex cannot start with --ignore-user-config: the run's config overrides set "
            f"{named}, and a run that loads no user config takes its settings from the "
            "overrides themselves. Set those settings in the overrides directly and drop the "
            "profile key."
        )
    config_path = codex_home_for_env(plain.env) / "config.toml"
    try:
        config = _read_codex_config(config_path)
    except ValueError as exc:
        from ._codex_project_layers import _READS_TOML_1_1

        remedy = (
            "Make the file one this Python's TOML parser reads"
            + ("" if _READS_TOML_1_1 else " (codex also reads TOML 1.1 forms)")
            if os.access(config_path, os.R_OK)
            else "Make the file readable, and its folder searchable, by this process"
        )
        raise ConfigurationError(
            f"Codex cannot start with --ignore-user-config: {exc}. The hooks it may declare "
            "cannot be checked, and its sandbox settings and trust records cannot ride "
            f"beside the flag. {remedy}, or run without ignore_user_config."
        ) from None
    # A dangling symlink is no file, as codex sees it. A directory is a file codex fails
    # to read, and a FIFO one it blocks on opening.
    if config is None and os.path.exists(config_path):
        raise ConfigurationError(
            f"Codex cannot start with --ignore-user-config: the codex config "
            f"{str(config_path)!r} is not a regular file, so the hooks it may declare cannot be "
            "checked and its settings cannot ride beside the flag. Replace it with a regular "
            "file, or remove it."
        )
    config = config or {}
    hooks = config.get("hooks")
    hooks = hooks if isinstance(hooks, Mapping) else {}
    declared = sorted(
        str(name) for name, groups in hooks.items() if name in _CODEX_HOOK_EVENTS and groups
    )
    if declared:
        raise ConfigurationError(
            f"Codex cannot start with --ignore-user-config: {config_path} declares hooks "
            f"({', '.join(declared)}), which the flag drops, and their trust records name that "
            "file, so they cannot ride beside it. Declared in hooks.json in the same directory, "
            "which codex reads under the flag, they run once trusted."
        )
    profile = config.get("profile")
    if profile is not None:
        # A key that is not a string is named by its type: its content is not echoed.
        named = (
            f"profile = {profile!r}"
            if isinstance(profile, str)
            else f"a root profile key of type {type(profile).__name__}"
        )
        raise ConfigurationError(
            f"Codex is not started with --ignore-user-config: {config_path} sets {named}. Codex "
            "0.130.0, given no -p, makes it the active profile, whose sandbox settings replace "
            "the root ones, and codex 0.162.1 still declares the key. The flag hides the key, "
            "so the run would start without what codex does with it. Remove the key, and set "
            "the sandbox in the run's config overrides."
        )
    if config.get("default_permissions") is not None and not _sets(flags, ("default_permissions",)):
        raise ConfigurationError(
            f"Codex cannot start with --ignore-user-config: {config_path} sets "
            "`default_permissions`, a permissions profile codex applies in place of "
            "`sandbox_mode`, which the flag drops and which cannot ride beside it. Set "
            "`default_permissions`, and the `permissions` profile it names, in the run's "
            "config overrides, or remove it from the file."
        )
    mode = config.get("sandbox_mode")
    if mode is not None and not (isinstance(mode, str) and mode in _SANDBOX_MODES):
        log.warning(
            "the ignored codex config %s sets sandbox_mode %r, which is not a mode codex "
            "reads, so it does not ride beside the flag",
            config_path,
            mode,
        )
        mode = None
    if (
        any(_sets(flags, key) for key in _SANDBOX_MODE_KEYS)
        or plain.sandbox
        or plain.full_auto
        or plain.bypass_approvals
    ):
        mode = None  # the request sets the mode itself, so a carried one would not render
    table = config.get("sandbox_workspace_write")
    table = table if isinstance(table, Mapping) else {}
    widening, narrowing = (
        {
            name: value
            for name in names
            if (value := _workspace_write_value(name, table.get(name), config_path)) is not None
            and not _sets(flags, ("sandbox_workspace_write", name))
        }
        for names in (_WIDENING_WORKSPACE_KEYS, _NARROWING_WORKSPACE_KEYS)
    )
    carried: dict[tuple[str, ...], Any] = {("sandbox_mode",): mode} if mode is not None else {}
    carried.update((("sandbox_workspace_write", name), value) for name, value in widening.items())
    if carried:
        _refuse_project_layer_disagreement(plain, cwd, config_path, carried)
    return _UserConfigCarry(
        sandbox_mode=mode,
        workspace_write=tuple({**widening, **narrowing}.items()),
        hook_trust=_merged_hook_trust(_hook_trust_records(hooks.get("state")), flags),
        project_trust=_merged_project_trust(_project_trust_records(config.get("projects")), flags),
    )


def _workspace_write_value(name: str, value: Any, config_path: Path) -> Any:
    """The value of the user config's `sandbox_workspace_write.<name>` that rides beside
    the flag, or ``None``: a flag only when it is true, which codex's default is not, and
    of the writable roots only the absolute paths. A relative root is left out rather than
    risk its resolving against another directory once it is on the command line."""
    if name != "writable_roots":
        return True if value is True else None
    if value is None:
        return None
    listed = value if isinstance(value, list) else [value]
    roots = [root for root in listed if isinstance(root, str) and os.path.isabs(root)]
    if len(roots) < len(listed) or listed is not value:
        log.warning(
            "the ignored codex config %s lists writable roots that are not absolute paths "
            "(%r), so only %r ride beside the flag",
            config_path,
            value,
            roots,
        )
    return roots or None


def _hook_trust_records(state: Any) -> dict[str, dict[str, Any]]:
    """The `[hooks.state]` entries codex would read from the user config.

    Codex skips an entry whose `enabled` is not a boolean or whose `trusted_hash` is
    not a string, so such an entry is left out here too rather than half carried.
    """
    if not isinstance(state, Mapping):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for key, entry in state.items():
        if not _is_hook_record(entry):
            continue
        record = {
            name: entry[name] for name in ("enabled", "trusted_hash") if entry.get(name) is not None
        }
        if record:
            records[str(key)] = record
    return records


def _is_hook_record(entry: Any) -> bool:
    """Whether codex reads ``entry`` as a hook state record: a table whose `enabled`, if
    set, is a boolean and whose `trusted_hash`, if set, is a string."""
    if not isinstance(entry, Mapping):
        return False
    enabled, trusted_hash = entry.get("enabled"), entry.get("trusted_hash")
    return (enabled is None or isinstance(enabled, bool)) and (
        trusted_hash is None or isinstance(trusted_hash, str)
    )


def _merged_hook_trust(
    records: dict[str, dict[str, Any]], flags: Mapping[str, Any]
) -> dict[str, dict[str, Any]] | None:
    """The `hooks.state` table the run's command line ends with, or ``None`` when the user
    config has no trust records to carry.

    Codex reads hook state from the user config and then from the command line, field by
    field, the later winning, with each key trimmed and an empty one skipped. The flag
    empties the first, so the carried records and the overrides' own are merged here in
    that order and handed over as one table. Overrides that make `hooks` or `hooks.state`
    something other than a table, or write a record codex cannot read, leave nothing to
    merge into, and the run is refused rather than handed a table they did not write.
    """
    if not records:
        return None
    hooks = flags.get("hooks", {})
    state = hooks.get("state", {}) if isinstance(hooks, Mapping) else None
    if not isinstance(state, Mapping) or not all(map(_is_hook_record, state.values())):
        raise ConfigurationError(
            "Codex cannot start with --ignore-user-config: the run's config overrides write "
            "hook state that is not a table of records with a boolean `enabled` and a string "
            "`trusted_hash`, so the user config's hook trust records cannot be merged with it."
        )
    merged: dict[str, dict[str, Any]] = {}
    for key, entry in (*records.items(), *state.items()):
        key = str(key).strip(_TRIMMED)
        if key:
            fields = {
                name: entry[name]
                for name in ("enabled", "trusted_hash")
                if entry.get(name) is not None
            }
            merged[key] = {**merged.get(key, {}), **fields}
    return merged


def _project_trust_records(projects: Any) -> dict[str, dict[str, Any]]:
    """The `[projects]` entries codex would read from the user config, each with its
    `trust_level` when that is one codex reads. An entry without one is kept, empty:
    codex lets an entry for the run's directory shadow one for its repository root
    whether or not it sets a level. One with a level codex cannot read is left out."""
    if not isinstance(projects, Mapping):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for key, entry in projects.items():
        if _is_project_record(entry):
            level = entry.get("trust_level")
            records[str(key)] = {} if level is None else {"trust_level": level}
    return records


def _is_project_record(entry: Any) -> bool:
    """Whether codex reads ``entry`` as a `[projects]` entry: a table whose
    `trust_level`, if set, is one of codex's trust levels."""
    if not isinstance(entry, Mapping):
        return False
    level = entry.get("trust_level")
    return level is None or (isinstance(level, str) and level in _TRUST_LEVELS)


def _merged_project_trust(
    records: dict[str, dict[str, Any]], flags: Mapping[str, Any]
) -> dict[str, dict[str, Any]] | None:
    """The `projects` table the run's command line ends with, or ``None`` when the user
    config has no entries to carry.

    Codex merges the user config's `projects` with the command line's, field by field,
    the later winning, and keys an entry by the directory exactly as written. Overrides
    that make `projects` something other than a table of entries codex reads leave
    nothing to merge into, and the run is refused, as for the hook trust records.
    """
    if not records:
        return None
    state = flags.get("projects", {})
    if not isinstance(state, Mapping) or not all(map(_is_project_record, state.values())):
        raise ConfigurationError(
            "Codex cannot start with --ignore-user-config: the run's config overrides write "
            "`projects` that is not a table of entries with a `trust_level` of "
            f"{' or '.join(map(repr, sorted(_TRUST_LEVELS)))}, so the user config's project "
            "trust decisions cannot be merged with it."
        )
    merged: dict[str, dict[str, Any]] = {}
    for key, entry in (*records.items(), *state.items()):
        level = entry.get("trust_level")
        merged[str(key)] = {
            **merged.get(str(key), {}),
            **({} if level is None else {"trust_level": level}),
        }
    return merged


def _command_line_layer(plain: _PlainRequest) -> dict[str, Any]:
    """The table codex builds from the run's `-c` overrides, as its loader builds it: the
    key trimmed and split at every `.`, quotes included, and a later override replacing
    whatever an earlier one left at its key. (Codex merges instead at a few
    `features.<name>` keys; nothing read from this table is one.) Codex reads any key
    this way, one TOML cannot read included, such as `projects./home/u/repo.trust_level`,
    so none is refused here."""
    root: dict[str, Any] = {}
    for key, value in plain.overrides:
        *parents, last = key.strip(_TRIMMED).split(".")
        table = root
        for part in parents:
            if not isinstance(table.get(part), dict):
                table[part] = {}
            table = table[part]
        table[last] = copy(value)  # a later dotted key writes into it
    return root


def _sets(table: Mapping[str, Any], key_path: Sequence[str]) -> bool:
    """Whether ``table`` holds a value at ``key_path``."""
    value: Any = table
    for part in key_path:
        if not isinstance(value, Mapping) or part not in value:
            return False
        value = value[part]
    return True


def _refuse_project_layer_disagreement(
    plain: _PlainRequest, cwd: Path, config_path: Path, carried: Mapping[tuple[str, ...], Any]
) -> None:
    """Refuse a run when a project layer sets a carried setting to another value.

    Codex applies a project layer only when it trusts the project, and then the layer's
    value beats the user config's; a carried value, on the command line, beats both. The
    trust decision is codex's, made from the layer's directory, the project and repository
    roots and managed configuration, so a carried value is exact only where no layer says
    otherwise: a layer that sets the same value agrees whichever way codex decides. A layer
    that sets another value, one that names a permissions profile in place of the mode, one this
    Python's TOML parser refuses (codex also reads TOML 1.1 forms), and project layers that cannot
    be determined all refuse the run. Setting the key in the run's overrides keeps it from riding,
    and codex then decides between the overrides and the layer as it does without the flag. A
    layer's root `profile` key is no concern here: codex drops `profile` and `profiles` from project
    layers (its project-local denylist, read at the 0.130.0 to 0.162.1 tags).
    """
    try:
        layers = list(project_layers(cwd, os.environ if plain.env is None else plain.env))
    except ConfigurationError as exc:
        raise ConfigurationError(
            f"Codex cannot start under cwd {cwd} with --ignore-user-config: {exc}."
        ) from exc
    for layer_path, text in layers:
        try:
            table: Mapping[str, Any] | None = _tomllib.loads(text)
        except (_tomllib.TOMLDecodeError, RecursionError) as exc:
            table, failure = None, parse_failure(exc)
        for key_path, value in carried.items():
            dotted = ".".join(key_path)
            if table is None:
                how = f"cannot be parsed here ({failure}), so its setting is unknown"
            elif key_path == ("sandbox_mode",) and _sets(table, ("default_permissions",)):
                how = "names a permissions profile in its place"
            elif not _sets(table, key_path):
                continue
            else:
                layer_value: Any = table
                for part in key_path:
                    layer_value = layer_value[part]
                if layer_value == value:
                    continue
                how = f"sets it to another value, {layer_value!r}"
            raise ConfigurationError(
                f"Codex cannot start under cwd {cwd} with --ignore-user-config: {config_path} "
                f"sets `{dotted}` to {value!r}, which rides beside the flag, and the project "
                f"config {layer_path} {how}. Codex applies the project's setting only when it "
                f"trusts the project, so which one the run gets is not decided here. Set "
                f"`{dotted}` in the run's config overrides."
            )


def refuse_declaring_project_layer(plain: _PlainRequest, cwd: Path) -> None:
    """Refuse a run that ignores the user config when one of its project layers declares
    `mcp_servers`, naming the file and the line, or when its project layers cannot be
    determined. Codex loads project layers under `--ignore-user-config`, and a trusted one
    adds MCP servers past the run's own set."""
    try:
        declared = declaring_project_layer(cwd, os.environ if plain.env is None else plain.env)
    except ConfigurationError as exc:
        raise ConfigurationError(
            f"Codex cannot start under cwd {cwd} with --ignore-user-config: {exc}."
        ) from exc
    if declared is not None:
        config_file, line_number, shown = declared
        where = (
            f"{config_file} (line {line_number}) is a Codex project config declaring "
            "`mcp_servers`, which a trusted project layer loads past the run's own server set"
            if shown
            else f"{config_file} is a Codex project config in which line {line_number} reads as "
            "a `mcp_servers` declaration that the parse does not confirm, and where the "
            "file declares the key, a trusted project layer loads it past the run's own server set"
        )
        raise ConfigurationError(
            f"Codex cannot start under cwd {cwd} with --ignore-user-config: {where}, which the "
            "flag does not suppress. Remove the declaration from that file, or run without "
            "ignore_user_config."
        )


# --------------------------------------------------------------------------- NDJSON stream


# TODO: migrate create_subprocess_exec + wait_for to anyio
async def _ndjson_from_cli(request: CodexCodeRequest):
    if CODEX_CLI is None:
        raise RuntimeError("Codex CLI not found. Install with: npm i -g @openai/codex")
    # model_copy(update=...) skips validation, so the request is reduced again here, where
    # its inputs reach the process: every check below and the command line read one
    # normalized snapshot, so a copied request cannot carry an object a check judges one
    # way and the renderer another.
    plain = _plain_request(request)
    _refuse_ambiguous_approval(plain)
    _refuse_light_escape(plain)
    _refuse_oversized_cli_inputs(
        plain.prompt,
        plain.system_prompt,
        plain.overrides,
        text_fields=plain.argument_text_fields(),
        contained=plain.contains_account_integrations(),
    )
    if plain.ignore_user_config:
        # Every `codex exec` reads its project layers afresh, so a layer written since the
        # request was built is checked here, where the process would start.
        refuse_declaring_project_layer(plain, request.cwd())
    cmd = [CODEX_CLI, *request.as_cmd_args(plain=plain)]
    _refuse_oversized_command(cmd)
    # Do NOT pass cwd here — Codex CLI already gets the workspace via -C <repo>;
    # setting both double-resolves to 'repo/repo'. See docs/internals/runtime.md.
    async with contextlib.aclosing(
        ndjson_from_cli(
            cmd,
            env=plain.env,
            # The plain prompt the checks measured, so the bytes written to the child's
            # stdin are the bytes those checks read.
            stdin_data=plain.prompt,
            on_spawn=request.on_spawn,
            engine="codex",
            workload=plain.workload,
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
    _start_monotonic = current_time()

    # turn.completed reports usage/cost as a running total-to-date, not a
    # per-turn delta; track the last-seen cumulative values so only the
    # marginal delta is ever exposed per event. See
    # docs/internals/providers.md#codex-turn-completed-usage-delta.
    _prev_input_tokens = 0
    _prev_output_tokens = 0
    _prev_cost: float = 0.0
    _seen_cost = False
    # Text of the turn's last non-empty agent text since the last turn end; the
    # call-markup check reads only the last one, when a turn completes or the
    # stream ends. Code regions of that text are removed before matching.
    _last_agent_text: str | None = None

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

                # A completed call (or its result) after a message means the
                # turn went on past it, so a markup message is superseded and
                # must not fail the turn. Other items leave the text as it was.
                if item_type in _CALL_ITEM_TYPES:
                    _last_agent_text = None

                if item_type == "agent_message":
                    text = item.get("text", "")
                    session.messages.append(item)
                    # A message with no visible text does not become the turn's
                    # last text; it does not replace a message that carried one.
                    if isinstance(text, str) and text.strip():
                        _last_agent_text = text
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

                # The turn end reads the turn's pending text: markup in it
                # means the model wrote a call as text, no call ran, and the
                # turn still completed. Markup in an earlier message was
                # already superseded by a later one.
                if _has_tool_call_markup(_last_agent_text or ""):
                    yield _call_markup_error_chunk(session, obj)
                _last_agent_text = None

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
                            f"{_EMPTY_ERROR_PAYLOAD}; event type={typ!r})"
                            if not any(err.values())
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

                # Benign-EOS sentinel on resumed sessions — see docs/internals/runtime.md
                # for the exact 3-condition classification. It stands in for
                # turn.completed on a stream that ends there, so it closes the
                # turn: it reads and clears the pending text the way a turn end
                # does, then emits its own non-error chunk. A consumer that
                # stops at that chunk never reaches the check after the loop.
                _is_benign_eos = (
                    typ == "error"
                    and "error" in obj  # a bare {"type": "error"} is malformed, not EOF
                    and _raw_err == {}  # null normalised to {} must NOT qualify
                    and not any(k in obj for k in ("code", "message", "status"))
                )
                if _is_benign_eos:
                    # The sentinel ends a turn, so it reads the pending text as
                    # the turn end does. A consumer that stops at the sentinel
                    # never reaches the stream-end check below.
                    if _has_tool_call_markup(_last_agent_text or ""):
                        yield _call_markup_error_chunk(session, obj)
                    _last_agent_text = None
                    sc = StreamChunk(
                        type="error",
                        content=_err_message,
                        is_error=False,
                        metadata={**obj, "benign_eos": True},
                    )
                    session.chunks.append(sc)
                    yield sc
                    continue

                session.is_error = True
                session.result = _err_message
                # The turn is over: a real failure replaces the turn's pending
                # text, as turn.completed does, so a later turn's end does not
                # read a message this failing turn left behind.
                _last_agent_text = None
                if request.verbose_output:
                    log.error("Codex error: %s", session.result)
                sc = StreamChunk(
                    type="error",
                    content=session.result,
                    is_error=True,
                    metadata=dict(obj),
                )
                session.chunks.append(sc)
                yield sc

            # -- legacy event types (older CLI versions) --
            elif typ in ("message", "assistant", "agent"):
                msg = obj.get("message", obj)
                session.messages.append(msg)

                content = msg.get("content", "")
                if isinstance(content, str):
                    if content and content.strip():
                        _last_agent_text = content
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
                        if btype in ("text", "output_text"):
                            text = blk.get("text", "")
                            if isinstance(text, str) and text.strip():
                                _last_agent_text = text
                            if on_text:
                                await maybe_await(on_text(text))
                            if request.verbose_output:
                                _pp_text(text, theme)
                            sc = StreamChunk(type="text", content=text, metadata=obj)
                            session.chunks.append(sc)
                            yield sc
                        elif btype in ("tool_use", "function_call"):
                            # A written block is a real call block, so any
                            # message seen before it is not the turn's last text.
                            _last_agent_text = None
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
                # The event's text is read only when it is a string, exactly as
                # a message's text is: a non-string value is not read.
                _raw_event_result = obj.get(
                    "result",
                    obj.get("response", obj.get("text", "")),
                )
                _event_result = (
                    _raw_event_result.strip() if isinstance(_raw_event_result, str) else ""
                )
                session.usage = obj.get("usage", obj.get("stats", {}))
                session.total_cost_usd = obj.get("total_cost_usd", obj.get("cost"))
                session.num_turns = obj.get("num_turns", obj.get("turns"))
                session.duration_ms = obj.get("duration_ms", obj.get("duration"))
                # The event's text is its own outcome, so it does not replace
                # the result of a failure already recorded; the event's own
                # failure, or its success, cannot retract one either.
                if not session.is_error and _event_result:
                    session.result = _event_result
                # The event's own text is read like a message: markup there
                # becomes the turn's pending text, which the stream end reads.
                # Clean text does not replace pending markup.
                if _event_result and _has_tool_call_markup(_raw_event_result):
                    _last_agent_text = _raw_event_result
                _event_failed = bool(obj.get("is_error", obj.get("error") is not None))
                session.is_error = session.is_error or _event_failed
                # A result event that reports its own failure names it in its
                # text, as an error event does, so it is that failure's chunk.
                if _event_failed and _event_result:
                    esc = StreamChunk(
                        type="error",
                        content=_event_result,
                        is_error=True,
                        metadata=dict(obj),
                    )
                    session.chunks.append(esc)
                    yield esc

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

    # A stream that is exhausted mid-turn leaves the turn's pending text
    # unread. turn.completed and the sentinel read and clear it, and a real
    # failure clears it, so this reads what is left: a stream that ended
    # without a turn end. A consumer that stops reading early never gets here.
    if _has_tool_call_markup(_last_agent_text or ""):
        yield _call_markup_error_chunk(session, {})
        _last_agent_text = None

    if not session.result:
        parts = [c.content for c in session.chunks if c.type == "text" and c.content]
        if parts:
            session.result = "\n".join(parts)
    if session.num_turns is None and session.messages:
        session.num_turns = len(session.messages)
    if session.duration_ms is None:
        session.duration_ms = int((current_time() - _start_monotonic) * 1000)

    if on_final:
        await maybe_await(on_final(session))
    if request.verbose_output:
        _pp_final(session, theme)

    yield session


codex_log = log


CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-6.1-sol": 1_050_000,
    "gpt-6.1-terra": 1_050_000,
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
                    # Only a failing error chunk already stands for the verdict;
                    # a non-error one (the end-of-stream sentinel) does not
                    # carry it, so the fallback still fires beside it.
                    if item.is_error and not any(
                        c.type == "error" and c.is_error for c in item.chunks
                    ):
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
        if session.is_error:
            # Classified on every failure the stream reported, in order: each
            # error event's text and a failing result event's text, never the
            # turn's assistant text or a failed command's output. An event whose
            # payload held nothing counts only when no event named the failure.
            failures = list(
                dict.fromkeys(
                    c.content
                    for c in session.chunks
                    if c.type == "error" and c.is_error and c.content
                )
            )
            named = [t for t in failures if not t.startswith(_EMPTY_ERROR_PAYLOAD)]
            if session.session_id:
                self.session_id = session.session_id
            raise classify_provider_error(
                "\n".join(named or failures) or "Codex session failed",
                status=session.error_status,
            )
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

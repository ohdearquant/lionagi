# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Codex rejects an oversized or unencodable input before starting the CLI.

A long branch sends its rendered history with each turn, so its input can exceed the CLI limit
even when the new instruction is short. A refusal at construction is raised as a
``CLIInputRefusedError``, not a ValueError, so pydantic does not wrap it in a validation
error that carries the whole input.
"""

from __future__ import annotations

import sys

import pydantic
import pytest

from lionagi._errors import CLIInputRefusedError
from lionagi.providers.openai import codex as codex_mod
from lionagi.providers.openai.codex import (
    CODEX_MAX_TURN_INPUT_CHARS,
    CodexCodeRequest,
    toml_override_value,
)

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib
    tomllib = None  # type: ignore[assignment]

ARGUMENT_MAX_BYTES = codex_mod._CLI_ARGUMENT_MAX_BYTES
# developer_instructions=<system prompt> is one argument; the prefix counts toward it.
PREFIX = len("developer_instructions=")


def test_the_limits_are_the_documented_values():
    assert CODEX_MAX_TURN_INPUT_CHARS == 1_048_576
    assert ARGUMENT_MAX_BYTES == 1_048_576


def test_a_prompt_at_the_limit_is_taken_and_one_past_it_is_refused(tmp_path):
    assert len(CodexCodeRequest(prompt="x" * CODEX_MAX_TURN_INPUT_CHARS, repo=tmp_path).prompt) == (
        CODEX_MAX_TURN_INPUT_CHARS
    )

    with pytest.raises(CLIInputRefusedError) as info:
        CodexCodeRequest(prompt="x" * (CODEX_MAX_TURN_INPUT_CHARS + 1), repo=tmp_path)

    message = str(info.value)
    assert f"{CODEX_MAX_TURN_INPUT_CHARS + 1:,} characters" in message
    assert f"limit of {CODEX_MAX_TURN_INPUT_CHARS:,}" in message
    assert "start a new session" in message


def test_the_turn_input_limit_counts_unicode_characters(tmp_path):
    prompt = "漢" * CODEX_MAX_TURN_INPUT_CHARS

    assert len(prompt.encode("utf-8")) > CODEX_MAX_TURN_INPUT_CHARS
    assert CodexCodeRequest(prompt=prompt, repo=tmp_path).prompt == prompt


def test_the_limit_is_measured_on_the_input_the_messages_join_into(tmp_path):
    """Each message is under the limit; the joined input is not, and that is what the CLI gets."""
    half = "y" * (CODEX_MAX_TURN_INPUT_CHARS // 2)
    messages = [
        {"role": "system", "content": "system instructions"},  # not part of the turn input
        {"role": "user", "content": half},
        {"role": "assistant", "content": half},
    ]
    with pytest.raises(CLIInputRefusedError, match="past the CLI's limit"):
        CodexCodeRequest(messages=messages, repo=tmp_path)

    # the same two halves less one character each fit, with the newline that joins them
    messages[1]["content"] = messages[2]["content"] = half[:-1]
    assert len(CodexCodeRequest(messages=messages, repo=tmp_path).prompt) == (
        CODEX_MAX_TURN_INPUT_CHARS - 1
    )


@pytest.mark.asyncio
async def test_a_request_copied_past_the_limit_is_refused_where_the_process_would_start(
    tmp_path, monkeypatch
):
    """model_copy(update=...) skips the request's validation and the exported stream takes such
    a request, so the input is checked again before the process starts."""

    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    request = CodexCodeRequest(prompt="x", repo=tmp_path).model_copy(
        update={"prompt": "x" * (CODEX_MAX_TURN_INPUT_CHARS + 1)}
    )

    with pytest.raises(ValueError, match="past the CLI's limit"):
        async for _ in codex_mod.stream_codex_cli(request):
            pass


@pytest.mark.asyncio
async def test_a_copied_large_system_prompt_is_refused_before_the_cli_starts(tmp_path, monkeypatch):
    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"system_prompt": "s" * (ARGUMENT_MAX_BYTES - PREFIX)}
    )

    with pytest.raises(ValueError, match="system prompt.*command-line argument"):
        async for _ in codex_mod.stream_codex_cli(request):
            pass


def test_a_system_message_too_long_for_one_argument_is_refused(tmp_path):
    with pytest.raises(CLIInputRefusedError, match="system prompt.*command-line argument"):
        CodexCodeRequest(
            messages=[
                {"role": "system", "content": "s" * (ARGUMENT_MAX_BYTES - PREFIX)},
                {"role": "user", "content": "hi"},
            ],
            repo=tmp_path,
        )


def test_the_system_prompt_is_bounded_as_the_argument_it_becomes(tmp_path):
    """The bound is on developer_instructions=<system prompt>, in its UTF-8 bytes, which is
    what the operating system measures for text that holds no surrogate; the turn input is
    bounded in characters."""
    just_under = "s" * (ARGUMENT_MAX_BYTES - PREFIX - 1)
    request = CodexCodeRequest(prompt="hi", system_prompt=just_under, repo=tmp_path)
    assert request.system_prompt == just_under
    argument = next(a for a in request.as_cmd_args() if a.startswith("developer_instructions="))
    assert len(argument.encode("utf-8")) == ARGUMENT_MAX_BYTES - 1

    with pytest.raises(CLIInputRefusedError, match=f"argument of {ARGUMENT_MAX_BYTES:,} bytes"):
        CodexCodeRequest(prompt="hi", system_prompt=just_under + "s", repo=tmp_path)

    wide = "漢" * ((ARGUMENT_MAX_BYTES - PREFIX) // 3 + 1)
    assert len(wide) < ARGUMENT_MAX_BYTES < len(wide.encode("utf-8")) + PREFIX
    size = len(wide.encode("utf-8")) + PREFIX
    with pytest.raises(CLIInputRefusedError, match=f"argument of {size:,} bytes,"):
        CodexCodeRequest(prompt="hi", system_prompt=wide, repo=tmp_path)


def test_a_config_override_too_long_for_one_argument_is_refused(tmp_path):
    """A `-c key=value` override is one argument too, developer_instructions included."""
    key = "developer_instructions"
    # toml_override_value quotes a string: two quote characters.
    fits = "s" * (ARGUMENT_MAX_BYTES - len(key) - 1 - 2 - 1)
    request = CodexCodeRequest(prompt="hi", config_overrides={key: fits}, repo=tmp_path)
    argument = next(a for a in request.as_cmd_args() if a.startswith(f"{key}="))
    assert len(argument.encode("utf-8")) == ARGUMENT_MAX_BYTES - 1

    with pytest.raises(CLIInputRefusedError, match="config override 'developer_instructions'"):
        CodexCodeRequest(prompt="hi", config_overrides={key: fits + "s"}, repo=tmp_path)


@pytest.mark.asyncio
async def test_a_copied_large_config_override_is_refused_before_the_cli_starts(
    tmp_path, monkeypatch
):
    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"config_overrides": {"k": "v" * ARGUMENT_MAX_BYTES}}
    )

    with pytest.raises(ValueError, match="config override 'k'"):
        async for _ in codex_mod.stream_codex_cli(request):
            pass


@pytest.mark.asyncio
async def test_a_branch_whose_transcript_is_past_the_limit_starts_nothing_and_gains_no_reply(
    tmp_path, monkeypatch
):
    """A long branch sends its rendered history with the next turn. Nothing is spawned, the
    existing messages remain unchanged, and no reply is recorded.

    The run commits its instruction before the model call, as it does for any turn that fails,
    so the refused instruction may be the one message added.

    The transcript alternates turns as a real one does: instructions with no reply between them
    are not all sent, so stacking them would test a shorter input than it seems to."""
    from lionagi import Branch
    from lionagi.cli._providers import build_imodel_from_spec
    from lionagi.protocols.messages import Instruction

    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "_ndjson_from_cli", spawned)
    branch = Branch(chat_model=build_imodel_from_spec("codex/gpt-5.4"))
    quarter = CODEX_MAX_TURN_INPUT_CHARS // 4
    for _ in range(2):
        branch.msgs.add_message(instruction="z" * quarter)
        branch.msgs.add_message(assistant_response="y" * quarter)
    prior = list(branch.messages)

    with pytest.raises(CLIInputRefusedError, match="past the CLI's limit"):
        await branch.operate(instruction="go on")

    messages = list(branch.messages)
    assert messages[: len(prior)] == prior
    added = messages[len(prior) :]
    assert len(added) <= 1
    assert all(isinstance(m, Instruction) for m in added)


def _refuse_a_spawn(monkeypatch):
    def spawned(*_args, **_kwargs):
        raise AssertionError("the Codex CLI was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update,named",
    [
        ({"reasoning_effort": "x" * ARGUMENT_MAX_BYTES}, "-c override 'reasoning_effort'"),
        (
            {"plan_mode_reasoning_effort": "x" * ARGUMENT_MAX_BYTES},
            "-c override 'plan_mode_reasoning_effort'",
        ),
        ({"enable_features": ["f" * ARGUMENT_MAX_BYTES]}, "value of --enable"),
    ],
    ids=["reasoning-effort", "plan-mode-effort", "enable-feature"],
)
async def test_every_argument_is_measured_before_the_cli_starts(
    tmp_path, monkeypatch, update, named
):
    """Arguments the request does not measure when it is built are measured where the
    process would start."""
    _refuse_a_spawn(monkeypatch)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, **update)

    with pytest.raises(ValueError, match=f"{named}.*command-line argument"):
        async for _ in codex_mod.stream_codex_cli(request):
            pass


def test_an_argument_is_measured_in_the_bytes_the_process_receives():
    """A path byte that is not UTF-8 is measured as the one byte exec passes; a lone
    surrogate exec cannot pass is refused before anything starts."""
    import os

    codex_mod._refuse_oversized_command(["codex", "-C", os.fsdecode(b"/tmp/not-utf8-\xff")])
    under = os.fsdecode(b"/" + b"\xff" * (ARGUMENT_MAX_BYTES - 2))
    codex_mod._refuse_oversized_command(["codex", "-C", under])
    with pytest.raises(ValueError, match=f"argument of {ARGUMENT_MAX_BYTES:,} bytes"):
        codex_mod._refuse_oversized_command(["codex", "-C", under + os.fsdecode(b"\xff")])

    with pytest.raises(ValueError):
        codex_mod._refuse_oversized_command(["codex", "-C", "/tmp/lone-\ud800"])


@pytest.mark.parametrize(
    "fields",
    [
        {"system_prompt": "hi \udcff"},
        {"config_overrides": {"k\udcff": 1}},
    ],
    ids=["system-prompt", "override-key"],
)
def test_a_lone_surrogate_in_a_prompt_or_an_override_is_refused_while_the_request_validates(
    tmp_path, fields
):
    """Text is not a path: a surrogate escape there would reach the CLI as a raw byte, so
    text the CLI cannot decode is refused by name while the request validates."""
    with pytest.raises(CLIInputRefusedError):
        CodexCodeRequest(prompt="hi", repo=tmp_path, **fields)


# ── a refusal at construction carries none of the request's input ─────────────

SECRET_OVERRIDE = "sentinel-in-another-override"
SECRET_PROMPT = "sentinel-in-the-turn-prompt"


def _fields_with_two_secrets(fields: dict) -> dict:
    """The parametrized fields, plus a sentinel in a second override and one in the turn
    prompt. The prompt stays past the CLI's limit when it was, and under it when it was."""
    overrides = {**(fields.get("config_overrides") or {}), "other": SECRET_OVERRIDE}
    prompt = fields.get("prompt", "hi")
    if len(prompt) > CODEX_MAX_TURN_INPUT_CHARS:
        prompt = prompt[: CODEX_MAX_TURN_INPUT_CHARS + 1 - len(SECRET_PROMPT) - 1]
    return {**fields, "prompt": f"{prompt} {SECRET_PROMPT}", "config_overrides": overrides}


def _everything_the_error_shows(error: BaseException) -> str:
    return " ".join([str(error), repr(error), *(repr(part) for part in error.args)])


@pytest.mark.parametrize(
    "fields",
    [
        {"prompt": "x" * (CODEX_MAX_TURN_INPUT_CHARS + 1)},
        {"prompt": "hi", "config_overrides": {"k": "v" * ARGUMENT_MAX_BYTES}},
        {"prompt": "hi \udcff"},
    ],
    ids=["over-limit-prompt", "oversized-override", "unencodable-prompt"],
)
@pytest.mark.parametrize("contained", [False, True], ids=["uncontained", "contained"])
def test_a_refusal_at_construction_carries_no_other_part_of_the_input(tmp_path, fields, contained):
    """Pydantic wraps a ValueError from a model validator in a ValidationError whose
    `.json()` and `.errors()` carry the whole input, so a secret in another override would
    reach them. The refusal is not a ValueError, so it passes through unwrapped."""
    fields = {**fields, "contain_account_integrations": contained}
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(repo=tmp_path, **_fields_with_two_secrets(fields))

    assert not isinstance(caught.value, pydantic.ValidationError)
    shown = _everything_the_error_shows(caught.value)
    assert SECRET_OVERRIDE not in shown
    assert SECRET_PROMPT not in shown


@pytest.mark.skipif(
    not hasattr(sys, "set_int_max_str_digits"),
    reason="only Pythons with an integer digit limit refuse to encode this value",
)
@pytest.mark.parametrize(
    "overrides",
    [
        {"k": 10**5000},
        {"features.apps": 10**5000},
        {"features": {"apps": 10**5000}},
        {"a b": 10**5000},
    ],
    ids=["value", "contained-key", "contained-table-entry", "unreadable-key"],
)
def test_an_unencodable_value_refuses_without_carrying_the_input(tmp_path, overrides):
    """A value the encoder refuses with ValueError refuses the request even under
    containment, and the refusal carries none of the other overrides."""
    fields = _fields_with_two_secrets(
        {
            "prompt": "hi",
            "contain_account_integrations": True,
            "config_overrides": overrides,
        }
    )
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(repo=tmp_path, **fields)

    assert not isinstance(caught.value, pydantic.ValidationError)
    assert SECRET_OVERRIDE not in _everything_the_error_shows(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["features.apps", "features.plugins"])
async def test_an_override_containment_drops_is_not_measured(tmp_path, monkeypatch, key):
    """With account integrations contained, an apps or plugins override never reaches the
    command line, so its size refuses nothing."""
    started = []

    async def spawned(cmd, **_kwargs):
        started.append(cmd)
        return
        yield

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "ndjson_from_cli", spawned)
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        contain_account_integrations=True,
        config_overrides={key: "v" * ARGUMENT_MAX_BYTES},
    )

    async for _ in codex_mod._ndjson_from_cli(request):
        pass

    (cmd,) = started
    assert not any(argument.startswith(key) for argument in cmd)


def test_a_features_table_is_measured_as_containment_leaves_it(tmp_path):
    """An oversized apps entry containment removes from an inline table refuses nothing; the
    table that reaches the command line is what is measured."""
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        contain_account_integrations=True,
        config_overrides={"features": {"apps": "v" * ARGUMENT_MAX_BYTES, "web_search": True}},
    )
    assert "features={ web_search = true }" in request.as_cmd_args()


# ── text the TOML encoder changes or cannot take ─────────────────────────────


@pytest.mark.skipif(tomllib is None, reason="tomllib landed in Python 3.11")
@pytest.mark.parametrize(
    "value",
    ["a\x01b", "\x01", "\x1b[0m", 'say "hi" \\ there', "tab\there", "名前", "🙂"],
    ids=["interior-control", "leading-control", "ansi-escape", "quotes", "tab", "cjk", "emoji"],
)
def test_a_string_override_reads_back_as_the_same_string(value):
    """The `toml` package renders a control character wrongly (`"a\\x01b"` came back as
    `"ax01b"`) and raises IndexError on a leading one, so a string override is rendered
    here. TOML reads every one of these back unchanged."""
    assert tomllib.loads("k = " + toml_override_value(value))["k"] == value


@pytest.mark.parametrize(
    "overrides",
    [{"k": "hi \udcff"}, {"outer": {"inner\udcff": 1}}],
    ids=["value", "nested-table-key"],
)
def test_a_lone_surrogate_a_rendered_string_cannot_hold_is_refused(tmp_path, overrides):
    with pytest.raises(CLIInputRefusedError, match="lone surrogate"):
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides=overrides)


def test_an_override_containment_drops_may_hold_a_control_character(tmp_path):
    """The dropped override is not rendered, so a character the encoder cannot take does
    not reach it."""
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        contain_account_integrations=True,
        config_overrides={"features.apps": "\x01"},
    )

    assert not any(argument.startswith("features.apps") for argument in request.as_cmd_args())


# ── an encoding refusal names the field ──────────────────────────────────────


@pytest.mark.parametrize(
    "fields,named",
    [
        ({"prompt": "hi", "system_prompt": "hi \udcff"}, "system prompt"),
        ({"prompt": "hi", "config_overrides": {"k\udcff": 1}}, "config override key"),
        ({"prompt": "hi", "config_overrides": {"k": "hi \udcff"}}, "config override"),
    ],
    ids=["system-prompt", "override-key", "override-value"],
)
def test_an_encoding_refusal_names_the_field(tmp_path, fields, named):
    """A bare UnicodeEncodeError names neither the input nor the request."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(repo=tmp_path, **fields)

    assert named in str(caught.value)


def test_an_override_key_refusal_cuts_the_key_like_the_argument_refusal(tmp_path):
    key = "k" * 200 + "\udcff"

    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides={key: 1})

    assert "k" * 200 not in str(caught.value)


# ── the turn prompt ──────────────────────────────────────────────────────────


def test_a_lone_surrogate_in_the_turn_prompt_is_refused_while_the_request_validates(tmp_path):
    """The turn prompt travels over the child's stdin; a prompt the child cannot decode is
    refused before the process starts, not when it is written to the pipe."""
    with pytest.raises(CLIInputRefusedError, match="turn prompt"):
        CodexCodeRequest(prompt="hi \udcff", repo=tmp_path)


@pytest.mark.asyncio
async def test_a_copied_turn_prompt_the_cli_cannot_decode_is_refused_before_the_cli_starts(
    tmp_path, monkeypatch
):
    _refuse_a_spawn(monkeypatch)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"prompt": "hi \udcff"}
    )

    with pytest.raises(ValueError, match="turn prompt"):
        async for _ in codex_mod.stream_codex_cli(request):
            pass


# ── a value the encoder refuses with TypeError ───────────────────────────────


def test_a_value_the_encoder_refuses_with_a_type_error_reaches_the_command_builder(tmp_path):
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides={"k": None})

    with pytest.raises(TypeError):
        request.as_cmd_args()

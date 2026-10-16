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
from collections.abc import Mapping

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
# developer_instructions=<system prompt> is one argument; the prefix counts toward it, and
# the prompt is written as a TOML string, so its two quotes do too.
PREFIX = len("developer_instructions=")
QUOTES = 2


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
    """The bound is on developer_instructions=<system prompt as a TOML string>, in its UTF-8
    bytes, which is what the operating system measures for text that holds no surrogate; the
    turn input is bounded in characters."""
    just_under = "s" * (ARGUMENT_MAX_BYTES - PREFIX - QUOTES - 1)
    request = CodexCodeRequest(prompt="hi", system_prompt=just_under, repo=tmp_path)
    assert request.system_prompt == just_under
    argument = next(a for a in request.as_cmd_args() if a.startswith("developer_instructions="))
    assert len(argument.encode("utf-8")) == ARGUMENT_MAX_BYTES - 1

    with pytest.raises(CLIInputRefusedError, match=f"argument of {ARGUMENT_MAX_BYTES:,} bytes"):
        CodexCodeRequest(prompt="hi", system_prompt=just_under + "s", repo=tmp_path)

    wide = "漢" * ((ARGUMENT_MAX_BYTES - PREFIX) // 3 + 1)
    assert len(wide) < ARGUMENT_MAX_BYTES < len(wide.encode("utf-8")) + PREFIX
    size = len(wide.encode("utf-8")) + PREFIX + QUOTES
    with pytest.raises(CLIInputRefusedError, match=f"argument of {size:,} bytes,"):
        CodexCodeRequest(prompt="hi", system_prompt=wide, repo=tmp_path)


@pytest.mark.skipif(tomllib is None, reason="tomllib is Python 3.11+")
@pytest.mark.parametrize(
    "system_prompt",
    [
        "true",
        "42",
        '"quoted"',
        "[1, 2]",
        "a\nk = v",
        "tab\there",
        'back\\slash "q"',
        "emoji \U0001f600",
    ],
    ids=["bool", "integer", "quoted", "array", "table-line", "tab", "escapes", "non-bmp"],
)
def test_the_system_prompt_reaches_codex_as_its_own_text(tmp_path, system_prompt):
    """codex reads a `-c` value as TOML, so a prompt written raw that parses as TOML
    would arrive as a bool, a number, an unquoted string or an array. Written as a TOML
    string, it reads back as exactly the text given."""
    request = CodexCodeRequest(prompt="hi", system_prompt=system_prompt, repo=tmp_path)
    argument = next(a for a in request.as_cmd_args() if a.startswith("developer_instructions="))
    assert tomllib.loads(argument) == {"developer_instructions": system_prompt}


def test_a_config_override_too_long_for_one_argument_is_refused(tmp_path):
    """A `-c key=value` override is one argument too, developer_instructions included."""
    key = "developer_instructions"
    # toml_override_value quotes a string: two quote characters.
    fits = "s" * (ARGUMENT_MAX_BYTES - len(key) - 1 - 2 - 1)
    request = CodexCodeRequest(prompt="hi", config_overrides={key: fits}, repo=tmp_path)
    argument = next(a for a in request.as_cmd_args() if a.startswith(f"{key}="))
    assert len(argument.encode("utf-8")) == ARGUMENT_MAX_BYTES - 1

    with pytest.raises(CLIInputRefusedError, match="config override entry 0 reaches"):
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

    with pytest.raises(ValueError, match="config override entry 0 reaches"):
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


@pytest.mark.parametrize(
    "update,named",
    [
        ({"reasoning_effort": "x" * ARGUMENT_MAX_BYTES}, "reasoning_effort"),
        (
            {"plan_mode_reasoning_effort": "x" * ARGUMENT_MAX_BYTES},
            "plan_mode_reasoning_effort",
        ),
        ({"enable_features": ["f" * ARGUMENT_MAX_BYTES]}, "enable_features"),
    ],
    ids=["reasoning-effort", "plan-mode-effort", "enable-feature"],
)
def test_every_argument_is_measured_naming_its_field(tmp_path, update, named):
    """Each field rendered into its own argument is measured at construction, and the
    refusal names the field."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, **update)

    message = str(caught.value)
    assert named in message
    assert "command-line argument" in message


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
    a system prompt or an override key that reaches the command line is refused by name
    while the request validates. An override containment drops is never measured, so a
    request held with `contain_account_integrations=True` over an unreadable key
    constructs and `as_cmd_args` refuses the key instead."""
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
        (
            {"prompt": "hi", "config_overrides": {"k\udcff": 1}},
            "key of the config override entry 0",
        ),
        ({"prompt": "hi", "config_overrides": {"k": "hi \udcff"}}, "config override"),
    ],
    ids=["system-prompt", "override-key", "override-value"],
)
def test_an_encoding_refusal_names_the_field(tmp_path, fields, named):
    """A bare UnicodeEncodeError names neither the input nor the request."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(repo=tmp_path, **fields)

    assert named in str(caught.value)


def test_a_long_override_key_is_not_printed_in_its_refusal(tmp_path):
    key = "k" * 200 + "\udcff"

    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides={key: 1})

    assert "k" * 8 not in str(caught.value)


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


# ── a validation error carries no config override ────────────────────────────

SECRET_IN_OVERRIDE = "sentinel-override-value"
SECRET_OVERRIDE_KEY = "sentinel-override-key"


def _overrides_with_two_secrets() -> dict:
    """One override entry holding a sentinel in its value and a second holding one
    in its key, as an override that carries a token."""
    return {SECRET_OVERRIDE_KEY: "plain", "k": SECRET_IN_OVERRIDE}


def _hidden_in_a_validation_error(error: BaseException) -> None:
    """A validation error must not show either sentinel in any of its renderings."""
    assert isinstance(error, pydantic.ValidationError)
    shown = " ".join([str(error), repr(error), error.json(), str(error.errors())])
    assert SECRET_IN_OVERRIDE not in shown
    assert SECRET_OVERRIDE_KEY not in shown


@pytest.mark.parametrize(
    "key",
    [(SECRET_OVERRIDE_KEY, 1), SECRET_OVERRIDE_KEY.encode(), 7],
    ids=["tuple", "bytes", "int"],
)
def test_a_key_that_is_not_a_string_is_refused_by_position_and_type(tmp_path, key):
    """pydantic quotes a key it cannot validate as a string, so such a key is refused
    before pydantic reads it, by its position and type only."""
    overrides = {"k": SECRET_IN_OVERRIDE, key: SECRET_IN_OVERRIDE}

    with pytest.raises(TypeError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides=overrides)

    message = str(caught.value)
    assert f"entry 1 (key is {type(key).__name__})" in message
    shown = " ".join([message, repr(caught.value), _everything_reachable_from(caught.value)])
    assert SECRET_OVERRIDE_KEY not in shown
    assert SECRET_IN_OVERRIDE not in shown


@pytest.mark.asyncio
async def test_a_copied_key_that_is_not_a_string_is_refused_before_the_cli_starts(
    tmp_path, monkeypatch
):
    _refuse_a_spawn(monkeypatch)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"config_overrides": {(SECRET_OVERRIDE_KEY, 1): "v \udcff"}}
    )

    with pytest.raises(ValueError, match="entry 0 has a key that is not a string") as caught:
        async for _ in codex_mod.stream_codex_cli(request):
            pass

    assert SECRET_OVERRIDE_KEY not in str(caught.value)


def test_a_missing_prompt_carries_no_config_override(tmp_path):
    """`validate_message_prompt` raises ValueError for neither a prompt nor messages,
    and pydantic wraps it in a ValidationError whose rendering carries the whole
    input — every override beside it included."""
    with pytest.raises(pydantic.ValidationError) as caught:
        CodexCodeRequest(repo=tmp_path, config_overrides=_overrides_with_two_secrets())

    _hidden_in_a_validation_error(caught.value)


def test_an_ambiguous_approval_policy_carries_no_config_override(tmp_path):
    overrides = {**_overrides_with_two_secrets(), "approval_policy": "on-request"}

    with pytest.raises(pydantic.ValidationError) as caught:
        CodexCodeRequest(
            prompt="hi", repo=tmp_path, ask_for_approval="never", config_overrides=overrides
        )

    _hidden_in_a_validation_error(caught.value)


def test_a_path_outside_the_repo_carries_no_config_override(tmp_path):
    """A relative path that resolves outside the repo is refused by the model-level
    validator, whose error carries the whole input."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.json").write_text("{}")
    (repo / "link").symlink_to(outside)

    with pytest.raises(pydantic.ValidationError) as caught:
        CodexCodeRequest(
            prompt="hi",
            repo=repo,
            output_schema="link/escape.json",
            config_overrides=_overrides_with_two_secrets(),
        )

    _hidden_in_a_validation_error(caught.value)


def test_a_profile_error_carries_no_config_override(tmp_path, monkeypatch):
    """A config profile that cannot be read raises ValueError from the profile
    resolver, which pydantic wraps in the same validation error."""
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "a-profile.config.toml").write_text("model = \n")
    monkeypatch.setenv("CODEX_HOME", str(home))

    with pytest.raises(pydantic.ValidationError) as caught:
        CodexCodeRequest(
            prompt="hi",
            model="a-profile",
            repo=tmp_path,
            config_overrides=_overrides_with_two_secrets(),
        )

    _hidden_in_a_validation_error(caught.value)


def test_a_config_override_that_is_not_a_mapping_is_refused_without_printing_it(tmp_path):
    with pytest.raises(TypeError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides=SECRET_IN_OVERRIDE)

    assert SECRET_IN_OVERRIDE not in str(caught.value)
    assert "must be a mapping" in str(caught.value)


def _write_profile(home, name: str, body: str) -> str:
    home.mkdir(exist_ok=True)
    (home / f"{name}.config.toml").write_text(body)
    return str(home)


def test_the_wrapper_round_trips_a_valid_request(tmp_path):
    """The field's type, value and serialization are what they were before the
    wrapper, and the argv is unchanged."""
    overrides = _overrides_with_two_secrets()

    request = CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides=overrides)

    assert isinstance(request.config_overrides, dict)
    assert request.config_overrides == overrides
    assert request.model_dump()["config_overrides"] == overrides
    assert (
        request.as_cmd_args()
        == CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides=overrides).as_cmd_args()
    )


def test_a_profile_merges_into_the_config_overrides_as_before(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    _write_profile(home, "a-profile", 'model = "gpt-5.4"\nverbosity = "low"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))

    request = CodexCodeRequest(
        prompt="hi", model="a-profile", repo=tmp_path, config_overrides={"caller": "yes"}
    )

    assert request.config_overrides == {"verbosity": "low", "caller": "yes"}


# ── a refusal's exception chain holds no input ───────────────────────────────

CHAIN_SENTINEL = "sentinel-in-the-chained-input"


def _everything_reachable_from(error: BaseException) -> str:
    """Everything every exception CHAINED to the error would print.

    The refusal itself names the field it refused, so the value it was given is part
    of its own message; what must not be reachable is the input beside it, which is
    what an earlier exception in the chain would hold.
    """
    parts: list[str] = []
    pending = [error.__context__, error.__cause__]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        parts.extend([str(current), repr(current), repr(current.args)])
        for attribute in ("object", "input"):
            value = getattr(current, attribute, None)
            if value is not None:
                parts.append(repr(value))
        pending.extend([current.__context__, current.__cause__])
    return " ".join(parts)


@pytest.mark.parametrize(
    "fields,named",
    [
        ({"prompt": f"hi \udcff {CHAIN_SENTINEL}"}, "turn prompt"),
        ({"prompt": "hi", "system_prompt": f"hi \udcff {CHAIN_SENTINEL}"}, "system prompt"),
        (
            {"prompt": "hi", "config_overrides": {f"k \udcff {CHAIN_SENTINEL}": 1}},
            "key of the config override entry 0",
        ),
        ({"prompt": "hi", "config_overrides": {"k": f"hi \udcff {CHAIN_SENTINEL}"}}, "override"),
    ],
    ids=["turn-prompt", "system-prompt", "override-key", "override-value"],
)
def test_a_refusal_chain_holds_no_input(tmp_path, fields, named):
    """`raise ... from None` inside an `except` still sets `__context__`, and the
    UnicodeEncodeError there holds the whole input in `.object`. The refusal is
    raised after the handler ends, so nothing is reachable through it."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(repo=tmp_path, **fields)

    error = caught.value
    assert named in str(error)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert CHAIN_SENTINEL not in _everything_reachable_from(error)


@pytest.mark.asyncio
async def test_the_spawn_check_chain_holds_no_input_either(tmp_path, monkeypatch):
    """The same check runs again where the process would start, on a request
    `model_copy` built without validation. That check is not a model validator, so its
    refusal is a plain ValueError, with the input unreachable through it all the same."""
    _refuse_a_spawn(monkeypatch)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"prompt": f"hi \udcff {CHAIN_SENTINEL}"}
    )

    with pytest.raises(ValueError) as caught:
        async for _ in codex_mod.stream_codex_cli(request):
            pass

    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert CHAIN_SENTINEL not in _everything_reachable_from(caught.value)


# ── every argument text checked at construction ──────────────────────────────


@pytest.mark.parametrize(
    "fields,named",
    [
        ({"reasoning_effort": "high\udcff"}, "reasoning_effort"),
        ({"plan_mode_reasoning_effort": "high\udcff"}, "plan_mode_reasoning_effort"),
        ({"enable_features": ["x\udcff"]}, "enable_features"),
        ({"disable_features": ["x\udcff"]}, "disable_features"),
        ({"model": "gpt-5.4\udcff"}, "model"),
        ({"profile": "a\udcff"}, "profile"),
    ],
    ids=[
        "reasoning-effort",
        "plan-mode-effort",
        "enable-features",
        "disable-features",
        "model",
        "profile",
    ],
)
def test_a_lone_surrogate_in_any_argument_text_is_refused_at_construction(tmp_path, fields, named):
    """A field `as_cmd_args` renders into its own argument reaches the spawn as a raw
    byte when it holds a lone surrogate, so it is refused at construction, by name."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, **fields)

    assert named in str(caught.value)


def test_a_surrogate_in_a_second_feature_entry_is_refused_too(tmp_path):
    """A repeated field renders each entry as its own argument, so every entry is
    measured, not only the one a mapping would keep."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, enable_features=["ok", "x\udcff"])

    assert "enable_features" in str(caught.value)


@pytest.mark.parametrize(
    "fields,named",
    [
        ({"model": "gpt-5.4\x00"}, "model"),
        ({"profile": "a\x00"}, "profile"),
        ({"enable_features": ["ok", "x\x00"]}, "enable_features"),
        ({"disable_features": ["x\x00"]}, "disable_features"),
        ({"config_overrides": {"a\x00b": 1}}, "The key of the config override entry 0"),
        ({"reasoning_effort": "high\x00"}, "reasoning_effort"),
        ({"plan_mode_reasoning_effort": "high\x00"}, "plan_mode_reasoning_effort"),
    ],
    ids=[
        "model",
        "profile",
        "enable-features",
        "disable-features",
        "override-key",
        "reasoning-effort",
        "plan-mode-effort",
    ],
)
def test_a_nul_in_any_argument_text_is_refused_at_construction(tmp_path, fields, named):
    """exec refuses an argument holding a NUL with a message that names nothing, so
    the request refuses it first, by name."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, **fields)

    assert named in str(caught.value)
    assert "NUL" in str(caught.value)


def test_a_nul_in_an_override_value_is_escaped_not_refused(tmp_path):
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides={"k": "a\x00b"})

    assert 'k="a\\u0000b"' in request.as_cmd_args()


@pytest.mark.parametrize(
    "key",
    ["a=b", 'model="gpt-5"#', '"a=b"'],
    ids=["bare", "sets-another-key", "quoted"],
)
@pytest.mark.parametrize("contained", [False, True], ids=["uncontained", "contained"])
def test_an_override_key_holding_an_equals_sign_is_refused(tmp_path, key, contained):
    """The CLI ends a `-c` key at its first `=`, so such a key would set the key
    before it: `model="gpt-5"#=1` reads as setting `model`."""
    with pytest.raises(CLIInputRefusedError, match="entry 0 holds '='") as caught:
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            contain_account_integrations=contained,
            config_overrides={key: 1},
        )
    assert key not in str(caught.value)

    copied = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=contained
    ).model_copy(update={"config_overrides": {key: 1}})
    with pytest.raises(ValueError, match="holds '='"):
        copied.as_cmd_args()


_SECRET_IN_A_KEY = "sk-SECRET-KEY-456"


@pytest.mark.parametrize(
    "key,contained",
    [
        (f"env.OPENAI_KEY={_SECRET_IN_A_KEY}", False),
        (f"env.OPENAI_KEY={_SECRET_IN_A_KEY}", True),
        (f"not a key {_SECRET_IN_A_KEY}", True),
        (f"env.{_SECRET_IN_A_KEY}\x00", False),
    ],
    ids=["equals-uncontained", "equals-contained", "unreadable-contained", "nul"],
)
def test_a_refused_override_key_is_never_printed(tmp_path, key, contained):
    """Each refusal names the entry by position, in the message, in to_dict() and in
    the command builder's refusal for a copy."""
    with pytest.raises(CLIInputRefusedError) as constructed:
        CodexCodeRequest(
            prompt="hi",
            repo=tmp_path,
            contain_account_integrations=contained,
            config_overrides={"model_verbosity": "low", key: 1},
        )
    copied = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=contained
    ).model_copy(update={"config_overrides": {"model_verbosity": "low", key: 1}})
    # The command builder refuses a key it cannot split or read; a NUL is refused where
    # the process starts, by the argument's position.
    with pytest.raises(ValueError) as built:
        codex_mod._refuse_oversized_command(["codex", *copied.as_cmd_args()])

    assert "config override entry 1" in str(constructed.value)
    assert ("config override entry 1" if "\x00" not in key else "-c override at argument") in str(
        built.value
    )
    for caught in (constructed, built):
        assert _SECRET_IN_A_KEY not in str(caught.value)
        assert _SECRET_IN_A_KEY not in repr(caught.value)
    assert _SECRET_IN_A_KEY not in str(constructed.value.to_dict(include_cause=True))


_SECRET_IN_A_VALUE = "sk-SECRET-VALUE-789"


@pytest.mark.parametrize(
    "update,named",
    [
        (
            {"config_overrides": {"k": f"v{_SECRET_IN_A_VALUE}\udcff"}},
            "The value of the config override entry 0",
        ),
        (
            {"config_overrides": {"k": {"inner": f"{_SECRET_IN_A_VALUE}\ud800"}}},
            "The value of the config override entry 0",
        ),
        ({"system_prompt": f"{_SECRET_IN_A_VALUE}\udcff"}, "The system prompt"),
        ({"reasoning_effort": f"high{_SECRET_IN_A_VALUE}\udcff"}, "reasoning_effort"),
        (
            {"plan_mode_reasoning_effort": f"high{_SECRET_IN_A_VALUE}\udcff"},
            "plan_mode_reasoning_effort",
        ),
        ({"ask_for_approval": f"on-request{_SECRET_IN_A_VALUE}\ud800"}, "ask_for_approval"),
    ],
    ids=["override-value", "inline-table", "system-prompt", "effort", "plan-effort", "approval"],
)
def test_a_copy_the_command_builder_cannot_encode_is_refused_without_its_text(
    tmp_path, update, named
):
    """model_copy(update=...) skips validation, so the command builder is the first to
    encode a copy's text: it names the field, and neither the refusal nor its chain holds
    the text, which the codec's own error holds whole."""
    copied = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(update=update)
    with pytest.raises(ValueError) as built:
        copied.as_cmd_args()

    assert str(built.value) == (
        f"{named} holds a character strict UTF-8 cannot encode: a lone surrogate cannot "
        "be passed to the Codex CLI."
    )
    assert not isinstance(built.value, UnicodeEncodeError)
    assert built.value.__context__ is None
    assert built.value.__cause__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(built.value)


@pytest.mark.parametrize("part", ["key", "value"])
def test_an_environment_the_spawn_cannot_encode_is_refused_at_construction(tmp_path, part):
    """The spawn encodes the environment with os.fsencode, whose error holds the whole key
    or value; the request refuses it first, naming the entry by position."""
    entry = (
        {f"K{_SECRET_IN_A_VALUE}\ud800": "v"}
        if part == "key"
        else {"OPENAI_API_KEY": f"{_SECRET_IN_A_VALUE}\ud800"}
    )
    with pytest.raises(TypeError) as refused:
        CodexCodeRequest(prompt="hi", repo=tmp_path, env={"HOME": "/h", **entry})

    assert str(refused.value) == (
        f"The environment entry 1 {part} holds a character the process environment cannot "
        "encode: a lone surrogate cannot be passed to a CLI process."
    )
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


def test_an_environment_holding_a_surrogate_escape_is_accepted(tmp_path):
    """U+DC80 to U+DCFF stand for undecodable bytes, which the spawn passes as those bytes."""
    request = CodexCodeRequest(prompt="hi", repo=tmp_path, env={"PATH_BYTES": "a\udcffb"})

    assert request.env == {"PATH_BYTES": "a\udcffb"}


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["key", "value"])
async def test_a_copied_environment_the_spawn_cannot_encode_is_refused_before_it(
    tmp_path, monkeypatch, part
):
    """A copy skips validation, so the spawn helper every CLI provider shares checks the
    environment itself, before any process starts."""
    from lionagi.providers import _cli_subprocess

    def spawned(*_args, **_kwargs):
        raise AssertionError("a process was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(_cli_subprocess.asyncio, "create_subprocess_exec", spawned)
    entry = (
        {f"K{_SECRET_IN_A_VALUE}\ud800": "v"}
        if part == "key"
        else {"OPENAI_API_KEY": f"{_SECRET_IN_A_VALUE}\ud800"}
    )
    copied = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(update={"env": entry})
    with pytest.raises(ValueError) as refused:
        async for _ in codex_mod._ndjson_from_cli(copied):
            pass

    assert str(refused.value) == (
        f"The environment entry 0 {part} holds a character the process environment cannot "
        "encode: a lone surrogate cannot be passed to a CLI process."
    )
    assert not isinstance(refused.value, UnicodeEncodeError)
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


def test_an_argument_exec_cannot_encode_is_refused_without_its_text():
    with pytest.raises(ValueError) as refused:
        codex_mod._refuse_oversized_command(["codex", "-c", f"k={_SECRET_IN_A_VALUE}\ud800"])

    assert "-c override at argument 2" in str(refused.value)
    assert not isinstance(refused.value, UnicodeEncodeError)
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


@pytest.mark.asyncio
async def test_an_unreadable_key_refusal_keeps_no_parser_error(tmp_path, monkeypatch):
    """The TOML parser's error can quote the key it could not read, so the refusal keeps
    it neither as its cause nor as its context, on the command builder's path and on the
    path where the process would start."""
    _refuse_a_spawn(monkeypatch)
    key = f'"{_SECRET_IN_A_KEY}\x01"'
    copied = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=True
    ).model_copy(update={"config_overrides": {key: 1}})
    with pytest.raises(ValueError) as built:
        copied.as_cmd_args()
    with pytest.raises(ValueError) as streamed:
        async for _ in codex_mod._ndjson_from_cli(copied):
            pass

    for caught in (built, streamed):
        assert "config override entry 0" in str(caught.value)
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None


class _IterationHidesAKey(dict):
    """A dict whose iteration names one key while its storage holds another."""

    def __iter__(self):
        return iter(["k"])


class _ItemsHideAKey(dict):
    """A dict whose items() names one pair while its storage holds another."""

    def items(self):
        return [("k", "v")]


@pytest.mark.parametrize("shape", [_IterationHidesAKey, _ItemsHideAKey])
@pytest.mark.parametrize("field", ["config_overrides", "env"])
def test_a_mapping_whose_views_disagree_is_checked_on_its_copy(tmp_path, field, shape):
    """The keys are checked on the plain copy the validator returns, so a key one view
    of the mapping hides is still found, by position and type, and not printed. The
    config_overrides check reads the keys and the env check reads items(), so each is
    fooled by a different shape without the copy."""
    hidden = shape({(SECRET_OVERRIDE_KEY, 1): "v"})

    with pytest.raises(TypeError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, **{field: hidden})

    assert "entry 0 (key is tuple)" in str(caught.value)
    shown = " ".join(
        [str(caught.value), repr(caught.value), _everything_reachable_from(caught.value)]
    )
    assert SECRET_OVERRIDE_KEY not in shown


@pytest.mark.parametrize("shape", [_IterationHidesAKey, _ItemsHideAKey])
def test_the_claude_code_env_is_checked_on_its_copy_too(shape):
    from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

    hidden = shape({(SECRET_OVERRIDE_KEY, 1): "v"})

    with pytest.raises(TypeError) as caught:
        ClaudeCodeRequest(prompt="hi", env=hidden)

    assert "entry 0 (key is tuple)" in str(caught.value)
    assert SECRET_OVERRIDE_KEY not in str(caught.value)


def test_a_copied_key_that_is_not_a_string_is_refused_by_the_command_builder(tmp_path):
    request = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=True
    ).model_copy(update={"config_overrides": {(SECRET_OVERRIDE_KEY, 1): "v"}})

    with pytest.raises(TypeError, match="entry 0 has a key that is not a string") as caught:
        request.as_cmd_args()

    assert SECRET_OVERRIDE_KEY not in str(caught.value)


def test_a_key_containment_cannot_read_is_named_by_position(tmp_path):
    """Refused at construction, and by the command builder for a copy, naming the
    entry and printing none of the key."""
    key = "not a key " + "x" * 120
    with pytest.raises(CLIInputRefusedError, match="is not a TOML key") as constructed:
        CodexCodeRequest(
            prompt="hi", repo=tmp_path, contain_account_integrations=True, config_overrides={key: 1}
        )
    request = CodexCodeRequest(
        prompt="hi", repo=tmp_path, contain_account_integrations=True
    ).model_copy(update={"config_overrides": {key: 1}})
    with pytest.raises(ValueError, match="is not a TOML key") as built:
        request.as_cmd_args()

    for caught in (constructed, built):
        assert "The key of the config override entry 0 is not a TOML key." == str(caught.value)


class _KeysHideAKey(Mapping):
    """A mapping, not a dict, whose keys() omits a key its items() yields."""

    def __init__(self, hidden):
        self._hidden = hidden

    def __getitem__(self, key):
        if key == "k":
            return "v"
        raise KeyError(key)

    def __iter__(self):
        return iter(["k"])

    def __len__(self):
        return 1

    def items(self):
        return [("k", "v"), (self._hidden, "v")]


def test_overrides_are_validated_as_the_plain_copy_of_a_mapping(tmp_path):
    """The copy reads the mapping's keys, so a key only items() yields is not
    copied, and pydantic never reads items() of the caller's mapping, where it would
    quote the key in its validation error."""
    request = CodexCodeRequest(
        prompt="hi",
        repo=tmp_path,
        config_overrides=_KeysHideAKey((SECRET_OVERRIDE_KEY, 1)),
    )

    assert dict(request.config_overrides) == {"k": "v"}
    assert SECRET_OVERRIDE_KEY not in repr(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "update,named",
    [
        ({"model": "gpt-5.4\x00"}, "model"),
        ({"images": ["a\x00.png"]}, "-i"),
        ({"ask_for_approval": "never\x00"}, "ask_for_approval"),
    ],
    ids=["model", "image-path", "approval-policy"],
)
async def test_a_copied_nul_is_refused_before_the_cli_starts(tmp_path, monkeypatch, update, named):
    """A path field is not a text field the construction check reads, so the spawn
    checks every argument it builds for a NUL as well."""
    _refuse_a_spawn(monkeypatch)
    request = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(update=update)

    with pytest.raises(ValueError, match="NUL") as caught:
        async for _ in codex_mod.stream_codex_cli(request):
            pass

    assert named in str(caught.value)


# ── bare TOML keys ───────────────────────────────────────────────────────────


@pytest.mark.skipif(tomllib is None, reason="tomllib landed in Python 3.11")
def test_a_nested_key_ending_in_a_newline_is_quoted_and_reads_back():
    """`match` with `$` accepts `"abc\\n"` as a bare key, which TOML refuses; the
    rendered override must quote it and read back with the key intact."""
    rendered = toml_override_value({"outer": {"abc\n": 1}})

    assert tomllib.loads("k = " + rendered)["k"] == {"outer": {"abc\n": 1}}


def test_a_key_ending_in_a_newline_renders_quoted():
    """A newline is a control character, so it is escaped rather than kept raw."""
    assert codex_mod._toml_key("abc\n") == '"abc\\u000A"'


# ── two encoder properties ───────────────────────────────────────────────────


@pytest.mark.skipif(tomllib is None, reason="tomllib landed in Python 3.11")
def test_u007f_is_rendered_as_an_escape_and_reads_back():
    """U+007F is a control character TOML may not hold raw, and the `toml` package
    renders it wrongly, so it is escaped here."""
    rendered = toml_override_value("a\x7fb")

    assert "\\u007F" in rendered
    assert tomllib.loads("k = " + rendered)["k"] == "a\x7fb"


def test_a_surrogate_in_an_override_value_is_refused_naming_the_value(tmp_path):
    """The strict encode belongs to the value's own rendering: without it the
    surrogate would be refused here as an unreadable key instead, so removing the
    encode from `_toml_string` turns this red."""
    with pytest.raises(CLIInputRefusedError) as caught:
        CodexCodeRequest(prompt="hi", repo=tmp_path, config_overrides={"k": "v \udcff"})

    message = str(caught.value)
    assert message.startswith("The value of the config override entry 0")
    assert "The key of the config override" not in message


class _SecretPath:
    """A path-like value, which the spawn encodes through ``__fspath__``."""

    def __fspath__(self):
        return f"{_SECRET_IN_A_VALUE}\ud800"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        pytest.param({"HOME": "/h", "OPENAI_API_KEY": _SecretPath()}, id="path-like-value"),
        pytest.param(
            _ItemsHideAKey({"HOME": "/h", "OPENAI_API_KEY": f"{_SECRET_IN_A_VALUE}\ud800"}),
            id="items-hide-it",
        ),
    ],
)
async def test_a_copied_environment_is_checked_as_the_spawn_copies_it(tmp_path, monkeypatch, env):
    """The spawn helper copies the mapping with dict() and encodes every key and value of
    that copy, as the spawn would: an entry items() leaves out, or a value read through
    ``__fspath__``, is refused by position before any process starts."""
    from lionagi.providers import _cli_subprocess

    def spawned(*_args, **_kwargs):
        raise AssertionError("a process was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(_cli_subprocess.asyncio, "create_subprocess_exec", spawned)
    copied = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(update={"env": env})
    with pytest.raises(ValueError) as refused:
        async for _ in codex_mod._ndjson_from_cli(copied):
            pass

    assert str(refused.value) == (
        "The environment entry 1 value holds a character the process environment cannot "
        "encode: a lone surrogate cannot be passed to a CLI process."
    )
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


class _ChangesAfterItsFirstRead:
    """A path-like value whose first read is plain text and whose every later read holds a
    character the process environment cannot encode."""

    def __init__(self):
        self.reads = 0

    def __fspath__(self):
        self.reads += 1
        return "plain" if self.reads == 1 else f"{_SECRET_IN_A_VALUE}\ud800"


class _CannotBeRead:
    """A path-like value whose read fails with the value's text in the error."""

    def __init__(self, error: type[Exception]):
        self.error = error

    def __fspath__(self):
        raise self.error(f"cannot read {_SECRET_IN_A_VALUE}")


class _StoppedAtTheSpawn(Exception):
    pass


@pytest.mark.asyncio
async def test_the_process_receives_the_environment_the_check_read(tmp_path, monkeypatch):
    """Each key and value is read once, and the copy that was checked is the copy the
    process is given, so a value that would answer a later read differently is never
    read again."""
    from lionagi.providers import _cli_subprocess

    given = {}

    def spawned(*_args, **kwargs):
        given.update(kwargs["env"])
        raise _StoppedAtTheSpawn

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(_cli_subprocess.asyncio, "create_subprocess_exec", spawned)
    value = _ChangesAfterItsFirstRead()
    copied = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"env": {"HOME": "/h", "OPENAI_API_KEY": value}}
    )
    with pytest.raises(_StoppedAtTheSpawn):
        async for _ in codex_mod._ndjson_from_cli(copied):
            pass

    assert given["OPENAI_API_KEY"] == "plain"
    assert value.reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "error", "type_name"),
    [
        pytest.param(_CannotBeRead(ValueError), ValueError, "_CannotBeRead", id="ValueError"),
        pytest.param(_CannotBeRead(TypeError), TypeError, "_CannotBeRead", id="TypeError"),
        pytest.param(789, TypeError, "int", id="not-text"),
    ],
)
async def test_a_copied_environment_value_that_cannot_be_read_is_refused_without_its_text(
    tmp_path, monkeypatch, value, error, type_name
):
    """Refused by position, with ``TypeError`` when the read raised it, as the spawn's own
    encoding would have."""
    from lionagi.providers import _cli_subprocess

    def spawned(*_args, **_kwargs):
        raise AssertionError("a process was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(_cli_subprocess.asyncio, "create_subprocess_exec", spawned)
    copied = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(
        update={"env": {"HOME": "/h", "OPENAI_API_KEY": value}}
    )
    with pytest.raises(error) as refused:
        async for _ in codex_mod._ndjson_from_cli(copied):
            pass

    assert type(refused.value) is error
    assert str(refused.value) == (
        f"The environment entry 1 value, of type {type_name}, could not be read as text, "
        "so it cannot be passed to a CLI process."
    )
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "first", "second"),
    [
        pytest.param(
            {
                "HOME": "/h",
                "\u00e9": f"{_SECRET_IN_A_VALUE}-1",
                "\udcc3\udca9": f"{_SECRET_IN_A_VALUE}-2",
            },
            1,
            2,
            id="surrogate-escaped-twin",
        ),
        pytest.param(
            {b"PATH": f"/{_SECRET_IN_A_VALUE}/a".encode(), "PATH": "/b"}, 0, 1, id="bytes-and-text"
        ),
    ],
)
async def test_keys_that_name_one_variable_once_encoded_are_refused_by_position(
    tmp_path, monkeypatch, env, first, second
):
    """Two keys that encode to the same bytes are one variable: the process would read one of
    the two values, and the copy would keep only the last, so the pair is refused."""
    from lionagi.providers import _cli_subprocess

    def spawned(*_args, **_kwargs):
        raise AssertionError("a process was started")

    monkeypatch.setattr(codex_mod, "CODEX_CLI", "codex")
    monkeypatch.setattr(codex_mod, "_linked_worktree_git_writable_roots", lambda _cwd: ())
    monkeypatch.setattr(_cli_subprocess.asyncio, "create_subprocess_exec", spawned)
    copied = CodexCodeRequest(prompt="hi", repo=tmp_path).model_copy(update={"env": env})
    with pytest.raises(ValueError) as refused:
        async for _ in codex_mod._ndjson_from_cli(copied):
            pass

    assert str(refused.value) == (
        f"The environment entries {first} and {second} name the same variable once encoded, "
        "so they cannot be passed to a CLI process."
    )
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


@pytest.mark.parametrize("part", ["key", "value"])
def test_a_claude_code_environment_the_spawn_cannot_encode_is_refused_at_construction(part):
    from lionagi.providers.anthropic.claude_code import ClaudeCodeRequest

    entry = (
        {f"K{_SECRET_IN_A_VALUE}\ud800": "v"}
        if part == "key"
        else {"ANTHROPIC_API_KEY": f"{_SECRET_IN_A_VALUE}\ud800"}
    )
    with pytest.raises(TypeError) as refused:
        ClaudeCodeRequest(prompt="hi", env={"HOME": "/h", **entry})

    assert str(refused.value) == (
        f"The environment entry 1 {part} holds a character the process environment cannot "
        "encode: a lone surrogate cannot be passed to a CLI process."
    )
    assert refused.value.__context__ is None
    assert _SECRET_IN_A_VALUE not in _everything_the_error_shows(refused.value)


@pytest.mark.parametrize("field", ["reasoning_effort", "plan_mode_reasoning_effort"])
@pytest.mark.parametrize("effort", [b"ultra", bytearray(b"ultra")], ids=["bytes", "bytearray"])
def test_an_effort_given_as_bytes_is_refused_rather_than_read_past_the_clamp(
    tmp_path, field, effort
):
    """Field validation reads bytes as text, so an effort given as bytes would reach the
    command line unclamped; the request refuses it by the field's name, in a validation
    error that holds no other part of the input."""
    with pytest.raises(pydantic.ValidationError) as refused:
        CodexCodeRequest(
            repo=tmp_path, model="gpt-5.5", **_fields_with_two_secrets({field: effort})
        )

    assert f"{field} must be a string, not {type(effort).__name__}" in str(refused.value)
    shown = " ".join(
        [str(refused.value), repr(refused.value), refused.value.json(), str(refused.value.errors())]
    )
    assert SECRET_PROMPT not in shown
    assert SECRET_OVERRIDE not in shown
    assert (
        CodexCodeRequest(
            prompt="hi", repo=tmp_path, model="gpt-5.5", **{field: "ultra"}
        ).model_dump()[field]
        == "xhigh"
    )

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
            "override key",
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
    assert message.startswith("The value of the config override")
    assert "The config override key" not in message

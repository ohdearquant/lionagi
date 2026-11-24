# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A profile's ``require_hooks`` key: parsed, carried to the leg, and checked at spawn."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lionagi import iModel
from lionagi._errors import ConfigurationError
from lionhub.agent.factory import forwarded_set_ignores_user_config
from lionhub.orchestration import console
from lionhub.orchestration.spawn import (
    AgentProfile,
    _parse_profile,
    build_chat_model,
    build_imodel_from_spec,
    chat_model_ignores_user_config,
    profile_config,
    refuse_require_hooks,
    refuse_require_hooks_at_submit,
)
from lionhub.providers.openai import _codex_hook_trust as trust
from lionhub.providers.openai.codex import CodexCodeRequest, check_require_hooks

_COMMAND = {"type": "command", "command": "echo hi"}


def _codex_home(tmp_path: Path, name: str = "codex", *, trusted: bool = False) -> tuple[Path, str]:
    """A codex home whose hooks.json declares one hook; returns the home and the hook's name."""
    home = Path(os.path.realpath(tmp_path)) / name
    home.mkdir()
    path = home / "hooks.json"
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]}}),
        encoding="utf-8",
    )
    hook = f"{path}:pre_tool_use:0:0"
    if trusted:
        digest = trust.hook_hash(trust.hook_identity("PreToolUse", "Bash", _COMMAND))
        (home / "config.toml").write_text(
            f"[hooks.state.{json.dumps(hook)}]\ntrusted_hash = {json.dumps(digest)}\n",
            encoding="utf-8",
        )
    return home, hook


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    """An empty HOME, so ~/.lionagi/settings.yaml is the test's own."""
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _guarded(model: str, **fields) -> AgentProfile:
    return AgentProfile(name="guarded", model=model, require_hooks=True, **fields)


# ── the profile key ───────────────────────────────────────────────────────


def test_the_key_is_off_when_absent_and_read_as_written_when_boolean():
    assert _parse_profile("plain", "---\nmodel: codex/gpt-5\n---\nBody.").require_hooks is False
    on = _parse_profile("on", "---\nmodel: codex/gpt-5\nrequire_hooks: true\n---\nBody.")
    off = _parse_profile("off", "---\nmodel: codex/gpt-5\nrequire_hooks: false\n---\nBody.")
    assert on.require_hooks is True
    assert off.require_hooks is False
    # A profile field, not an extra the leg would be handed.
    assert "require_hooks" not in on.extra
    assert profile_config(on)["require_hooks"] is True
    assert "require_hooks" not in profile_config(off)


def test_a_value_that_is_not_a_boolean_warns_and_is_read_as_true(monkeypatch):
    warned: list[str] = []
    monkeypatch.setattr(console, "warn", warned.append)
    profile = _parse_profile("yes", '---\nmodel: codex/gpt-5\nrequire_hooks: "yes"\n---\nBody.')
    assert profile.require_hooks is True
    assert warned == ["agent profile 'yes': require_hooks 'yes' is not true or false; requiring"]


# ── carried to the codex request ──────────────────────────────────────────


def test_li_agent_carries_the_key_to_the_codex_request_and_its_check_refuses(tmp_path):
    """`li agent -a` builds its model with build_chat_model; the request it makes carries the
    key, so the launch's own check is reached."""
    home, hook = _codex_home(tmp_path)
    model = build_chat_model(
        "codex", "gpt-5", False, False, None, env={"CODEX_HOME": str(home)}, require_hooks=True
    )
    payload, _ = model.endpoint.create_payload({"prompt": "hi"})
    request = payload["request"]
    assert request.require_hooks is True
    with pytest.raises(ConfigurationError, match="require_hooks") as refused:
        request.as_cmd_args()
    assert f"{hook} (untrusted)" in str(refused.value)
    # Without the key the request carries none, and the same home starts.
    plain = build_chat_model("codex", "gpt-5", False, False, None, env={"CODEX_HOME": str(home)})
    assert plain.endpoint.create_payload({"prompt": "hi"})[0]["request"].require_hooks is False


def test_a_worker_build_carries_the_key_to_the_codex_request(tmp_path):
    home, _hook = _codex_home(tmp_path)
    model = build_imodel_from_spec(
        "codex/gpt-5", ignore_user_config=True, env={"CODEX_HOME": str(home)}, require_hooks=True
    )
    request = model.endpoint.create_payload({"prompt": "hi"})[0]["request"]
    assert request.require_hooks is True
    assert request.ignore_user_config is True


# ── one answer before and at the launch ───────────────────────────────────


def _answer(call) -> str | None:
    try:
        call()
    except ConfigurationError as exc:
        return str(exc)
    return None


@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("ignore_user_config", [False, True])
def test_check_require_hooks_gives_the_launch_answer(tmp_path, trusted, ignore_user_config):
    home, _hook = _codex_home(tmp_path, trusted=trusted)
    work = Path(os.path.realpath(tmp_path)) / "work"
    work.mkdir()
    request = CodexCodeRequest(
        prompt="hi",
        repo=work,
        env={"CODEX_HOME": str(home)},
        require_hooks=True,
        ignore_user_config=ignore_user_config,
    )
    before = _answer(lambda: check_require_hooks(request, work))
    assert before == _answer(request.as_cmd_args)
    assert (before is None) is trusted


def test_a_hooks_table_in_an_ignored_user_config_gets_one_answer_before_and_at_launch(tmp_path):
    """A launch that ignores the user config while its `[hooks]` table declares hooks is
    refused whatever require_hooks says, and the check before the launch reaches that
    refusal too."""
    home = Path(os.path.realpath(tmp_path)) / "codex"
    home.mkdir()
    (home / "config.toml").write_text(
        '[[hooks.PreToolUse]]\nmatcher = "Bash"\n'
        '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "echo hi"\n',
        encoding="utf-8",
    )
    work = Path(os.path.realpath(tmp_path)) / "work"
    work.mkdir()
    request = CodexCodeRequest(
        prompt="hi",
        repo=work,
        env={"CODEX_HOME": str(home)},
        require_hooks=True,
        ignore_user_config=True,
    )
    before = _answer(lambda: check_require_hooks(request, work))
    assert before is not None
    assert before == _answer(request.as_cmd_args)


def test_a_request_without_the_key_is_not_checked_before_the_launch(tmp_path):
    home, _hook = _codex_home(tmp_path)
    work = Path(os.path.realpath(tmp_path)) / "work"
    work.mkdir()
    request = CodexCodeRequest(prompt="hi", repo=work, env={"CODEX_HOME": str(home)})
    assert check_require_hooks(request, work) is None


# ── API-model legs ────────────────────────────────────────────────────────


def test_an_api_model_leg_is_met_when_no_hook_is_declared(user_home):
    refuse_require_hooks("guarded", _guarded("openai/gpt-4.1"), iModel(model="openai/gpt-4.1"))


@pytest.mark.parametrize(
    "settings",
    [
        # Trusted or not, a CLI-spawned API-model leg loads no settings hooks.
        "hooks_external:\n  - event: pre_tool\n    command: [/bin/true]\n    trusted: true\n",
        "hooks_external:\n  - event: pre_tool\n    command: [/bin/true]\n",
        "hooks:\n  pre_tool: [some.module:guard]\n",
    ],
)
def test_an_api_model_leg_refuses_a_hook_the_global_settings_declare(user_home, settings):
    (user_home / ".lionagi" / "settings.yaml").write_text(settings, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="applies no lionagi hooks") as refused:
        refuse_require_hooks("guarded", _guarded("openai/gpt-4.1"), iModel(model="openai/gpt-4.1"))
    assert "~/.lionagi/settings.yaml" in str(refused.value)
    # A profile without the key is not refused.
    plain = AgentProfile(name="plain", model="openai/gpt-4.1")
    refuse_require_hooks("plain", plain, iModel(model="openai/gpt-4.1"))


def test_an_api_model_leg_refuses_a_hooks_assembly_on_its_profile(user_home):
    profile = _guarded("openai/gpt-4.1", extra={"hooks": [{"hook": "guard", "event": "pre_tool"}]})
    with pytest.raises(ConfigurationError, match="hooks in the agent profile"):
        refuse_require_hooks("guarded", profile, iModel(model="openai/gpt-4.1"))


# ── providers whose hook rules lionagi does not read ──────────────────────


@pytest.mark.parametrize(
    ("spec", "named"),
    [("claude_code/sonnet", "claude_code"), ("gemini-cli/gemini-2.5-pro", "gemini")],
)
def test_another_cli_provider_refuses_naming_it(user_home, tmp_path, spec, named):
    model = build_imodel_from_spec(spec)
    with pytest.raises(
        ConfigurationError, match="checked for codex and API-model legs only"
    ) as refused:
        refuse_require_hooks("guarded", _guarded(spec), model)
    assert named in str(refused.value)
    with pytest.raises(ConfigurationError, match=named):
        refuse_require_hooks_at_submit("guarded", _guarded(spec), spec, cwd=tmp_path)


# ── the check before the launch ───────────────────────────────────────────


def test_the_submit_check_reads_the_profile_codex_home(user_home, tmp_path, monkeypatch):
    clean = Path(os.path.realpath(tmp_path)) / "server-codex"
    clean.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(clean))
    home, hook = _codex_home(tmp_path)
    profile = _guarded("codex/gpt-5", codex_home=str(home))
    with pytest.raises(ConfigurationError, match="require_hooks") as refused:
        refuse_require_hooks_at_submit("guarded", profile, "codex/gpt-5", cwd=tmp_path)
    assert f"{hook} (untrusted)" in str(refused.value)
    # The server's own home declares nothing, so without codex_home the key is met.
    refuse_require_hooks_at_submit("guarded", _guarded("codex/gpt-5"), "codex/gpt-5", cwd=tmp_path)


def test_a_named_account_wins_over_the_profile_codex_home_at_submit(
    user_home, tmp_path, monkeypatch
):
    from lionhub.agent import subscription_accounts

    account_home, hook = _codex_home(tmp_path, "account-codex")
    profile_home, _ = _codex_home(tmp_path, "profile-codex", trusted=True)
    asked: list[dict] = []

    class _Leg:
        env = {"CODEX_HOME": str(account_home)}

    def resolve(provider, **kwargs):
        asked.append({"provider": provider, **kwargs})
        return _Leg()

    monkeypatch.setattr(subscription_accounts, "resolve_leg_account", resolve)
    profile = _guarded("codex/gpt-5", codex_home=str(profile_home))
    with pytest.raises(ConfigurationError, match="require_hooks") as refused:
        refuse_require_hooks_at_submit(
            "guarded", profile, "codex/gpt-5", account="work", cwd=tmp_path
        )
    assert f"{hook} (untrusted)" in str(refused.value)
    assert asked == [{"provider": "codex", "account": "work", "codex_home": None}]


@pytest.mark.parametrize(
    ("servers", "exclusive", "ignored"),
    [
        (None, False, False),
        (None, True, True),
        ({}, False, True),
        ({}, True, True),
        ({"probe": {"command": "true"}}, False, False),
        ({"probe": {"command": "true"}}, True, False),
    ],
)
def test_the_submit_answer_is_the_flag_the_builder_sets(servers, exclusive, ignored):
    """One predicate decides both: what the submit check asks, and what the build sets."""
    assert chat_model_ignores_user_config(servers, exclusive) is ignored
    model = build_chat_model(
        "codex", "gpt-5", False, False, None, mcp_servers=servers, mcp_exclusive=exclusive
    )
    # With no CLI option the builder returns the spec, which `li agent` turns into the model.
    if isinstance(model, str):
        model = iModel(model=model)
    payload, _ = model.endpoint.create_payload({"prompt": "x"})
    assert payload["request"].ignore_user_config is ignored


def test_an_allowlist_keeps_the_user_config_whatever_the_set():
    assert forwarded_set_ignores_user_config({}, exclusive=True) is True
    assert forwarded_set_ignores_user_config({}, exclusive=True, allowed_names=["probe"]) is False
    assert forwarded_set_ignores_user_config({}, exclusive=False) is False
    assert forwarded_set_ignores_user_config({"probe": {}}, exclusive=True) is False


def test_the_submit_check_skips_a_profile_without_the_key(tmp_path):
    plain = AgentProfile(name="plain", model="claude_code/sonnet")
    refuse_require_hooks_at_submit("plain", plain, "claude_code/sonnet", cwd=tmp_path)
    refuse_require_hooks_at_submit("none", None, "claude_code/sonnet", cwd=tmp_path)

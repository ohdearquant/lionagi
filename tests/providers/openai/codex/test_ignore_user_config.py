# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A codex request that ignores the user config.

``--ignore-user-config`` keeps CODEX_HOME/config.toml, and every server and profile
it declares, out of the run. What rides with it: the user config's sandbox mode,
network grant and hook trust records, which the flag would otherwise drop with the
rest of the file; no effort derived from a file the child never reads; a refusal
when the user config declares hooks itself; and a refusal when a project layer,
which the flag does not suppress, declares MCP servers.
"""

from __future__ import annotations

import logging
import sys

import pytest

from lionagi._errors import ConfigurationError
from lionagi.providers.openai.codex import CodexCodeRequest

if sys.version_info >= (3, 11):
    import tomllib
else:  # the declared `tomli`, a TOML 1.0 parser like tomllib
    import tomli as tomllib

GRANT = ["-c", "sandbox_workspace_write.network_access=true"]


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _has_pair(args: list[str], pair: list[str]) -> bool:
    return any(args[i : i + 2] == pair for i in range(len(args) - 1))


def _grants_network(home) -> None:
    (home / "config.toml").write_text("[sandbox_workspace_write]\nnetwork_access = true\n")


def test_the_flag_reaches_the_command_line(codex_home):
    assert (
        "--ignore-user-config"
        in CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    )
    assert "--ignore-user-config" not in CodexCodeRequest(prompt="hi").as_cmd_args()


def test_the_user_configs_network_grant_rides_beside_the_flag(codex_home):
    _grants_network(codex_home)

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _has_pair(args, GRANT)


def test_no_grant_is_invented_when_the_user_config_makes_none(codex_home):
    (codex_home / "config.toml").write_text("[sandbox_workspace_write]\nnetwork_access = false\n")
    denied = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    (codex_home / "config.toml").unlink()
    absent = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert not _has_pair(denied, GRANT)
    assert not _has_pair(absent, GRANT)


def test_a_run_that_reads_the_user_config_gets_no_extra_grant(codex_home):
    """Without the flag codex reads the grant from the file itself."""
    _grants_network(codex_home)

    assert not _has_pair(CodexCodeRequest(prompt="hi").as_cmd_args(), GRANT)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sandbox_workspace_write.network_access": False},
        {"sandbox_workspace_write": {"network_access": False}},
    ],
    ids=["dotted", "table"],
)
def test_an_override_naming_the_key_wins_over_the_user_config(codex_home, overrides):
    _grants_network(codex_home)

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()
    without = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert not _has_pair(args, GRANT)
    assert _has_pair(without, GRANT)


def test_the_grant_is_read_from_the_home_the_childs_environment_names(tmp_path, codex_home):
    """A request carrying a replacement environment runs codex against the home
    THAT environment names, so the grant is read there and not from ours."""
    _grants_network(codex_home)
    child_home = tmp_path / "child-home"
    child_home.mkdir()

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, env={"CODEX_HOME": str(child_home)}
    ).as_cmd_args()
    assert not _has_pair(args, GRANT)

    # The converse: the child's home grants and ours does not.
    _grants_network(child_home)
    (codex_home / "config.toml").unlink()
    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, env={"CODEX_HOME": str(child_home)}
    ).as_cmd_args()
    assert _has_pair(args, GRANT)


def test_an_unreadable_user_config_grants_nothing_and_says_so(codex_home, caplog):
    (codex_home / "config.toml").write_text("[sandbox_workspace_write\n")

    with caplog.at_level(logging.WARNING, logger="codex-cli"):
        args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert "--ignore-user-config" in args
    assert not _has_pair(args, GRANT)
    assert "codex config cannot be read" in caplog.text
    assert "no hook it trusts runs" in caplog.text


def test_no_effort_is_derived_from_a_user_config_the_run_ignores(codex_home):
    (codex_home / "config.toml").write_text('model_reasoning_effort = "ultra"\n')

    reads = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark")
    ignores = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark", ignore_user_config=True)

    # The control: a run that reads the file gets its value, clamped to the model.
    assert reads.reasoning_effort == "xhigh"
    assert ignores.reasoning_effort is None
    assert "model_reasoning_effort" not in " ".join(ignores.as_cmd_args())


def test_the_effort_skip_reads_the_flag_the_way_the_field_will(codex_home):
    """A before-validator sees raw input, where `bool("false")` is True."""
    (codex_home / "config.toml").write_text('model_reasoning_effort = "ultra"\n')

    request = CodexCodeRequest(prompt="hi", model="gpt-5.3-codex-spark", ignore_user_config="false")

    assert request.ignore_user_config is False
    assert request.reasoning_effort == "xhigh"


def test_an_explicit_effort_survives_the_flag(codex_home):
    request = CodexCodeRequest(
        prompt="hi", model="gpt-5.3-codex-spark", ignore_user_config=True, reasoning_effort="low"
    )

    assert request.reasoning_effort == "low"


HOOKS_JSON_KEY = "/home/u/.codex/hooks.json:pre_tool_use:0:0"
TRUST = (
    "[hooks.state]\n"
    f'[hooks.state."{HOOKS_JSON_KEY}"]\n'
    'trusted_hash = "sha256:abc"\n'
    "enabled = true\n"
)


def _carried_hook_state(args: list[str]):
    """The table a `-c hooks.state=...` pair hands codex, parsed as codex parses it."""
    values = [
        args[i + 1]
        for i in range(len(args) - 1)
        if args[i] == "-c" and args[i + 1].startswith("hooks.state=")
    ]
    assert len(values) <= 1, values
    if not values:
        return None
    return tomllib.loads(values[0])["hooks"]["state"]


def _sandbox_values(args: list[str]) -> list[str]:
    return [args[i + 1] for i in range(len(args) - 1) if args[i] in ("-s", "--sandbox")]


def test_the_user_configs_sandbox_mode_rides_beside_the_flag(codex_home):
    """Without the user config codex falls back to its read-only sandbox."""
    (codex_home / "config.toml").write_text('sandbox_mode = "workspace-write"\n')

    ignores = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    reads = CodexCodeRequest(prompt="hi").as_cmd_args()

    assert _sandbox_values(ignores) == ["workspace-write"]
    assert _sandbox_values(reads) == []


@pytest.mark.parametrize(
    ("request_kwargs", "expected"),
    [
        ({"sandbox": "read-only"}, ["read-only"]),
        ({"full_auto": True}, ["workspace-write"]),
        ({"bypass_approvals": True}, []),
        ({"config_overrides": {"sandbox_mode": "read-only"}}, []),
    ],
    ids=["explicit", "full-auto", "bypass", "override"],
)
def test_a_sandbox_the_request_names_wins_over_the_user_config(
    codex_home, request_kwargs, expected
):
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True, **request_kwargs).as_cmd_args()

    assert _sandbox_values(args) == expected


def test_no_sandbox_is_invented_when_the_user_config_sets_none_or_an_unknown_one(codex_home):
    absent = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    (codex_home / "config.toml").write_text('sandbox_mode = "everything"\n')
    unknown = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _sandbox_values(absent) == []
    assert _sandbox_values(unknown) == []


def test_the_user_configs_hook_trust_records_ride_beside_the_flag(codex_home):
    """Codex finds CODEX_HOME/hooks.json under the flag but runs a hook from it only
    when its trust record is in the user config or on the command line."""
    (codex_home / "config.toml").write_text(TRUST)

    ignores = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    reads = CodexCodeRequest(prompt="hi").as_cmd_args()

    assert _carried_hook_state(ignores) == {
        HOOKS_JSON_KEY: {"trusted_hash": "sha256:abc", "enabled": True}
    }
    assert _carried_hook_state(reads) is None


def test_a_disabled_hook_stays_disabled_and_a_malformed_record_is_left_out(codex_home):
    (codex_home / "config.toml").write_text(
        '[hooks.state."a:stop:0:0"]\nenabled = false\n'
        '[hooks.state."b:stop:1:0"]\nenabled = "yes"\ntrusted_hash = "sha256:b"\n'
        '[hooks.state."c:stop:2:0"]\ntrusted_hash = 7\n'
    )

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _carried_hook_state(args) == {"a:stop:0:0": {"enabled": False}}


def test_a_user_config_with_no_trust_records_carries_none(codex_home):
    (codex_home / "config.toml").write_text("[hooks.state]\n")

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _carried_hook_state(args) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {f'hooks.state."{HOOKS_JSON_KEY}".enabled': False},
        {"hooks": {"state": {}}},
    ],
    ids=["dotted", "table"],
)
def test_an_override_naming_the_hook_state_wins_over_the_user_config(codex_home, overrides):
    (codex_home / "config.toml").write_text(TRUST)

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()

    assert _carried_hook_state(args) is None


def test_the_hook_trust_records_are_read_from_the_home_the_childs_environment_names(
    tmp_path, codex_home
):
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    (child_home / "config.toml").write_text(TRUST)

    child = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, env={"CODEX_HOME": str(child_home)}
    ).as_cmd_args()
    ours = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert HOOKS_JSON_KEY in _carried_hook_state(child)
    assert _carried_hook_state(ours) is None


def test_a_user_config_that_declares_hooks_itself_is_refused(codex_home):
    """Their trust records name config.toml, which the run does not load."""
    (codex_home / "config.toml").write_text(
        "[[hooks.PreToolUse]]\n"
        'matcher = "shell"\n'
        "[[hooks.PreToolUse.hooks]]\n"
        'type = "command"\n'
        'command = "guard"\n'
    )

    with pytest.raises(ConfigurationError) as excinfo:
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    message = str(excinfo.value)
    assert "PreToolUse" in message
    assert str(codex_home / "config.toml") in message
    assert "hooks.json" in message
    # The control: a run that reads the file loads them itself.
    assert "--ignore-user-config" not in CodexCodeRequest(prompt="hi").as_cmd_args()


def test_an_empty_hook_event_list_is_not_refused(codex_home):
    (codex_home / "config.toml").write_text("[hooks]\nPreToolUse = []\n")

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert "--ignore-user-config" in args

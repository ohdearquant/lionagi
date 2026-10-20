# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A codex request that ignores the user config.

``--ignore-user-config`` keeps CODEX_HOME/config.toml, and every server, hook and
profile it declares, out of the run. Three things ride with it: the user config's
sandbox network grant, which the flag would otherwise drop with the rest of the
file; no effort derived from a file the child never reads; and a refusal when a
project layer, which the flag does not suppress, declares MCP servers.
"""

from __future__ import annotations

import logging

import pytest

from lionagi.providers.openai.codex import CodexCodeRequest

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
    assert "network grant of the ignored codex config is unknown" in caplog.text


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

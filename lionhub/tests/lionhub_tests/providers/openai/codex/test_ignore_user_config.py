# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A codex request that ignores the user config.

``--ignore-user-config`` keeps CODEX_HOME/config.toml, and every server and profile
it declares, out of the run. What rides with it: the user config's sandbox mode and
the `[sandbox_workspace_write]` settings that widen the sandbox, unless the run's
overrides set them, the settings that narrow it, unless the overrides set them, and its
hook trust records and project trust decisions merged with any the overrides write,
which the flag would otherwise drop with the rest of the file; no effort derived from
a file the child never reads; a refusal when the user config cannot be read or declares
hooks, a root profile or a permissions profile itself; a refusal when the run names a profile
itself; a refusal when a project layer sets a carried sandbox setting to another value or
sets a root profile; and a refusal when a project layer, which the flag does not suppress,
declares MCP servers.
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

from lionagi._errors import ConfigurationError
from lionhub.providers.openai import _codex_project_layers
from lionhub.providers.openai.codex import CodexCodeRequest

if sys.version_info >= (3, 11):
    import tomllib
else:  # the declared `tomli`; the locked 2.4.1 reads TOML 1.1
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


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex"])
def test_a_request_codex_home_that_is_not_absolute_is_refused(value, codex_home):
    """A request's CODEX_HOME is refused by the one predicate, before the config is read,
    with the same message a process home gets."""
    with pytest.raises(ConfigurationError, match="CODEX_HOME") as refusal:
        CodexCodeRequest(
            prompt="hi", ignore_user_config=True, env={"CODEX_HOME": value}
        ).as_cmd_args()
    message = str(refusal.value)
    assert "is not an absolute path" in message
    assert repr(value) in message


@pytest.mark.parametrize("value", ["~/.codex", "relative/.codex"])
def test_a_process_codex_home_that_is_not_absolute_is_refused(value, monkeypatch):
    """The process's CODEX_HOME goes through the same refusal as a request's."""
    monkeypatch.setenv("CODEX_HOME", value)
    with pytest.raises(ConfigurationError, match="CODEX_HOME") as refusal:
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    message = str(refusal.value)
    assert "is not an absolute path" in message
    assert repr(value) in message


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


@pytest.mark.parametrize(
    "text",
    [
        "[sandbox_workspace_write\n",
        # TOML 1.1, which codex reads: a TOML 1.0 parser refuses the file, so the hook it
        # declares cannot be seen and the file is refused as unreadable; a parser that
        # reads TOML 1.1 (`tomli` on Python 3.10) sees the hook and refuses it as declared
        'note = "\\x41"\n[hooks]\nPreToolUse = [{ type = "command", command = "true" }]\n',
    ],
    ids=["broken", "toml-1.1-with-hooks"],
)
def test_an_unreadable_user_config_is_refused(codex_home, text):
    (codex_home / "config.toml").write_text(text)

    with pytest.raises(ConfigurationError, match="cannot read the codex config|declares hooks"):
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    # the control: the run that reads the user config is not checked
    assert "--ignore-user-config" not in CodexCodeRequest(prompt="hi").as_cmd_args()


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


def _hook_state_pairs(args: list[str]) -> list[str]:
    return [
        args[i + 1]
        for i in range(len(args) - 1)
        if args[i] == "-c" and args[i + 1].startswith("hooks.state=")
    ]


def _carried_hook_state(args: list[str]):
    """The table the last `-c hooks.state=...` pair hands codex, parsed as codex parses it:
    a later `-c` of a key replaces what an earlier one set."""
    values = _hook_state_pairs(args)
    if not values:
        return None
    return tomllib.loads(values[-1])["hooks"]["state"]


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


TRUSTED = {"trusted_hash": "sha256:abc", "enabled": True}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"hooks": {"state": {}}}, {HOOKS_JSON_KEY: TRUSTED}),
        ({"hooks.state": {}}, {HOOKS_JSON_KEY: TRUSTED}),
        (
            {"hooks.state": {HOOKS_JSON_KEY: {"enabled": False}}},
            {HOOKS_JSON_KEY: {"trusted_hash": "sha256:abc", "enabled": False}},
        ),
        (
            {"hooks.state": {"b:stop:1:0": {"enabled": True}}},
            {HOOKS_JSON_KEY: TRUSTED, "b:stop:1:0": {"enabled": True}},
        ),
        (
            {"hooks.state": {HOOKS_JSON_KEY: {"enabled": False}}, "hooks": {"state": {}}},
            {HOOKS_JSON_KEY: TRUSTED},
        ),
        # codex splits a key at every `.`, quotes included, so this one writes a record
        # under the key `"/home/u/` and leaves the user config's own untouched
        (
            {f'hooks.state."{HOOKS_JSON_KEY}".enabled': False},
            {HOOKS_JSON_KEY: TRUSTED, '"/home/u/': {}},
        ),
    ],
    ids=["empty-table", "empty-dotted", "one-field", "other-record", "later-wins", "quoted-key"],
)
def test_hook_state_the_overrides_write_merges_with_the_user_configs_field_by_field(
    codex_home, overrides, expected
):
    """Codex reads hook state from the user config and then from the command line, field
    by field, the later winning; under the flag the user config's records ride merged with
    the overrides' own, after them on the command line."""
    (codex_home / "config.toml").write_text(TRUST)

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()

    assert _carried_hook_state(args) == expected
    # the control: with no records to carry, the overrides reach codex as written and
    # nothing is added after them
    (codex_home / "config.toml").unlink()
    alone = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()
    assert len(_hook_state_pairs(args)) == len(_hook_state_pairs(alone)) + 1


def test_hook_state_keys_merge_once_trimmed_as_codex_trims_them(codex_home):
    """Codex trims a hook state key of Unicode white space and skips one left empty, so a
    padded key in the user config and a plain one in the overrides name one hook. U+001C,
    which Python's `strip` would also take, is not white space to codex."""
    (codex_home / "config.toml").write_text(
        '[hooks.state." a:stop:0:0\t"]\ntrusted_hash = "sha256:a"\nenabled = true\n'
        '[hooks.state."  "]\nenabled = true\n'
    )

    args = CodexCodeRequest(
        prompt="hi",
        ignore_user_config=True,
        config_overrides={
            "hooks.state": {"a:stop:0:0": {"enabled": False}, "\x1ca:stop:0:0": {"enabled": True}}
        },
    ).as_cmd_args()

    assert _carried_hook_state(args) == {
        "a:stop:0:0": {"trusted_hash": "sha256:a", "enabled": False},
        "\x1ca:stop:0:0": {"enabled": True},
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"hooks": "x"},
        {"hooks.state": "x"},
        {"hooks.state": {HOOKS_JSON_KEY: 1}},
        {"hooks.state": {HOOKS_JSON_KEY: {"enabled": "yes"}}},
        {"hooks.state": {"b:stop:1:0": {"trusted_hash": 7}}},
    ],
    ids=["hooks", "state", "record", "enabled", "trusted-hash"],
)
def test_hook_state_overrides_codex_cannot_read_are_refused_beside_carried_records(
    codex_home, overrides
):
    (codex_home / "config.toml").write_text(TRUST)

    with pytest.raises(ConfigurationError, match="cannot be merged"):
        CodexCodeRequest(
            prompt="hi", ignore_user_config=True, config_overrides=overrides
        ).as_cmd_args()

    # the control: with no records to carry, the overrides reach codex as written
    (codex_home / "config.toml").unlink()
    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()
    assert "--ignore-user-config" in args


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


@pytest.mark.parametrize(
    "event",
    [
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
    ],
)
def test_every_hook_event_codex_reads_is_refused_in_the_user_config(codex_home, event):
    (codex_home / "config.toml").write_text(f'[[hooks.{event}]]\nmatcher = "x"\n')

    with pytest.raises(ConfigurationError, match=event):
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()


@pytest.mark.parametrize(
    "text",
    [
        '[hooks]\npre_tool_use = "ignored"\n',
        '[hooks]\nStopper = ["x"]\n',
        '[hooks]\ndescription = "x"\n',
    ],
    ids=["snake-case", "unknown", "description"],
)
def test_a_hooks_key_codex_does_not_read_as_an_event_is_not_refused(codex_home, text):
    """Codex reads `[hooks]` events by their own names and ignores any other key."""
    (codex_home / "config.toml").write_text(text + TRUST.replace("[hooks.state]\n", "", 1))

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _carried_hook_state(args) == {HOOKS_JSON_KEY: TRUSTED}


def _project_layer(directory, text: str):
    (directory / ".codex").mkdir(parents=True)
    (directory / ".codex" / "config.toml").write_text(text)


@pytest.mark.parametrize(
    ("layer", "refused"),
    [
        ('model = "x"\n', False),
        ('sandbox_mode = "danger-full-access"\n', False),
        ("[sandbox_workspace_write]\nnetwork_access = true\n", False),
        ("[sandbox_workspace_write]\nwritable_roots = []\n", False),
        ('sandbox_mode = "read-only"\n', True),
        ('default_permissions = ":read-only"\n', True),
        ("[sandbox_workspace_write]\nnetwork_access = false\n", True),
        ("sandbox_mode =\n", True),
    ],
    ids=[
        "neither",
        "same-mode",
        "same-grant",
        "other-field",
        "other-mode",
        "permissions-profile",
        "other-grant",
        "unparsed",
    ],
)
@pytest.mark.parametrize("where", ["repo", "above-the-repo"])
def test_a_project_layer_setting_the_sandbox_otherwise_refuses_the_run(
    tmp_path, codex_home, layer, refused, where
):
    """Codex applies a project layer only when it trusts the project, and a value carried
    on the command line beats the layer's, so a carried value is exact only where no layer
    sets it to anything else. A layer above the repository's root counts too: the root
    codex picks can be higher."""
    (codex_home / "config.toml").write_text(
        'sandbox_mode = "danger-full-access"\n[sandbox_workspace_write]\nnetwork_access = true\n'
    )
    repo = tmp_path / "outer" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _project_layer(repo if where == "repo" else tmp_path / "outer", layer)

    def args(**overrides):
        return CodexCodeRequest(
            prompt="hi", repo=repo, ignore_user_config=True, config_overrides=overrides
        ).as_cmd_args()

    if refused:
        with pytest.raises(ConfigurationError, match="Codex applies the project's setting"):
            args()
        # set in the overrides, nothing rides for the mode and the grant, and the run starts
        settled = args(sandbox_mode="read-only", sandbox_workspace_write={"network_access": False})
        assert _sandbox_values(settled) == []
        assert not _has_pair(settled, GRANT)
    else:
        assert _sandbox_values(args()) == ["danger-full-access"]
        assert _has_pair(args(), GRANT)
    # the control: with no project layer both ride
    (tmp_path / "bare").mkdir()
    bare = CodexCodeRequest(prompt="hi", repo=tmp_path / "bare", ignore_user_config=True)
    assert _sandbox_values(bare.as_cmd_args()) == ["danger-full-access"]
    assert _has_pair(bare.as_cmd_args(), GRANT)


@pytest.mark.parametrize("trust", ["trusted", "untrusted", None])
@pytest.mark.parametrize(
    ("user", "layer"),
    [
        ('sandbox_mode = "read-only"\n', 'sandbox_mode = "workspace-write"\n'),
        (
            'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n',
            'sandbox_mode = "read-only"\n[sandbox_workspace_write]\nnetwork_access = false\n',
        ),
    ],
    ids=["read-only-user", "workspace-write-user"],
)
def test_the_refusal_does_not_turn_on_the_trust_decision(tmp_path, codex_home, user, layer, trust):
    """Whether codex applies the layer turns on its trust decision: trusted, the layer's
    value; untrusted or with no decision, the user's, or a default codex derives from the
    decision. Dropping the user's value for an untrusted layer would let codex derive
    workspace-write past a read-only user config, and carrying it for a trusted one would
    beat the layer, so the run is refused whatever the decision says."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _project_layer(repo, layer)
    entry = f'[projects."{repo}"]\ntrust_level = "{trust}"\n' if trust else ""
    (codex_home / "config.toml").write_text(user + entry)

    with pytest.raises(ConfigurationError, match="sets it to another value"):
        CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()


def test_a_mode_the_request_sets_itself_is_not_checked_against_a_project_layer(
    tmp_path, codex_home
):
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    repo = tmp_path / "repo"
    _project_layer(repo, 'sandbox_mode = "read-only"\n')

    for request in (
        {"sandbox": "workspace-write"},
        {"full_auto": True},
        {"bypass_approvals": True},
    ):
        args = CodexCodeRequest(
            prompt="hi", repo=repo, ignore_user_config=True, **request
        ).as_cmd_args()
        assert "danger-full-access" not in args
    # the control: left to the user config, the mode is checked
    with pytest.raises(ConfigurationError, match="sets it to another value"):
        CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()


def test_reading_the_overrides_leaves_them_as_written(codex_home):
    """The command line's table is built from copies: a later dotted key writing into an
    earlier override's table does not change what that override hands codex."""
    (codex_home / "config.toml").write_text(TRUST)
    overrides = {"hooks": {"state": {}}, "hooks.state.extra": {"enabled": True}}

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()

    assert _has_pair(args, ["-c", "hooks={ state = {} }"])
    assert overrides == {"hooks": {"state": {}}, "hooks.state.extra": {"enabled": True}}
    assert _carried_hook_state(args) == {HOOKS_JSON_KEY: TRUSTED, "extra": {"enabled": True}}


def _workspace_pairs(args: list[str]) -> dict[str, object]:
    """The `sandbox_workspace_write` settings the command line hands codex."""
    pairs = {}
    for i in range(len(args) - 1):
        if args[i] == "-c" and args[i + 1].startswith("sandbox_workspace_write."):
            pairs.update(tomllib.loads(args[i + 1])["sandbox_workspace_write"])
    return pairs


WORKSPACE = (
    "[sandbox_workspace_write]\n"
    'writable_roots = ["/srv/data", "relative/dir", "~/cache"]\n'
    "network_access = true\n"
    "exclude_tmpdir_env_var = true\n"
    "exclude_slash_tmp = true\n"
)


def test_every_workspace_write_setting_codex_reads_rides_beside_the_flag(codex_home, caplog):
    """Codex reads four `[sandbox_workspace_write]` settings; a root that is not an
    absolute path is left out, and the warning names it."""
    (codex_home / "config.toml").write_text(WORKSPACE)

    with caplog.at_level(logging.WARNING, logger="codex-cli"):
        args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _workspace_pairs(args) == {
        "writable_roots": ["/srv/data"],
        "network_access": True,
        "exclude_tmpdir_env_var": True,
        "exclude_slash_tmp": True,
    }
    assert "relative/dir" in caplog.text and "not absolute paths" in caplog.text
    # the control: settings at codex's own defaults add nothing
    (codex_home / "config.toml").write_text(
        "[sandbox_workspace_write]\nwritable_roots = []\nexclude_slash_tmp = false\n"
    )
    assert (
        _workspace_pairs(CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()) == {}
    )


@pytest.mark.parametrize(
    ("layer", "expected"),
    [
        (
            'model = "x"\n',
            {"writable_roots", "network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"},
        ),
        (
            '[sandbox_workspace_write]\nwritable_roots = ["/srv/data"]\nnetwork_access = true\n',
            {"writable_roots", "network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"},
        ),
        (
            "[sandbox_workspace_write]\nexclude_tmpdir_env_var = false\nexclude_slash_tmp = false\n",
            {"writable_roots", "network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"},
        ),
        ("[sandbox_workspace_write]\nwritable_roots = []\n", None),
        ("[sandbox_workspace_write]\nnetwork_access = false\n", None),
        ("sandbox_workspace_write =\n", None),
    ],
    ids=["neither", "same-widening", "narrowing", "other-roots", "other-grant", "unparsed"],
)
def test_a_setting_that_widens_the_sandbox_is_checked_against_a_project_layer_and_one_that_narrows_it_is_not(
    tmp_path, codex_home, layer, expected
):
    """Carried on the command line, the user's value beats a project layer's. A widening
    setting a layer sets otherwise refuses the run (``None``); a narrowing one rides
    whatever the layer says, since it can only tighten the sandbox. The carried roots are
    the user's absolute ones, so a layer listing exactly those agrees."""
    (codex_home / "config.toml").write_text(WORKSPACE)
    repo = tmp_path / "repo"
    _project_layer(repo, layer)
    request = CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True)

    if expected is None:
        with pytest.raises(ConfigurationError, match="sandbox_workspace_write"):
            request.as_cmd_args()
    else:
        assert set(_workspace_pairs(request.as_cmd_args())) == expected


def test_a_workspace_write_setting_the_overrides_write_is_theirs(codex_home):
    (codex_home / "config.toml").write_text(WORKSPACE)
    overrides = {
        "sandbox_workspace_write": {"writable_roots": ["/elsewhere"]},
        "sandbox_workspace_write.exclude_slash_tmp": False,
    }

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()

    carried = [
        arg
        for i, arg in enumerate(args)
        if args[i - 1] == "-c" and arg.startswith("sandbox_workspace_write.")
    ]
    assert sorted(arg.split("=")[0] for arg in carried) == [
        "sandbox_workspace_write.exclude_slash_tmp",  # the override's own pair
        "sandbox_workspace_write.exclude_tmpdir_env_var",
        "sandbox_workspace_write.network_access",
    ]
    assert "sandbox_workspace_write.exclude_slash_tmp=false" in carried


@pytest.mark.parametrize(
    "text",
    [
        'default_permissions = ":workspace"\n',
        'default_permissions = "lab"\n[permissions.lab.network]\nenabled = true\n',
    ],
    ids=["built-in", "named"],
)
def test_a_user_config_that_sets_a_permissions_profile_is_refused(codex_home, text):
    """Codex applies `default_permissions` in place of `sandbox_mode`, so carrying the
    mode beside the flag without the profile could widen the sandbox."""
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n' + text)

    with pytest.raises(ConfigurationError, match="default_permissions") as refusal:
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    assert str(codex_home / "config.toml") in str(refusal.value)

    # the overrides naming their own profile replace the file's, and no mode rides
    args = CodexCodeRequest(
        prompt="hi",
        ignore_user_config=True,
        config_overrides={"default_permissions": ":read-only"},
    ).as_cmd_args()
    assert _sandbox_values(args) == []
    # the control: without the profile the run starts and the mode rides
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    assert _sandbox_values(
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    ) == ["danger-full-access"]


@pytest.mark.parametrize(
    "text",
    [
        'sandbox_mode = ["read-only"]\n',
        "[sandbox_mode]\nx = 1\n",
        'sandbox_mode = "open"\n',
        "sandbox_mode = 1\n",
    ],
    ids=["array", "table", "unknown", "integer"],
)
def test_a_sandbox_mode_codex_does_not_read_rides_nowhere_and_says_so(codex_home, caplog, text):
    (codex_home / "config.toml").write_text(text)

    with caplog.at_level(logging.WARNING, logger="codex-cli"):
        args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _sandbox_values(args) == []
    assert "not a mode codex reads" in caplog.text


def test_a_user_config_that_is_not_a_regular_file_is_refused(codex_home, caplog):
    (codex_home / "config.toml").mkdir()

    with pytest.raises(ConfigurationError, match="not a regular file") as refused:
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    assert "Replace it with a regular file" in str(refused.value)
    assert "codex also reads TOML 1.1" not in str(refused.value)

    # the control: no file at all is no config, and nothing is said
    (codex_home / "config.toml").rmdir()
    with caplog.at_level(logging.WARNING, logger="codex-cli"):
        args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    assert "--ignore-user-config" in args
    assert caplog.text == ""


# a directory with a `.` in it: codex splits a dotted `-c` key at every `.`, so the
# decisions ride as one inline table
PROJECT = "/home/u/work.repo"
PROJECT_TRUST = f'[projects."{PROJECT}"]\ntrust_level = "trusted"\n'


def _project_pairs(args: list[str]) -> list[str]:
    return [
        args[i + 1]
        for i in range(len(args) - 1)
        if args[i] == "-c" and args[i + 1].startswith("projects=")
    ]


def _carried_projects(args: list[str]):
    """The table the last `-c projects=...` pair hands codex, parsed as codex parses it."""
    values = _project_pairs(args)
    if not values:
        return None
    return tomllib.loads(values[-1])["projects"]


def test_the_user_configs_project_trust_decisions_ride_beside_the_flag(codex_home):
    """With no sandbox mode set, codex gives a directory it has a trust decision for
    workspace-write, and loads a project layer only when trusted. The decisions are in
    the user config, so they ride and codex matches the run's directory itself."""
    (codex_home / "config.toml").write_text(
        PROJECT_TRUST + '[projects."/home/u/other"]\ntrust_level = "untrusted"\n'
    )

    ignores = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    reads = CodexCodeRequest(prompt="hi").as_cmd_args()

    assert _carried_projects(ignores) == {
        PROJECT: {"trust_level": "trusted"},
        "/home/u/other": {"trust_level": "untrusted"},
    }
    assert _sandbox_values(ignores) == []
    assert _carried_projects(reads) is None


def test_an_entry_without_a_trust_level_rides_empty_and_one_codex_cannot_read_is_left_out(
    codex_home,
):
    """An entry for the run's directory shadows one for its repository root whether or not
    it sets a level, so an entry without one rides too."""
    (codex_home / "config.toml").write_text(
        '[projects]\n"/home/u/d" = 1\n'
        '[projects."/home/u/a"]\n'
        '[projects."/home/u/b"]\ntrust_level = "TRUSTED"\n'
        '[projects."/home/u/c"]\ntrust_level = "trusted"\n'
    )

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _carried_projects(args) == {"/home/u/a": {}, "/home/u/c": {"trust_level": "trusted"}}


def test_a_user_config_with_no_project_entries_carries_none(codex_home):
    (codex_home / "config.toml").write_text("[projects]\n")

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert _carried_projects(args) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"projects": {}}, {PROJECT: {"trust_level": "trusted"}}),
        (
            {"projects": {"/home/u/x": {"trust_level": "untrusted"}}},
            {PROJECT: {"trust_level": "trusted"}, "/home/u/x": {"trust_level": "untrusted"}},
        ),
        (
            {"projects": {PROJECT: {"trust_level": "untrusted"}}},
            {PROJECT: {"trust_level": "untrusted"}},
        ),
        ({"projects": {PROJECT: {}}}, {PROJECT: {"trust_level": "trusted"}}),
        # split at every `.`, the directory's included, this writes an entry under the key
        # `"/home/u/work` and leaves the user config's own untouched
        (
            {f'projects."{PROJECT}".trust_level': "untrusted"},
            {PROJECT: {"trust_level": "trusted"}, '"/home/u/work': {}},
        ),
        # a key TOML cannot read, which codex reads by the same split: an entry for
        # `/home/u/repo`
        (
            {"projects./home/u/repo.trust_level": "untrusted"},
            {PROJECT: {"trust_level": "trusted"}, "/home/u/repo": {"trust_level": "untrusted"}},
        ),
    ],
    ids=["empty-table", "other-entry", "same-entry", "no-level", "dotted-key", "path-key"],
)
def test_projects_the_overrides_write_merge_with_the_user_configs_field_by_field(
    codex_home, overrides, expected
):
    (codex_home / "config.toml").write_text(PROJECT_TRUST)

    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()

    assert _carried_projects(args) == expected
    # the control: with no entries to carry, the overrides reach codex as written and
    # nothing is added after them
    (codex_home / "config.toml").unlink()
    alone = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()
    assert len(_project_pairs(args)) == len(_project_pairs(alone)) + 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"projects": "x"},
        {"projects": {PROJECT: 1}},
        {"projects": {PROJECT: {"trust_level": "yes"}}},
    ],
    ids=["projects", "entry", "level"],
)
def test_projects_overrides_codex_cannot_read_are_refused_beside_carried_entries(
    codex_home, overrides
):
    (codex_home / "config.toml").write_text(PROJECT_TRUST)

    with pytest.raises(ConfigurationError, match="cannot be merged"):
        CodexCodeRequest(
            prompt="hi", ignore_user_config=True, config_overrides=overrides
        ).as_cmd_args()

    # the control: with no entries to carry, the overrides reach codex as written
    (codex_home / "config.toml").unlink()
    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides=overrides
    ).as_cmd_args()
    assert "--ignore-user-config" in args


def test_a_dangling_user_config_symlink_is_no_config(codex_home, tmp_path):
    """Codex finds no file behind a dangling symlink, so neither does the carry."""
    (codex_home / "config.toml").symlink_to(tmp_path / "missing.toml")

    args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert "--ignore-user-config" in args
    assert _sandbox_values(args) == []


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ('profile = "ro"\n[profiles.ro]\nsandbox_mode = "read-only"\n', "profile = 'ro'"),
        ('profile = "ro"\n[profiles.ro]\ndefault_permissions = ":read-only"\n', "profile = 'ro'"),
        ('profile = "ro"\n', "profile = 'ro'"),
        ("profile = 1\n", "a root profile key of type int"),
    ],
    ids=["profile-sandbox", "profile-permissions", "profile-only", "not-a-string"],
)
def test_a_root_profile_in_the_user_config_refuses_the_run(tmp_path, codex_home, text, named):
    """Codex 0.130.0, given no -p, applies a root `profile` key's profile sandbox settings in
    place of the root ones, and codex 0.162.1 still declares the key. The flag
    hides the key, so a trusted project would otherwise run workspace-write where the profile
    chose read-only."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    trusted = f'[projects."{repo}"]\ntrust_level = "trusted"\n'
    (codex_home / "config.toml").write_text(text + trusted)

    with pytest.raises(ConfigurationError, match="root profile|profile = ") as refused:
        CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()
    assert named in str(refused.value)

    # the control: the same file without the selector starts, and the trust decision rides
    (codex_home / "config.toml").write_text(text.split("\n", 1)[1] + trusted)
    args = CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()
    assert "--ignore-user-config" in args
    assert any(str(repo) in a and "trusted" in a for a in args)


def test_the_disagreement_refusal_names_both_values(tmp_path, codex_home):
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _project_layer(repo, 'sandbox_mode = "read-only"\n')

    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()
    assert "`sandbox_mode` to 'danger-full-access'" in str(refused.value)
    assert "sets it to another value, 'read-only'" in str(refused.value)


@pytest.mark.parametrize(
    "request_sets",
    [
        {"sandbox": "workspace-write"},
        {"full_auto": True},
        {"bypass_approvals": True},
        {"config_overrides": {"sandbox_mode": "read-only"}},
    ],
    ids=["sandbox", "full-auto", "bypass", "override"],
)
def test_a_root_profile_refuses_the_run_whatever_sandbox_the_request_sets(codex_home, request_sets):
    """The refusal is for the key codex reports, not for the sandbox it would select, so
    a request that sets the sandbox itself is refused too, and the message names no codex
    version boundary it has no source for."""
    (codex_home / "config.toml").write_text('profile = "ro"\n')

    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(prompt="hi", ignore_user_config=True, **request_sets).as_cmd_args()
    assert "codex 0.162.1 still declares the key" in str(refused.value)
    assert "0.135" not in str(refused.value)


def test_a_profile_the_run_names_is_refused_with_the_flag(codex_home):
    """Codex loads no user config under the flag, and a profile is user config, so the
    profile's settings would not apply."""
    with pytest.raises(ConfigurationError, match="and profile='work'"):
        CodexCodeRequest(prompt="hi", profile="work", ignore_user_config=True).as_cmd_args()

    # the control: without the flag the profile reaches codex
    args = CodexCodeRequest(prompt="hi", profile="work").as_cmd_args()
    assert _has_pair(args, ["-p", "work"])


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"profile": "ro"}, "profile = 'ro', which selects a profile, and"),
        ({"profile.name": "ro"}, "a profile key of type dict, and"),
    ],
    ids=["selects", "dotted"],
)
def test_overrides_that_select_a_profile_are_refused_with_the_flag(codex_home, overrides, named):
    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(
            prompt="hi", ignore_user_config=True, config_overrides=overrides
        ).as_cmd_args()
    assert f"the run's config overrides set {named}" in str(refused.value)

    # the control: another override starts the run
    args = CodexCodeRequest(
        prompt="hi", ignore_user_config=True, config_overrides={"model_verbosity": "low"}
    ).as_cmd_args()
    assert "--ignore-user-config" in args


def test_overrides_that_define_a_profile_without_selecting_it_start_with_the_flag(codex_home):
    """A profile table nothing selects is never applied, so it does not refuse the run."""
    args = CodexCodeRequest(
        prompt="hi",
        ignore_user_config=True,
        config_overrides={"profiles.ro.sandbox_mode": "read-only"},
    ).as_cmd_args()
    assert "--ignore-user-config" in args


def test_a_project_layer_root_profile_does_not_refuse_a_carried_setting(tmp_path, codex_home):
    """Codex drops `profile` and `profiles` from project layers, so a layer's root profile
    selects nothing and the carried mode applies as it would without the flag."""
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _project_layer(repo, 'profile = "ro"\n[profiles.ro]\nsandbox_mode = "read-only"\n')

    args = CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()

    assert "--ignore-user-config" in args
    assert _sandbox_values(args) == ["danger-full-access"]


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a mode the reader is refused"
)
def test_an_unreadable_user_config_gets_the_permission_remedy(codex_home):
    config = codex_home / "config.toml"
    config.write_text('sandbox_mode = "workspace-write"\n')
    config.chmod(0)
    try:
        with pytest.raises(ConfigurationError) as refused:
            CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    finally:
        config.chmod(0o600)
    assert "Make the file readable, and its folder searchable, by this process" in str(
        refused.value
    )
    assert "codex also reads TOML 1.1" not in str(refused.value)

    # the control: a file that is read but not parsed gets the parser remedy
    config.write_text("[sandbox_workspace_write\n")
    with pytest.raises(ConfigurationError, match="Make the file one this Python's TOML parser"):
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()


@pytest.mark.parametrize(
    "syntax",
    ['note = "k\\e"', 'note = "\\x41"', "note = {a = 1,}", "note = {\n  a = 1 }", "note = 07:30"],
)
def test_toml_1_1_syntax_in_the_user_config_is_read_as_this_pythons_parser_reads_it(
    codex_home, syntax
):
    """Codex reads TOML 1.1. tomllib (3.11 and later) and tomli before 2.4 read TOML 1.0, so
    under the flag such a config is refused with the parser remedy; tomli from 2.4 reads it."""
    (codex_home / "config.toml").write_text(f'{syntax}\nsandbox_mode = "workspace-write"\n')
    request = CodexCodeRequest(prompt="hi", ignore_user_config=True)
    tomli = None if sys.version_info >= (3, 11) else pytest.importorskip("tomli")
    if tomli is None or tuple(int(x) for x in tomli.__version__.split(".")[:2]) < (2, 4):
        with pytest.raises(ConfigurationError, match="Make the file one this Python's TOML parser"):
            request.as_cmd_args()
    else:
        args = request.as_cmd_args()
        assert "--ignore-user-config" in args
        assert "workspace-write" in " ".join(args)


@pytest.mark.parametrize(
    "config_text",
    ['\ufeffsandbox_mode = "workspace-write"\n', "[sandbox_workspace_write\n"],
    ids=["byte-order-mark", "broken-table"],
)
def test_a_user_config_refused_for_another_cause_names_the_parsers_error(codex_home, config_text):
    """A file every TOML parser refuses gets the parser's own error and the parser remedy;
    nothing in the message says to write the file in TOML 1.0."""
    (codex_home / "config.toml").write_text(config_text, encoding="utf-8")
    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    message = str(refused.value)
    assert "(at line 1, column " in message
    assert "Make the file one this Python's TOML parser reads" in message
    assert "TOML 1.0" not in message


@pytest.mark.parametrize("reads_1_1", [True, False], ids=["reads-1.1", "reads-1.0"])
def test_the_user_config_remedy_names_toml_1_1_only_where_the_parser_reads_1_0(
    codex_home, monkeypatch, reads_1_1
):
    """A parser that reads TOML 1.1 refused the file for another cause, so the remedy does not
    say codex reads 1.1 forms; one that reads only 1.0 may have refused a form codex reads."""
    monkeypatch.setattr(_codex_project_layers, "_READS_TOML_1_1", reads_1_1)
    (codex_home / "config.toml").write_text("[sandbox_workspace_write\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    message = str(refused.value)
    assert "Make the file one this Python's TOML parser reads" in message
    assert ("codex also reads TOML 1.1 forms" in message) is not reads_1_1


def test_a_project_layer_the_parser_refuses_is_named_with_the_parsers_limit(tmp_path, codex_home):
    (codex_home / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _project_layer(repo, "[sandbox_workspace_write\n")

    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()
    message = str(refused.value)
    assert "cannot be parsed here (" in message
    assert "(at line 1, column " in message
    assert "Set `sandbox_mode` in the run's config overrides" in message
    # the TOML 1.1 note only where the parser reads 1.0: tomllib (3.11 to 3.14), tomli before 2.4
    tomli = None if sys.version_info >= (3, 11) else pytest.importorskip("tomli")
    reads_1_1 = tomli is not None and tuple(int(x) for x in tomli.__version__.split(".")[:2]) >= (
        2,
        4,
    )
    assert ("codex reads it and this parser does not" in message) is not reads_1_1


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs a mode the reader is refused"
)
def test_a_user_config_in_a_folder_this_process_cannot_search_gets_the_folder_remedy(codex_home):
    (codex_home / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    codex_home.chmod(0o600)
    try:
        with pytest.raises(ConfigurationError) as refused:
            CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
    finally:
        codex_home.chmod(0o700)
    message = str(refused.value)
    assert "its folder searchable" in message
    assert "TOML parser reads" not in message


@pytest.mark.parametrize("where", ["user", "project"])
def test_a_config_nested_deeper_than_the_parser_follows_is_refused_by_name(
    tmp_path, codex_home, where
):
    """A parser that recurses per nesting level runs out of stack on a deep enough file; the
    run is refused with a ConfigurationError, never a RecursionError."""
    deep = "note = " + "[" * 1500 + "]" * 1500 + "\n"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    if where == "user":
        (codex_home / "config.toml").write_text(deep + 'sandbox_mode = "workspace-write"\n')
    else:
        (codex_home / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
        _project_layer(repo, deep)
    with pytest.raises(ConfigurationError) as refused:
        CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()
    message = str(refused.value)
    if where == "project":
        assert "cannot be parsed here (it nests deeper than this Python's TOML parser" in message
    else:
        assert "Make the file one this Python's TOML parser reads" in message

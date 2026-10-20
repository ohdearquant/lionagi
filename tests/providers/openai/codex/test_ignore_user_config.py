# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A codex request that ignores the user config.

``--ignore-user-config`` keeps CODEX_HOME/config.toml, and every server and profile
it declares, out of the run. What rides with it: the user config's sandbox mode and
the `[sandbox_workspace_write]` settings that widen the sandbox, unless the run's
overrides or a project layer set them, the settings that narrow it, unless the
overrides set them, and its hook trust records and project trust decisions merged with
any the overrides write, which the flag would otherwise drop with the rest of the file;
no effort derived from
a file the child never reads; a refusal when the user config declares hooks or a
permissions profile itself; and a refusal when a project layer, which the flag does
not suppress, declares MCP servers.
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
    assert "no hook or project it trusts is trusted" in caplog.text


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
    ("layer", "sandbox", "grant"),
    [
        ('model = "x"\n', ["danger-full-access"], True),
        ('sandbox_mode = "read-only"\n', [], True),
        ("[sandbox_workspace_write]\nnetwork_access = false\n", ["danger-full-access"], False),
        ("[sandbox_workspace_write]\nwritable_roots = []\n", ["danger-full-access"], True),
        ("sandbox_mode =\n", [], False),
    ],
    ids=["neither", "mode", "grant", "other-field", "unparsed"],
)
@pytest.mark.parametrize("where", ["repo", "above-the-repo"])
def test_a_project_layer_setting_the_sandbox_keeps_the_precedence_codex_gives_it(
    tmp_path, codex_home, layer, sandbox, grant, where
):
    """Codex reads a project layer above the user config and the command line above both,
    so a value carried on the command line would beat the project layer's. A layer above
    the repository's root counts too: the root codex picks can be higher."""
    (codex_home / "config.toml").write_text(
        'sandbox_mode = "danger-full-access"\n[sandbox_workspace_write]\nnetwork_access = true\n'
    )
    repo = tmp_path / "outer" / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _project_layer(repo if where == "repo" else tmp_path / "outer", layer)

    args = CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()

    assert _sandbox_values(args) == sandbox
    assert _has_pair(args, GRANT) is grant
    # the control: with no project layer both ride
    (tmp_path / "bare").mkdir()
    bare = CodexCodeRequest(prompt="hi", repo=tmp_path / "bare", ignore_user_config=True)
    assert _sandbox_values(bare.as_cmd_args()) == ["danger-full-access"]
    assert _has_pair(bare.as_cmd_args(), GRANT)


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
            "[sandbox_workspace_write]\nwritable_roots = []\nnetwork_access = false\n",
            {"exclude_tmpdir_env_var", "exclude_slash_tmp"},
        ),
        (
            "[sandbox_workspace_write]\nexclude_tmpdir_env_var = false\nexclude_slash_tmp = false\n",
            {"writable_roots", "network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp"},
        ),
        ("sandbox_workspace_write =\n", {"exclude_tmpdir_env_var", "exclude_slash_tmp"}),
    ],
    ids=["neither", "widening", "narrowing", "unparsed"],
)
def test_a_setting_that_widens_the_sandbox_gives_way_to_a_project_layer_and_one_that_narrows_it_does_not(
    tmp_path, codex_home, layer, expected
):
    """Carried on the command line, the user's value beats a project layer's. Giving way
    leaves a widening setting at codex's default; a narrowing one given way would widen
    the sandbox past what either file asks, so it rides."""
    (codex_home / "config.toml").write_text(WORKSPACE)
    repo = tmp_path / "repo"
    _project_layer(repo, layer)

    args = CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()

    assert set(_workspace_pairs(args)) == expected


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


def test_a_project_layer_naming_a_permissions_profile_keeps_the_mode_off_the_command_line(
    tmp_path, codex_home
):
    (codex_home / "config.toml").write_text('sandbox_mode = "danger-full-access"\n')
    repo = tmp_path / "repo"
    _project_layer(repo, 'default_permissions = ":read-only"\n')

    args = CodexCodeRequest(prompt="hi", repo=repo, ignore_user_config=True).as_cmd_args()

    assert _sandbox_values(args) == []


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


def test_a_user_config_that_is_not_a_regular_file_carries_nothing_and_says_so(codex_home, caplog):
    (codex_home / "config.toml").mkdir()

    with caplog.at_level(logging.WARNING, logger="codex-cli"):
        args = CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()

    assert "--ignore-user-config" in args
    assert "not a regular file" in caplog.text
    # the control: no file at all is no config, and nothing is said
    (codex_home / "config.toml").rmdir()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="codex-cli"):
        CodexCodeRequest(prompt="hi", ignore_user_config=True).as_cmd_args()
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
    ],
    ids=["empty-table", "other-entry", "same-entry", "no-level", "dotted-key"],
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

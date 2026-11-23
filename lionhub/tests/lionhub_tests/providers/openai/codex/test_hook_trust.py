# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Which hooks a codex home declares, and whether codex will run each one."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from lionagi._errors import ConfigurationError
from lionhub.providers.openai import _codex_hook_trust as trust
from lionhub.providers.openai.codex import CodexCodeRequest
from lionhub.serve.cli import doctor

_COMMAND = {"type": "command", "command": "echo hi"}
# The text codex hashes for that handler under PreToolUse with the matcher "Bash", written
# out by hand: keys sorted, compact, no field without a value.
_TEXT = (
    '{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"echo hi",'
    '"timeout":600,"type":"command"}],"matcher":"Bash"}'
)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _home(tmp_path: Path, name: str = "codex") -> Path:
    home = Path(os.path.realpath(tmp_path)) / name
    home.mkdir()
    return home


def _write_hooks(home: Path, hooks: dict, **extra) -> Path:
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": hooks, **extra}), encoding="utf-8")
    return path


def _write_state(home: Path, records: dict, extra: str = "") -> None:
    lines = [extra] if extra else []
    for name, fields in records.items():
        lines.append(f"[hooks.state.{json.dumps(name)}]")
        for field, value in fields.items():
            lines.append(f"{field} = {json.dumps(value)}")
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hash(event: str, matcher, handler) -> str:
    return trust.hook_hash(trust.hook_identity(event, matcher, handler))


# ── the hash ──────────────────────────────────────────────────────────────


def test_the_hash_is_the_sha256_of_the_sorted_compact_identity():
    assert _hash("PreToolUse", "Bash", _COMMAND) == _sha(_TEXT)


def test_a_field_with_no_value_is_left_out_of_the_hashed_text():
    """Codex turns the identity into a TOML value, which holds no null. The other reading,
    with the empty fields kept as null, gives a different hash; a hash codex wrote decides
    between them, and until one is matched the module says so."""
    with_nulls = (
        '{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"echo hi",'
        '"statusMessage":null,"timeout":600,"type":"command"}],"matcher":null}'
    )
    without = (
        '{"event_name":"pre_tool_use","hooks":[{"async":false,"command":"echo hi",'
        '"timeout":600,"type":"command"}]}'
    )
    computed = _hash("PreToolUse", None, _COMMAND)
    assert computed == _sha(without)
    assert computed != _sha(with_nulls)
    assert trust.HASH_CONFIRMED is False


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop", "Interrupt"])
def test_an_event_that_ignores_its_matcher_hashes_without_it(event):
    assert _hash(event, "Bash", _COMMAND) == _hash(event, None, _COMMAND)


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "SessionStart", "SessionEnd"])
def test_an_event_that_reads_its_matcher_hashes_it(event):
    assert _hash(event, "Bash", _COMMAND) != _hash(event, None, _COMMAND)
    assert _hash(event, "Bash", _COMMAND) != _hash(event, "Edit", _COMMAND)


def _normalized(event: str, handler: dict) -> dict:
    return trust.hook_identity(event, None, handler)["hooks"][0]


@pytest.mark.parametrize(
    ("event", "written", "hashed"),
    [
        ("PreToolUse", None, 600),
        ("PreToolUse", 0, 1),
        ("PreToolUse", 5000, 5000),
        ("SessionEnd", None, 1),
        ("SessionEnd", 2, 2),
        ("SessionEnd", 10, 3),
        ("Interrupt", None, 1),
        ("Interrupt", 0, 1),
        ("Interrupt", 60, 3),
    ],
)
def test_the_timeout_is_always_hashed_as_codex_normalizes_it(event, written, hashed):
    handler = dict(_COMMAND) if written is None else {**_COMMAND, "timeout": written}
    assert _normalized(event, handler)["timeout"] == hashed


def test_async_is_always_hashed_and_false_when_unset():
    assert _normalized("PreToolUse", _COMMAND)["async"] is False
    assert _normalized("PreToolUse", {**_COMMAND, "async": True})["async"] is True
    assert _hash("PreToolUse", None, _COMMAND) == _hash(
        "PreToolUse", None, {**_COMMAND, "async": False}
    )


def test_the_windows_command_never_reaches_the_hash():
    assert _hash("PreToolUse", None, _COMMAND) == _hash(
        "PreToolUse", None, {**_COMMAND, "commandWindows": "echo other"}
    )


def test_a_status_message_is_hashed_when_written():
    assert _hash("PreToolUse", None, _COMMAND) != _hash(
        "PreToolUse", None, {**_COMMAND, "statusMessage": "checking"}
    )


@pytest.mark.parametrize(
    ("event", "limit", "kept"),
    [
        ("PreToolUse", 4000, True),
        ("PreToolUse", 2500, False),
        ("SubagentStart", 4000, True),
        ("Stop", 4000, False),
        ("SessionEnd", 4000, False),
    ],
)
def test_a_context_limit_is_hashed_only_off_its_default_and_where_it_applies(event, limit, kept):
    normalized = _normalized(event, {**_COMMAND, "additionalContextLimit": limit})
    assert ("additionalContextLimit" in normalized) is kept


def test_an_mcp_tool_handler_hashes_its_server_tool_input_and_timeout():
    handler = {"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": 1}}
    assert _normalized("PreToolUse", handler) == {
        "type": "mcp_tool",
        "server": "s",
        "tool": "t",
        "input": {"a": 1},
        "timeout": 600,
        "statusMessage": None,
    }
    assert _hash("PreToolUse", None, handler) != _hash(
        "PreToolUse", None, {**handler, "input": {"a": 2}}
    )


@pytest.mark.parametrize("kind", ["prompt", "agent"])
def test_a_handler_codex_skips_at_load_has_no_identity(kind):
    assert trust.hook_identity("PreToolUse", None, {"type": kind, "prompt": "x"}) is None


# ── entry names and statuses ──────────────────────────────────────────────


def test_entries_are_named_by_file_event_group_and_handler(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(
        home,
        {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [_COMMAND]},
                {"hooks": [{"type": "prompt", "prompt": "x"}, _COMMAND]},
            ],
            "Stop": [{"hooks": [_COMMAND]}],
        },
    )
    report = trust.hook_trust_report(home)
    assert [hook.hook.name for hook in report.hooks] == [
        f"{path}:pre_tool_use:0:0",
        f"{path}:pre_tool_use:1:1",
        f"{path}:stop:0:0",
    ]
    assert report.problems == ()


def test_a_hook_reads_trusted_untrusted_and_disabled_from_the_state(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND] * 3}]})
    names = [f"{path}:pre_tool_use:0:{index}" for index in range(3)]
    _write_state(
        home,
        {names[0]: {"trusted_hash": _sha(_TEXT)}, names[2]: {"enabled": False}},
    )
    statuses = [hook.status for hook in trust.hook_trust_report(home).hooks]
    assert statuses == [trust.TRUSTED, trust.UNTRUSTED, trust.DISABLED]
    assert [hook.status for hook in trust.hook_trust_report(home).refused()] == [trust.UNTRUSTED]


def test_a_different_recorded_hash_is_unconfirmed_until_the_hash_is_confirmed(
    tmp_path, monkeypatch
):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]})
    _write_state(home, {f"{path}:pre_tool_use:0:0": {"trusted_hash": _sha("edited")}})
    (hook,) = trust.hook_trust_report(home).hooks
    assert hook.status == trust.UNCONFIRMED
    assert hook.recorded_hash == _sha("edited")
    monkeypatch.setattr(trust, "HASH_CONFIRMED", True)
    assert trust.hook_trust_report(home).hooks[0].status == trust.MODIFIED


def test_a_hook_trusted_under_one_home_is_untrusted_under_another(tmp_path):
    """Two homes declaring the same hook: the trust record names the first home's file."""
    first, second = _home(tmp_path, "first"), _home(tmp_path, "second")
    hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]}
    trusted = {f"{_write_hooks(first, hooks)}:pre_tool_use:0:0": {"trusted_hash": _sha(_TEXT)}}
    _write_hooks(second, hooks)
    _write_state(first, trusted)
    _write_state(second, trusted)
    assert trust.hook_trust_report(first).hooks[0].status == trust.TRUSTED
    assert trust.hook_trust_report(second).hooks[0].status == trust.UNTRUSTED


def test_the_config_hooks_are_named_by_the_config_file(tmp_path):
    home = _home(tmp_path)
    config = home / "config.toml"
    config.write_text(
        '[[hooks.PreToolUse]]\nmatcher = "Bash"\n'
        '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "echo hi"\n'
        f'[hooks.state."{config}:pre_tool_use:0:0"]\ntrusted_hash = "{_sha(_TEXT)}"\n',
        encoding="utf-8",
    )
    (hook,) = trust.hook_trust_report(home).hooks
    assert (hook.hook.name, hook.status) == (f"{config}:pre_tool_use:0:0", trust.TRUSTED)
    assert trust.hook_trust_report(home, include_config_hooks=False).hooks == ()


def test_the_overrides_hook_state_is_read_after_the_user_config(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND] * 2}]})
    first, second = f"{path}:pre_tool_use:0:0", f"{path}:pre_tool_use:0:1"
    _write_state(home, {first: {"trusted_hash": _sha("stale")}})
    report = trust.hook_trust_report(
        home,
        override_state={f"  {first} ": {"trusted_hash": _sha(_TEXT)}, second: {"enabled": False}},
    )
    assert [hook.status for hook in report.hooks] == [trust.TRUSTED, trust.DISABLED]


def test_a_state_record_codex_cannot_read_counts_as_none(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]})
    report = trust.hook_trust_report(
        home,
        override_state={f"{path}:pre_tool_use:0:0": {"trusted_hash": _sha(_TEXT), "enabled": 1}},
    )
    assert report.hooks[0].status == trust.UNTRUSTED


@pytest.mark.parametrize(
    ("content", "named"),
    [
        ("{not json", "cannot be read as JSON"),
        ('["hooks"]', "not a JSON object"),
        ('{"hooks": {}, "extra": 1}', "fields Codex refuses (extra)"),
        ('{"description": "d"}', "no hooks field"),
        ('{"hooks": {"PreToolUse": {"hooks": []}}}', "not a list of matcher groups"),
        ('{"hooks": {"PreToolUse": [{"matcher": "x"}]}}', "group 0 has no list of hooks"),
        ('{"hooks": {"Stop": [{"hooks": [{"type": "shell"}]}]}}', "type 'shell'"),
        ('{"hooks": {"Stop": [{"hooks": [{"type": "command"}]}]}}', "command is not text"),
    ],
)
def test_a_hooks_file_codex_cannot_read_is_a_problem_not_an_empty_list(tmp_path, content, named):
    home = _home(tmp_path)
    (home / "hooks.json").write_text(content, encoding="utf-8")
    report = trust.hook_trust_report(home)
    assert report.hooks == ()
    (problem,) = report.problems
    assert named in problem


def test_an_unreadable_config_is_a_problem(tmp_path):
    home = _home(tmp_path)
    (home / "config.toml").write_text("hooks = [", encoding="utf-8")
    (problem,) = trust.hook_trust_report(home).problems
    assert "cannot read the codex config" in problem


def test_a_handler_holding_what_toml_cannot_hold_reads_unreadable(tmp_path):
    home = _home(tmp_path)
    handler = {"type": "mcp_tool", "server": "s", "tool": "t", "input": {"a": [1, None]}}
    _write_hooks(home, {"Stop": [{"hooks": [handler]}]})
    (hook,) = trust.hook_trust_report(home).hooks
    assert (hook.hook.hash, hook.status) == (None, trust.UNREADABLE)


def test_a_home_with_no_hooks_reports_none(tmp_path):
    report = trust.hook_trust_report(_home(tmp_path))
    assert (report.hooks, report.problems) == ((), ())


def test_the_home_is_the_one_the_environment_names_made_canonical(tmp_path):
    home = _home(tmp_path)
    link = Path(os.path.realpath(tmp_path)) / "link"
    link.symlink_to(home)
    path = _write_hooks(home, {"Stop": [{"hooks": [_COMMAND]}]})
    report = trust.hook_trust_for_env({"CODEX_HOME": str(link)})
    assert report.codex_home == home
    assert report.hooks[0].hook.name == f"{path}:stop:0:0"


# ── require_hooks ─────────────────────────────────────────────────────────


def _request(tmp_path: Path, home: Path, **kwargs) -> CodexCodeRequest:
    work = Path(os.path.realpath(tmp_path)) / "work"
    work.mkdir(exist_ok=True)
    return CodexCodeRequest(prompt="hi", repo=work, env={"CODEX_HOME": str(home)}, **kwargs)


def test_require_hooks_refuses_a_hook_that_will_not_run(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]})
    with pytest.raises(ConfigurationError, match="require_hooks") as refused:
        _request(tmp_path, home, require_hooks=True).as_cmd_args()
    assert f"{path}:pre_tool_use:0:0 (untrusted)" in str(refused.value)
    # The same request without the setting starts, and codex skips the hook.
    assert _request(tmp_path, home).as_cmd_args()


def test_require_hooks_starts_when_every_hook_is_trusted_or_disabled(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND] * 2}]})
    _write_state(
        home,
        {
            f"{path}:pre_tool_use:0:0": {"trusted_hash": _sha(_TEXT)},
            f"{path}:pre_tool_use:0:1": {"enabled": False},
        },
    )
    # The setting adds nothing to the command line: it decides whether codex starts.
    assert (
        _request(tmp_path, home, require_hooks=True).as_cmd_args()
        == _request(tmp_path, home).as_cmd_args()
    )


def test_require_hooks_reads_the_trust_the_overrides_write(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]})
    overrides = {"hooks.state": {f"{path}:pre_tool_use:0:0": {"trusted_hash": _sha(_TEXT)}}}
    assert _request(tmp_path, home, require_hooks=True, config_overrides=overrides).as_cmd_args()


def test_require_hooks_refuses_an_unconfirmed_hash_and_says_why(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]})
    _write_state(home, {f"{path}:pre_tool_use:0:0": {"trusted_hash": _sha("edited")}})
    with pytest.raises(ConfigurationError, match="not yet been checked against a hash codex"):
        _request(tmp_path, home, require_hooks=True).as_cmd_args()


def test_require_hooks_refuses_a_hooks_file_it_cannot_read(tmp_path):
    home = _home(tmp_path)
    (home / "hooks.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="cannot be read as JSON"):
        _request(tmp_path, home, require_hooks=True).as_cmd_args()


def test_require_hooks_under_ignore_user_config_reads_the_carried_trust(tmp_path):
    home = _home(tmp_path)
    path = _write_hooks(home, {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]})
    _write_state(home, {f"{path}:pre_tool_use:0:0": {"trusted_hash": _sha(_TEXT)}})
    assert _request(tmp_path, home, require_hooks=True, ignore_user_config=True).as_cmd_args()


# ── li doctor ─────────────────────────────────────────────────────────────


def test_doctor_reports_each_codex_home_and_names_the_hook_that_will_not_run(tmp_path):
    first, second = _home(tmp_path, "first"), _home(tmp_path, "second")
    hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [_COMMAND]}]}
    first_path = _write_hooks(first, hooks)
    second_path = _write_hooks(second, hooks)
    _write_state(first, {f"{first_path}:pre_tool_use:0:0": {"trusted_hash": _sha(_TEXT)}})
    logins = Path(os.path.realpath(tmp_path)) / "logins"
    logins.mkdir()
    (logins / "accounts.toml").write_text(
        f'[codex.second]\nhome = "{second}"\n[codex.gone]\nhome = "{tmp_path}/gone"\n',
        encoding="utf-8",
    )
    checks = doctor._check_codex_hooks(
        {"CODEX_HOME": str(first), "LIONAGI_LOGINS_DIR": str(logins)}
    )
    assert checks["codex_hooks"]["status"] == "ok"
    assert "1 trusted" in checks["codex_hooks"]["detail"]
    assert checks["codex_hooks:account:second"]["status"] == "warn"
    assert (
        f"{second_path}:pre_tool_use:0:0 (untrusted)"
        in checks["codex_hooks:account:second"]["detail"]
    )
    assert checks["codex_hooks:account:gone"]["status"] == "warn"


def test_doctor_leaves_a_home_whose_hooks_cannot_be_read_unknown(tmp_path):
    home = _home(tmp_path)
    (home / "hooks.json").write_text("{not json", encoding="utf-8")
    logins = Path(os.path.realpath(tmp_path)) / "logins"
    checks = doctor._check_codex_hooks({"CODEX_HOME": str(home), "LIONAGI_LOGINS_DIR": str(logins)})
    assert checks["codex_hooks"]["status"] == "unknown"


def test_doctor_reports_a_home_with_no_hooks_as_ok(tmp_path):
    home = _home(tmp_path)
    logins = Path(os.path.realpath(tmp_path)) / "logins"
    checks = doctor._check_codex_hooks({"CODEX_HOME": str(home), "LIONAGI_LOGINS_DIR": str(logins)})
    assert checks == {
        "codex_hooks": {
            "status": "ok",
            "detail": f"{home}: no hooks declared in hooks.json or config.toml",
        }
    }

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reading a hooks file named at submit, and planning what a leg carries from it.

ADR-0048 Amendment 1, H2 (reading), H3 (carried, reported, refused) and H6 (rule paths).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from lionhub.hooks.hooks_from import (
    MAX_HOOKS_FROM_BYTES,
    HooksFromError,
    plan_carry,
    read_hooks_file,
    render_permission_rule,
)

# Planted in every refused fixture: no refusal may carry it.
MARKER = "MARKER-7f3a91"


def _write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    return path


def _refusal(path: Path | str) -> HooksFromError:
    with pytest.raises(HooksFromError) as caught:
        read_hooks_file(str(path))
    assert MARKER not in str(caught.value) and MARKER not in repr(caught.value)
    return caught.value


# H2: reading the file


def test_a_relative_path_and_dash_are_refused(tmp_path, monkeypatch):
    _write(tmp_path / "settings.json", "{}")
    monkeypatch.chdir(tmp_path)
    assert _refusal("settings.json").rule == "absolute-path"
    assert _refusal("-").rule == "absolute-path"


def test_a_link_as_the_last_component_is_refused(tmp_path):
    target = _write(tmp_path / "real.json", json.dumps({"note": MARKER}))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert _refusal(link).rule == "no-symlink"


def test_a_link_in_a_parent_directory_is_resolved_and_recorded(tmp_path):
    real_dir = tmp_path / "real"
    _write(real_dir / "settings.json", "{}")
    (tmp_path / "alias").symlink_to(real_dir)
    given = tmp_path / "alias" / "settings.json"
    hooks_file = read_hooks_file(str(given))
    assert hooks_file.path == str(given)
    assert hooks_file.resolved_path == str(real_dir.resolve() / "settings.json")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "settings.json"
    os.mkfifo(fifo)
    assert _refusal(fifo).rule == "regular-file"


def test_a_directory_is_refused(tmp_path):
    (tmp_path / "settings.json").mkdir()
    assert _refusal(tmp_path / "settings.json").rule in ("regular-file", "open")


def _padded(size: int) -> str:
    head = '{"note": "'
    tail = '"}'
    return head + MARKER + "x" * (size - len(head) - len(tail) - len(MARKER)) + tail


def test_a_file_at_the_cap_is_read_and_one_byte_over_is_refused(tmp_path):
    at_cap = _write(tmp_path / "at.json", _padded(MAX_HOOKS_FROM_BYTES))
    assert len(read_hooks_file(str(at_cap)).data) == MAX_HOOKS_FROM_BYTES
    over = _write(tmp_path / "over.json", _padded(MAX_HOOKS_FROM_BYTES + 1))
    assert _refusal(over).rule == "size"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_refused(tmp_path, constant):
    path = _write(tmp_path / "s.json", '{"note": "' + MARKER + '", "x": ' + constant + "}")
    assert _refusal(path).rule == "json"


@pytest.mark.parametrize(
    "text",
    [
        '{"permissions": {"deny": ["Read(.env)"], "deny": []}, "note": "%s"}',
        '{"note": "%s", "note": "again"}',
    ],
)
def test_a_repeated_name_is_refused_at_any_depth(tmp_path, text):
    path = _write(tmp_path / "s.json", text % MARKER)
    error = _refusal(path)
    assert error.rule == "json" and "repeats a name" in str(error)


@pytest.mark.parametrize(
    "content",
    [
        '{"note": "' + MARKER + '",',
        '["' + MARKER + '"]',
        b'{"note": "' + MARKER.encode() + b'\xff"}',
    ],
)
def test_bad_json_a_non_object_and_non_utf8_are_refused_without_their_bytes(tmp_path, content):
    path = _write(tmp_path / "s.json", content)
    assert _refusal(path).rule == "json"


def test_the_accepted_bytes_and_their_digest_are_returned(tmp_path):
    raw = json.dumps({"permissions": {"deny": ["Read(.env)"]}}).encode()
    hooks_file = read_hooks_file(str(_write(tmp_path / "s.json", raw)))
    assert hooks_file.data == raw
    assert hooks_file.sha256 == hashlib.sha256(raw).hexdigest()
    assert hooks_file.document == {"permissions": {"deny": ["Read(.env)"]}}


# H6: rule paths

SOURCE = Path("/work/project")
LEG = Path("/work/legs/one")


def _project(rule: str) -> list[str]:
    return render_permission_rule(rule, source_kind="project", source_dir=SOURCE, leg_cwd=LEG)


def _refused_rule(rule: str, *, kind: str = "project") -> str:
    source_dir = SOURCE if kind == "project" else None
    with pytest.raises(ValueError) as caught:
        render_permission_rule(rule, source_kind=kind, source_dir=source_dir, leg_cwd=LEG)
    return str(caught.value)


def test_a_relative_rule_renders_both_absolute_forms_when_the_leg_works_elsewhere():
    assert _project("Read(secrets/**)") == [
        "Read(//work/project/**/secrets/**)",
        "Read(//work/legs/one/**/secrets/**)",
    ]
    assert render_permission_rule(
        "Read(secrets/**)", source_kind="project", source_dir=SOURCE, leg_cwd=SOURCE
    ) == ["Read(//work/project/**/secrets/**)"]


def test_a_bare_name_renders_the_any_depth_form_for_both_directories():
    assert _project("Read(.env)") == [
        "Read(//work/project/**/.env)",
        "Read(//work/legs/one/**/.env)",
    ]


def test_one_segment_and_dotted_patterns_are_any_depth_and_two_segments_are_anchored():
    any_depth = ["Edit(//work/project/**/secrets/**)", "Edit(//work/legs/one/**/secrets/**)"]
    assert _project("Edit(secrets/**)") == any_depth
    assert _project("Edit(./secrets/**)") == any_depth
    assert _project("Edit(src/components/**)") == [
        "Edit(//work/project/src/components/**)",
        "Edit(//work/legs/one/src/components/**)",
    ]


def test_slash_rules_anchor_by_settings_source_and_root_and_home_rules_carry():
    assert _project("Read(/config/*.pem)") == [
        "Read(//work/project/config/*.pem)",
        "Read(//work/legs/one/config/*.pem)",
    ]
    assert render_permission_rule(
        "Read(/notes.md)", source_kind="user", source_dir=None, leg_cwd=LEG
    ) == ["Read(~/.claude/notes.md)"]
    assert "no known anchor" in _refused_rule("Read(/x)", kind="other")
    assert _project("Read(//etc/shadow)") == ["Read(//etc/shadow)"]
    assert _project("Read(~/.ssh/**)") == ["Read(~/.ssh/**)"]


@pytest.mark.parametrize("name", ["proj[1]", "a*b", "a?b", "a{b,c}", "a(b)", "a\\b"])
def test_a_directory_the_rule_syntax_reads_as_a_pattern_refuses(name):
    odd = Path("/work") / name
    for source_dir, leg_cwd, which in (
        (odd, LEG, "the source project directory"),
        (SOURCE, odd, "the leg's working directory"),
    ):
        for rule in ("Read(.env)", "Read(/config/key.pem)", "Edit(src/components/**)"):
            with pytest.raises(ValueError) as caught:
                render_permission_rule(
                    rule, source_kind="project", source_dir=source_dir, leg_cwd=leg_cwd
                )
            assert str(caught.value).startswith(which), (rule, str(caught.value))
            assert name not in str(caught.value)
        # a rule that pastes no directory still carries
        assert render_permission_rule(
            "Read(//etc/shadow)", source_kind="project", source_dir=source_dir, leg_cwd=leg_cwd
        ) == ["Read(//etc/shadow)"]


def test_a_directory_that_is_not_absolute_refuses():
    for source_dir, leg_cwd, which in (
        (Path("work/project"), LEG, "the source project directory"),
        (SOURCE, Path("legs/one"), "the leg's working directory"),
    ):
        for rule in ("Read(.env)", "Read(/config/key.pem)"):
            with pytest.raises(ValueError) as caught:
                render_permission_rule(
                    rule, source_kind="project", source_dir=source_dir, leg_cwd=leg_cwd
                )
            assert (
                str(caught.value)
                == f"{which} is not an absolute path, so no rule can be anchored there"
            )


def test_negation_and_relative_rules_outside_project_settings_refuse():
    assert "negation" in _refused_rule("Read(!keep.txt)")
    assert "no known directory" in _refused_rule("Read(.env)", kind="other")
    assert "no known directory" in _refused_rule("Read(.env)", kind="user")


def test_cd_rules_carry_only_from_root_or_home():
    assert _project("Cd(//srv/data)") == ["Cd(//srv/data)"]
    assert _project("Cd(~/scratch)") == ["Cd(~/scratch)"]
    for rule in ("Cd(/srv)", "Cd(srv)"):
        assert "Cd(//...)" in _refused_rule(rule)


def test_path_rules_claude_code_never_matches_refuse_naming_the_spelling_to_use():
    assert "as a Edit rule" in _refused_rule("Write(src/x)")
    assert "as a Edit rule" in _refused_rule("MultiEdit(src/x)")
    assert "as a Read rule" in _refused_rule("Grep(src/x)")
    assert "as a Read rule" in _refused_rule("Glob(src/**)")


def test_anchorless_rules_carry_as_written_and_other_specifiers_refuse():
    for rule in (
        "Bash(rm -rf:*)",
        "WebFetch(domain:example.com)",
        "PowerShell(Remove-Item:*)",
        "Agent(Explore)",
        "mcp__server__tool",
        "mcp__server__tool(x)",
        "WebSearch",
        "Read",
        "Write(*)",
    ):
        assert _project(rule) == [rule]
    assert "for WebSearch" in _refused_rule("WebSearch(anything)")


# H3: what is carried, reported and refused


def _settings(tmp_path: Path, document: dict, *, local: bool = False) -> str:
    name = "settings.local.json" if local else "settings.json"
    path = _write(tmp_path / "project" / ".claude" / name, json.dumps(document))
    return str(path)


def _plan(tmp_path: Path, document: dict, **kwargs):
    source = kwargs.pop("source_path", None) or _settings(tmp_path, document)
    kwargs.setdefault("fmt", "claude")
    return plan_carry(
        document,
        source_path=source,
        leg_cwd=str(tmp_path / "leg"),
        home=str(tmp_path / "home"),
        **kwargs,
    )


def test_ask_rules_are_carried_as_deny_and_counted(tmp_path):
    plan = _plan(tmp_path, {"permissions": {"deny": ["Bash(curl:*)"], "ask": ["Bash(git push:*)"]}})
    assert plan.deny_rules == ("Bash(curl:*)", "Bash(git push:*)")
    assert plan.ask_rules_as_deny == 1
    assert "1 ask rules rendered as deny: a leg has no one to ask" in plan.report()


def test_an_ask_rule_the_renderer_refuses_refuses_the_plan(tmp_path):
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, {"permissions": {"ask": [f"Read(!{MARKER})"]}})
    assert caught.value.problems[0][0] == "permissions.ask[0]"
    assert MARKER not in str(caught.value)


def test_a_relative_rule_renders_against_the_project_and_the_leg(tmp_path):
    plan = _plan(tmp_path, {"permissions": {"deny": ["Read(.env)"]}})
    project = str(tmp_path / "project").strip("/")
    leg = str(tmp_path / "leg").strip("/")
    assert plan.deny_rules == (f"Read(//{project}/**/.env)", f"Read(//{leg}/**/.env)")


def test_a_relative_rule_in_a_file_that_is_not_project_settings_refuses(tmp_path):
    document = {"permissions": {"deny": ["Read(.env)"]}}
    elsewhere = _write(tmp_path / "guards.json", json.dumps(document))
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, document, source_path=str(elsewhere))
    assert caught.value.problems == (("permissions.deny[0]", caught.value.problems[0][1]),)
    assert "no known directory" in caught.value.problems[0][1]


def test_a_project_directory_the_rule_syntax_reads_as_a_pattern_refuses_the_plan(tmp_path):
    document = {"permissions": {"deny": ["Read(.env)", "Bash(curl:*)"]}}
    source = _write(tmp_path / "proj[1]" / ".claude" / "settings.json", json.dumps(document))
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, document, source_path=str(source))
    assert [problem[0] for problem in caught.value.problems] == ["permissions.deny[0]"]
    assert caught.value.problems[0][1].startswith("the source project directory holds")
    assert "proj[1]" not in str(caught.value)


def test_a_user_settings_file_anchors_slash_rules_under_its_claude_directory(tmp_path):
    document = {"permissions": {"deny": ["Read(/history.jsonl)"]}}
    user_file = _write(tmp_path / "home" / ".claude" / "settings.json", json.dumps(document))
    plan = _plan(tmp_path, document, source_path=str(user_file))
    assert plan.deny_rules == ("Read(~/.claude/history.jsonl)",)


HOOKS_AND_DENY = {
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard --check"}]}
        ]
    },
    "permissions": {"deny": ["Bash(curl:*)"]},
}


def test_hooks_are_translated_and_tagged_with_their_format(tmp_path):
    plan = _plan(tmp_path, HOOKS_AND_DENY)
    assert plan.hooks == {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": ["guard", "--check"],
                        "source": "imported:claude",
                    }
                ],
            }
        ]
    }
    assert plan.deny_rules == ("Bash(curl:*)",) and plan.left_out == ()


def test_a_hook_timeout_is_carried(tmp_path):
    document = {"hooks": {"PreToolUse": [{"hooks": [{"command": "guard", "timeout": 2.5}]}]}}
    assert _plan(tmp_path, document).hooks == {
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": ["guard"],
                        "source": "imported:claude",
                        "timeout": 2.5,
                    }
                ]
            }
        ]
    }


def test_carrying_only_deny_leaves_the_hooks_out_and_says_so(tmp_path):
    plan = _plan(tmp_path, HOOKS_AND_DENY, carry=["deny"])
    assert plan.hooks == {} and plan.deny_rules == ("Bash(curl:*)",)
    assert plan.left_out == ("hooks",)
    assert "hooks left out at the caller's request" in plan.report()
    plan = _plan(tmp_path, HOOKS_AND_DENY, carry=["hooks"])
    assert plan.deny_rules == () and plan.left_out == ("deny",)


def test_a_left_out_kind_is_not_checked(tmp_path):
    document = {
        "hooks": {"Unknown": []},
        "permissions": {"deny": ["Read(!x)"]},
    }
    assert _plan(tmp_path, document, carry=[]).left_out == ("hooks", "deny")


@pytest.mark.parametrize(
    ("hooks", "where"),
    [
        (
            {"PreToolUse": [{"hooks": [{"type": "command", "command": f"rm -rf ${MARKER}"}]}]},
            "hooks.PreToolUse[0].hooks[0]",
        ),
        (
            {"PostToolUse": [{"hooks": [{"type": "prompt", "prompt": MARKER}]}]},
            "hooks.PostToolUse[0].hooks[0]",
        ),
        ({"SessionStart": [{"hooks": [{"command": []}]}]}, "hooks.SessionStart[0].hooks[0]"),
        (
            {"PreToolUse": [{"hooks": [{"command": "guard", "timeout": MARKER}]}]},
            "hooks.PreToolUse[0].hooks[0]",
        ),
        (
            {"PreToolUse": [{"hooks": [{"command": "guard", "timeout": True}]}]},
            "hooks.PreToolUse[0].hooks[0]",
        ),
        (
            {"PreToolUse": [{"hooks": [{"command": "guard", "timeout": 0}]}]},
            "hooks.PreToolUse[0].hooks[0]",
        ),
        ({"Stop": [{"hooks": [{"command": MARKER}]}]}, "hooks.Stop"),
        ({f"{MARKER} event": []}, "hooks key 0"),
    ],
)
def test_a_hook_entry_that_cannot_be_carried_refuses_naming_where_and_no_content(
    tmp_path, hooks, where
):
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, {"hooks": hooks})
    assert [problem[0] for problem in caught.value.problems] == [where]
    assert MARKER not in str(caught.value)


def test_every_refused_guard_is_listed(tmp_path):
    document = {
        "hooks": {"Stop": []},
        "permissions": {"deny": ["Grep(x)", "Bash(ok)"], "ask": "not a list"},
    }
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, document)
    assert [problem[0] for problem in caught.value.problems] == [
        "hooks.Stop",
        "permissions.deny[0]",
        "permissions.ask",
    ]


def test_keys_that_are_not_guards_are_dropped_named_only_when_known(tmp_path):
    document = {
        "model": "x",
        "env": {"TOKEN": MARKER},
        "sandbox": {"enabled": False},
        "permissions": {"allow": ["Bash(*)"], "deny": ["Bash(curl:*)"], MARKER: 1},
        f"{MARKER}-key": 1,
    }
    plan = _plan(tmp_path, document)
    assert plan.deny_rules == ("Bash(curl:*)",)
    assert plan.dropped_keys == ("env", "model", "permissions.allow", "sandbox")
    assert plan.dropped_other == 2
    assert not any(MARKER in line for line in plan.report())


def test_a_codex_file_carries_hooks_and_drops_permissions(tmp_path):
    document = {
        "hooks": {"PreToolUse": [{"hooks": [{"command": ["guard", "--codex"]}]}]},
        "permissions": {"deny": ["Bash(curl:*)"]},
    }
    source = _write(tmp_path / "hooks.json", json.dumps(document))
    plan = _plan(tmp_path, document, fmt="codex", source_path=str(source))
    assert plan.hooks["PreToolUse"][0]["hooks"][0]["source"] == "imported:codex"
    assert plan.deny_rules == () and plan.dropped_keys == ("permissions",)


def test_an_unknown_format_or_carry_kind_is_refused(tmp_path):
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, {}, fmt="gemini")
    assert caught.value.rule == "format"
    with pytest.raises(HooksFromError) as caught:
        _plan(tmp_path, {}, carry=["allow"])
    assert caught.value.rule == "carry"

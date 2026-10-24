# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Play launches: typed playbook args and the prompt positional in argv,
plus the Operator's placeholder guard."""

from __future__ import annotations

import pytest

from lionagi.studio.scheduler.subprocess import (
    _validate_playbook_args,
    build_argv,
    validate_launch_fields,
)


def _play_schedule(**overrides):
    schedule = {
        "action_kind": "play",
        "action_model": "",
        "action_prompt": "",
        "action_agent": None,
        "action_playbook": "audit",
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
    }
    schedule.update(overrides)
    return schedule


class TestBuildArgvPlay:
    def test_bare_play(self):
        argv, tmp = build_argv(_play_schedule(), {}, executable_prefix=["li"])
        assert argv == ["li", "play", "audit"]
        assert tmp is None

    def test_prompt_becomes_positional(self):
        argv, _ = build_argv(
            _play_schedule(action_prompt="review the parser"), {}, executable_prefix=["li"]
        )
        assert argv == ["li", "play", "audit", "review the parser"]

    def test_args_become_schema_flags_before_prompt(self):
        argv, _ = build_argv(
            _play_schedule(
                action_prompt="target text",
                action_playbook_args={"mode": "deep", "max_workers": 4},
            ),
            {},
            executable_prefix=["li"],
        )
        assert argv[:3] == ["li", "play", "audit"]
        assert argv[-1] == "target text"
        middle = argv[3:-1]
        assert ["--mode", "deep"] == middle[middle.index("--mode") : middle.index("--mode") + 2]
        # Underscores in arg names map to dashes, matching the play CLI's own
        # schema-derived flags.
        assert ["--max-workers", "4"] == (
            middle[middle.index("--max-workers") : middle.index("--max-workers") + 2]
        )

    def test_bool_arg_is_bare_flag_when_true_absent_when_false(self):
        argv_true, _ = build_argv(
            _play_schedule(action_playbook_args={"strict": True}),
            {},
            executable_prefix=["li"],
        )
        assert argv_true == ["li", "play", "audit", "--strict"]
        argv_false, _ = build_argv(
            _play_schedule(action_playbook_args={"strict": False}),
            {},
            executable_prefix=["li"],
        )
        assert argv_false == ["li", "play", "audit"]

    def test_leading_dash_prompt_rejected(self):
        with pytest.raises(ValueError, match="must not start with '-'"):
            build_argv(_play_schedule(action_prompt="--bypass"), {}, executable_prefix=["li"])

    def test_leading_dash_refusal_does_not_carry_the_rendered_value(self):
        # The refusal is stored on the run and served, and the value can come from an event.
        schedule = _play_schedule(action_prompt="{{pr_title}}")
        with pytest.raises(ValueError, match="must not start with '-'") as refused:
            build_argv(schedule, {"pr_title": "--private-title"}, executable_prefix=["li"])
        assert "private-title" not in str(refused.value)

    @pytest.mark.parametrize("kind", ["play", "playbook"])
    def test_a_stored_leading_dash_prompt_is_refused_before_rendering(self, kind):
        # A manual trigger runs this check before it records a run.
        with pytest.raises(ValueError, match="must not start with '-'"):
            validate_launch_fields(_play_schedule(action_kind=kind, action_prompt="--bypass"))

    @pytest.mark.parametrize("kind", ["play", "playbook"])
    @pytest.mark.parametrize("name", ["list", "check", "status"])
    def test_a_playbook_named_for_a_play_subcommand_is_refused(self, kind, name):
        # `li play list` runs the listing and exits 0; the playbook would never run.
        with pytest.raises(ValueError, match="names a `li play` subcommand"):
            validate_launch_fields(_play_schedule(action_kind=kind, action_playbook=name))

    def test_the_refused_names_are_the_subcommands_li_play_reads(self):
        import ast
        import importlib
        import inspect

        from lionagi.studio.scheduler.subprocess import _PLAY_SUBCOMMANDS

        # The module, not the `main` function the package exports under the same name.
        cli_main = importlib.import_module("lionagi.cli.main")

        (shortcut,) = [
            node
            for node in ast.walk(ast.parse(inspect.getsource(cli_main)))
            if isinstance(node, ast.FunctionDef) and node.name == "_handle_play_shortcut"
        ]
        read = {
            comparator.value
            for node in ast.walk(shortcut)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "head"
            and isinstance(node.ops[0], ast.Eq)
            for comparator in node.comparators
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
        }
        # Flags (--resume) are refused by the identifier check already.
        assert {name for name in read if not name.startswith("-")} == _PLAY_SUBCOMMANDS

    @pytest.mark.parametrize("playbook", [None, ""])
    def test_a_leading_dash_prompt_is_kept_when_no_playbook_sends_it(self, playbook):
        schedule = _play_schedule(action_playbook=playbook, action_prompt="--bypass")
        assert validate_launch_fields(schedule) == "play"

    @pytest.mark.parametrize(
        "bad_args",
        [
            {"--mode": "deep"},
            {"mode": "-deep"},
            {"mode": ["deep"]},
            {"bad name": "x"},
        ],
    )
    def test_invalid_args_rejected(self, bad_args):
        with pytest.raises(ValueError):
            build_argv(_play_schedule(action_playbook_args=bad_args), {}, executable_prefix=["li"])

    def test_validator_direct(self):
        _validate_playbook_args({"mode": "deep", "strict": True, "n": 3})
        with pytest.raises(ValueError):
            _validate_playbook_args(["mode"])

    def test_a_scheduled_row_reads_the_args_its_target_declares(self):
        """A scheduled row carries its play args in the resolved target snapshot,
        in place of the one-shot args field; the launch must read them there."""
        argv, _ = build_argv(
            _play_schedule(
                resolved_target={
                    "target": {"kind": "playbook", "name": "audit", "args": {"depth": 2}}
                }
            ),
            {},
            executable_prefix=["li"],
        )
        assert argv == ["li", "play", "audit", "--depth", "2"]

    @pytest.mark.parametrize("bad_args", [[], "", 0, False])
    def test_empty_values_of_the_wrong_shape_are_refused_too(self, bad_args):
        """An empty list, string, zero or false is a stored value of the wrong
        shape, not the absence of args, on either path."""
        with pytest.raises(ValueError, match="must be an object"):
            build_argv(_play_schedule(action_playbook_args=bad_args), {}, executable_prefix=["li"])
        with pytest.raises(ValueError, match="must be an object"):
            build_argv(
                _play_schedule(
                    resolved_target={
                        "target": {"kind": "playbook", "name": "audit", "args": bad_args}
                    }
                ),
                {},
                executable_prefix=["li"],
            )

    def test_an_empty_object_reads_the_target_snapshot(self):
        argv, _ = build_argv(
            _play_schedule(
                action_playbook_args={},
                resolved_target={
                    "target": {"kind": "playbook", "name": "audit", "args": {"depth": 2}}
                },
            ),
            {},
            executable_prefix=["li"],
        )
        assert argv == ["li", "play", "audit", "--depth", "2"]

    def test_args_of_the_wrong_shape_are_refused_rather_than_dropped(self):
        """A stored args value that is not an object is refused at launch; it is
        never read as no args, which would run the play with its defaults."""
        with pytest.raises(ValueError):
            build_argv(_play_schedule(action_playbook_args=["mode"]), {}, executable_prefix=["li"])
        with pytest.raises(ValueError):
            build_argv(
                _play_schedule(
                    resolved_target={
                        "target": {"kind": "playbook", "name": "audit", "args": ["mode"]}
                    }
                ),
                {},
                executable_prefix=["li"],
            )

    def test_a_scheduled_row_with_no_declared_args_still_builds_a_bare_play(self):
        argv, _ = build_argv(
            _play_schedule(resolved_target={"target": {"kind": "playbook", "name": "audit"}}),
            {},
            executable_prefix=["li"],
        )
        assert argv == ["li", "play", "audit"]


class TestPlaceholderGuard:
    @pytest.fixture()
    def playbook(self, monkeypatch):
        """Point the guard's playbook lookup at a synthetic parameterized playbook."""
        from lionagi.studio.services import playbooks as pb

        detail = {
            "name": "audit",
            "data": {
                "prompt": "Audit {input} in {mode} mode with {workers} workers.",
                "args": {
                    "mode": {"type": "str", "default": "quick"},
                    "workers": {"type": "int", "default": 2},
                },
            },
        }
        monkeypatch.setattr(pb, "get_playbook", lambda name: detail)
        return detail

    def _problems(self, provided_input="", provided_args=None):
        from lionagi.studio.operator.application_mcp import (
            _playbook_placeholder_problems,
        )

        return _playbook_placeholder_problems("audit", provided_input, provided_args or {})

    def test_input_plus_declared_args_pass(self, playbook):
        assert self._problems("the parser", {"mode": "deep", "workers": 4}) is None

    def test_declared_args_cover_even_when_omitted(self, playbook):
        # mode/workers have defaults — only input needs providing.
        assert self._problems("the parser") is None

    def test_missing_input_refused_with_reason(self, playbook):
        reason = self._problems("")
        assert reason is not None
        assert "input" in reason

    def test_undeclared_arg_refused(self, playbook):
        reason = self._problems("the parser", {"nope": "x"})
        assert reason is not None
        assert "nope" in reason

    def test_placeholder_free_playbook_passes_without_input(self, monkeypatch):
        from lionagi.studio.services import playbooks as pb

        monkeypatch.setattr(
            pb,
            "get_playbook",
            lambda name: {"name": name, "data": {"prompt": "Fixed task, no params."}},
        )
        assert self._problems("") is None

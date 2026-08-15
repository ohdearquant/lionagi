# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Play launches: typed playbook args and the prompt positional in argv,
plus the Operator's placeholder guard."""

from __future__ import annotations

import pytest

from lionagi.studio.scheduler.subprocess import (
    _validate_playbook_args,
    build_argv,
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

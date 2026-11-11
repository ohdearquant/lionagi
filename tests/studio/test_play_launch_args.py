# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Play launches: typed playbook args and the prompt positional in argv,
plus the Operator's placeholder guard."""

from __future__ import annotations

import pytest
from lionhub_tests.orchestration.schedule._test_support_play_launch_args import (
    _play_schedule,
)

from lionhub.orchestration.schedule.action_validation import (
    _validate_playbook_args,
    validate_launch_fields,
)
from lionhub.orchestration.scheduler.subprocess import build_argv


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

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Play launches: typed playbook args and the prompt positional in argv,
plus the Operator's placeholder guard."""

from __future__ import annotations

import os
import shlex

import pytest

from lionhub.orchestration.schedule.action_validation import (
    _validate_playbook_args,
    validate_launch_fields,
)

from ._test_support_command_action_kind import (
    ALLOWLIST_ENV,
)
from ._test_support_play_launch_args import (
    _play_schedule,
)


class TestBuildArgvPlay:
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

    def test_validator_direct(self):
        _validate_playbook_args({"mode": "deep", "strict": True, "n": 3})
        with pytest.raises(ValueError):
            _validate_playbook_args(["mode"])


class TestCommandAllowlist:
    def test_refusal_names_whose_environment_it_read_and_not_the_document(self, monkeypatch):
        """A refusal identifies the environment that supplied the value.

        Schedule creation and the later spawn check can run in separate
        processes, so the message needs to identify the configuration it read.
        """
        from lionhub.orchestration.schedule.action_validation import _validate_command_allowlisted

        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
        with pytest.raises(ValueError) as unset:
            _validate_command_allowlisted("uv")
        message = str(unset.value)
        assert "is not set in the environment of this process" in message
        assert f"(pid {os.getpid()})" in message
        assert "not of the document being checked" in message
        assert "checks again at spawn time" in message
        assert "scheduler daemon's environment" in message
        assert "restart the scheduler daemon" in message
        assert "<your command>" not in message

    @pytest.mark.parametrize("allowlisted_name", ["tool with space", "tool'quote", "tool$dollar"])
    def test_refusal_prints_a_shell_safe_allowlist_value(self, monkeypatch, allowlisted_name):
        from lionhub.orchestration.schedule.action_validation import _validate_command_allowlisted

        monkeypatch.setenv(ALLOWLIST_ENV, allowlisted_name)
        with pytest.raises(ValueError) as caught:
            _validate_command_allowlisted("kdev")

        permitted = ",".join(sorted({allowlisted_name, "kdev"}))
        assert str(caught.value).endswith(f"{ALLOWLIST_ENV}={shlex.quote(permitted)}")

    def test_refusal_separates_an_unset_variable_from_an_empty_one(self, monkeypatch):
        """`(currently: [])` is what BOTH an unset and a deliberately empty
        variable produced, and the two have different causes: one is a shell
        that never exported it, the other is a policy that permits nothing.
        A reader cannot act on the first without being told which it is.
        """
        from lionhub.orchestration.schedule.action_validation import _validate_command_allowlisted

        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
        with pytest.raises(ValueError) as unset:
            _validate_command_allowlisted("uv")
        monkeypatch.setenv(ALLOWLIST_ENV, " , ")
        with pytest.raises(ValueError) as empty:
            _validate_command_allowlisted("uv")
        assert "is not set in" in str(unset.value)
        assert "naming no command" in str(empty.value) and "' , '" in str(empty.value)
        assert str(unset.value) != str(empty.value)

    def test_refusal_preserves_existing_allow_list_entries(self, monkeypatch):
        """The suggested daemon value keeps existing entries and adds the refused command."""
        from lionhub.orchestration.schedule.action_validation import _validate_command_allowlisted

        monkeypatch.setenv(ALLOWLIST_ENV, "other-tool,kdev")
        with pytest.raises(ValueError) as exc:
            _validate_command_allowlisted("uv")
        message = str(exc.value)
        assert message.endswith(f"{ALLOWLIST_ENV}=kdev,other-tool,uv")
        assert "['kdev', 'other-tool']" in message

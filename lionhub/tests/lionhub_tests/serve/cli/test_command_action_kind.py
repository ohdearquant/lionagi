# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the 'command' action kind: allow-listed executable
spawned directly with templated argv, distinct from the `li`-invoking kinds."""

from __future__ import annotations

import asyncio
import os
import shlex
from unittest.mock import AsyncMock, patch

import pytest

from ...orchestration.schedule._test_support_command_action_kind import (
    ALLOWLIST_ENV,
)


def _schedule(**kwargs) -> dict:
    base = {
        "id": "sched-cmd",
        "name": "command-test",
        "trigger_type": "cron",
        "action_kind": "command",
        "action_command": "kdev",
        "action_command_args": ["review-pr"],
    }
    base.update(kwargs)
    return base


# subprocess.build_argv — basic argv shape


# subprocess.build_argv — templated-argv rendering


# subprocess.build_argv — leading-'-' injection rejection on a rendered arg


# subprocess — action_command allow-list gate


# services.schedules — build/validation-time refusal


class TestCreateScheduleCommandValidation:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_create_command_without_action_command_raises(self):
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-command-missing",
            "trigger_type": "cron",
            "cron_expr": "0 * * * *",
            "action_kind": "command",
        }
        with pytest.raises(ValueError, match="action_command is required"):
            self._run(create_schedule(data))

    def test_create_command_not_allowlisted_raises(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "other-tool")
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-command-not-allowed",
            "trigger_type": "cron",
            "action_kind": "command",
            "action_command": "kdev",
        }
        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            self._run(create_schedule(data))

    def test_create_command_path_separator_raises(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-command-path",
            "trigger_type": "cron",
            "action_kind": "command",
            "action_command": "./kdev",
        }
        with pytest.raises(ValueError, match="path separators"):
            self._run(create_schedule(data))

    def test_create_command_args_not_a_list_raises(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-command-args-type",
            "trigger_type": "cron",
            "action_kind": "command",
            "action_command": "kdev",
            "action_command_args": "not-a-list",
        }
        with pytest.raises(ValueError, match="action_command_args must be a list"):
            self._run(create_schedule(data))

    def test_create_valid_command_does_not_raise(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            data = {
                "name": "good-command",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "command",
                "action_command": "kdev",
                "action_command_args": ["review-pr", "--repo", "{{repo}}"],
            }
            result = self._run(
                __import__(
                    "lionagi.studio.services.schedules", fromlist=["create_schedule"]
                ).create_schedule(data)
            )
        assert "id" in result


class TestUpdateScheduleCommandValidation:
    def _mock_db(self, existing: dict):
        class _MockDB:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def get_schedule(self_inner, sid):
                return existing

            async def update_schedule(self_inner, sid, **kw):
                pass

        return _MockDB()

    def _run_update(self, existing: dict, fields: dict):
        from lionagi.studio.services.schedules import update_schedule

        async def _go():
            with patch(
                "lionagi.studio.services.schedules.StateDB",
                return_value=self._mock_db(existing),
            ):
                return await update_schedule(existing["id"], fields)

        return asyncio.run(_go())

    def _existing(self, **over) -> dict:
        base = {
            "id": "sid-cmd",
            "name": "cmd-patch-test",
            "trigger_type": "cron",
            "action_kind": "command",
            "action_command": "kdev",
            "action_command_args": [],
        }
        base.update(over)
        return base

    def test_patch_command_not_allowlisted_raises(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            self._run_update(self._existing(), {"action_command": "evil-tool"})

    def test_patch_valid_command_does_not_raise(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        result = self._run_update(self._existing(), {"action_command": "kdev"})
        assert result is True

    def test_patch_clearing_action_kind_to_command_requires_command(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        existing = self._existing(action_kind="agent", action_command=None)
        with pytest.raises(ValueError, match="action_command is required"):
            self._run_update(existing, {"action_kind": "command"})


# CLI — --action-kind accepts 'command'


class TestCliActionKindChoices:
    def test_action_kind_choices_include_command(self):
        import argparse

        from lionhub.orchestration.schedule.parsing import add_schedule_subparser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_schedule_subparser(sub)
        args = parser.parse_args(
            [
                "schedule",
                "create",
                "cli-command-test",
                "--cron",
                "0 * * * *",
                "--action-kind",
                "command",
                "--action-command",
                "kdev",
                "--action-command-args",
                '["review-pr"]',
            ]
        )
        assert args.action_kind == "command"
        assert args.action_command == "kdev"
        # The flag declares its shape as its argparse type, so the parser hands
        # back the decoded list rather than the JSON text it arrived as.
        assert args.action_command_args == ["review-pr"]


# worker.default_execute — the worker/task-application execution path


# CLI — _cmd_create's --action-command-args JSON handling. TestCliActionKindChoices
# above only pins argparse's raw string capture; the JSON-parse/shape error
# paths inside _cmd_create itself (cli.py lines ~839-848) were untested.


class TestCmdCreateActionCommandArgsJSON:
    def _parse(self, argv: list[str]):
        import argparse

        from lionhub.orchestration.schedule.parsing import add_schedule_subparser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_schedule_subparser(sub)
        return parser.parse_args(argv)

    def _argv(self, action_command_args_json: str) -> list[str]:
        return [
            "schedule",
            "create",
            "cli-command-json-test",
            "--cron",
            "0 * * * *",
            "--action-kind",
            "command",
            "--action-command",
            "kdev",
            "--action-command-args",
            action_command_args_json,
        ]

    def test_invalid_json_rejected_before_api_call(self, capsys):
        """Malformed JSON in --action-command-args is refused while parsing, so
        no handler runs and nothing can reach the HTTP layer.

        The shape is declared as the argument's type, which means the parser
        rejects a bad value itself and exits 2 the way it does for every other
        malformed argument, rather than a handler decoding the string later and
        returning 1."""
        with patch("lionhub.serve.cli.schedule._api") as mock_api:
            with pytest.raises(SystemExit) as excinfo:
                self._parse(self._argv("{not valid json"))

        assert excinfo.value.code == 2
        mock_api.assert_not_called()
        err = capsys.readouterr().err
        assert "--action-command-args" in err
        assert "must be valid JSON" in err

    def test_valid_json_non_array_rejected_before_api_call(self, capsys):
        """Valid JSON that parses to a non-array (e.g. an object) is refused
        while parsing, so no handler runs and nothing reaches the HTTP layer."""
        with patch("lionhub.serve.cli.schedule._api") as mock_api:
            with pytest.raises(SystemExit) as excinfo:
                self._parse(self._argv('{"not": "an array"}'))

        assert excinfo.value.code == 2
        mock_api.assert_not_called()
        err = capsys.readouterr().err
        assert "--action-command-args" in err
        assert "must be a JSON array" in err

    def test_valid_json_array_parsed_and_forwarded(self):
        """A well-formed JSON array must be parsed and forwarded as the
        action_command_args field of the POST body."""
        from lionhub.serve.cli.schedule import _cmd_create

        args = self._parse(self._argv('["review-pr", "--repo", "{{repo}}"]'))
        with patch(
            "lionhub.serve.cli.schedule._api",
            return_value={"id": "sid1", "name": "cli-command-json-test"},
        ) as mock_api:
            rc = _cmd_create(args)

        assert rc == 0
        mock_api.assert_called_once()
        body = mock_api.call_args.kwargs["body"]
        assert body["action_command_args"] == ["review-pr", "--repo", "{{repo}}"]
        assert body["action_command"] == "kdev"

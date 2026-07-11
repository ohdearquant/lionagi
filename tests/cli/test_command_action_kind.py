# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the 'command' action kind: allow-listed executable
spawned directly with templated argv, distinct from the `li`-invoking kinds."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

ALLOWLIST_ENV = "LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"


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


# ---------------------------------------------------------------------------
# subprocess.build_argv — basic argv shape
# ---------------------------------------------------------------------------


class TestBuildArgvCommandShape:
    def test_command_argv_bypasses_li_prefix(self, monkeypatch):
        """argv for kind='command' starts with the executable itself, never
        the `uv run li` prefix used by every other kind."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        argv, tmp = build_argv(_schedule(), {})
        assert tmp is None
        assert argv[0] == "kdev"
        assert "uv" not in argv
        assert "li" not in argv

    def test_command_argv_with_literal_and_templated_args(self, monkeypatch):
        """A literal flag token (e.g. '--repo') passes through unchanged;
        a {{var}} template renders from trigger_context."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(
            action_command_args=[
                "review-pr",
                "--repo",
                "{{repo}}",
                "--pr",
                "{{pr_number}}",
            ]
        )
        argv, tmp = build_argv(sched, {"repo": "ohdearquant/lionagi", "pr_number": 1234})
        assert tmp is None
        assert argv == [
            "kdev",
            "review-pr",
            "--repo",
            "ohdearquant/lionagi",
            "--pr",
            "1234",
        ]

    def test_executable_prefix_kwarg_ignored_for_command_kind(self, monkeypatch):
        """An explicit executable_prefix (as engine.py/worker.py pass for the
        `li`-invoking kinds) must not leak into a command-kind argv."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        argv, tmp = build_argv(_schedule(), {}, executable_prefix=["/abs/path/to/li"])
        assert tmp is None
        assert argv[0] == "kdev"
        assert "/abs/path/to/li" not in argv


# ---------------------------------------------------------------------------
# subprocess.build_argv — templated-argv rendering
# ---------------------------------------------------------------------------


class TestBuildArgvCommandTemplateRendering:
    def test_bare_template_renders_from_trigger_context(self, monkeypatch):
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_command_args=["{{pr_number}}"])
        argv, _ = build_argv(sched, {"pr_number": 42})
        assert argv == ["kdev", "42"]

    def test_unresolved_template_left_as_literal_braces(self, monkeypatch):
        """A template whose key is absent from trigger_context renders back
        to the original '{{var}}' text (existing _render_template fallback),
        which then fails the post-render charset guard."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_command_args=["{{missing_key}}"])
        with pytest.raises(ValueError, match="characters not allowed"):
            build_argv(sched, {})

    def test_mixed_literal_and_template_in_one_token(self, monkeypatch):
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_command_args=["pr-{{pr_number}}"])
        argv, _ = build_argv(sched, {"pr_number": 7})
        assert argv == ["kdev", "pr-7"]


# ---------------------------------------------------------------------------
# subprocess.build_argv — leading-'-' injection rejection on a rendered arg
# ---------------------------------------------------------------------------


class TestBuildArgvCommandArgInjection:
    def test_literal_leading_dash_token_accepted(self, monkeypatch):
        """A hand-authored literal flag (no template) is author-controlled
        and must NOT be rejected -- this is the whole point of a generic
        command runner (e.g. legitimate flags like '--repo', '--pr')."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_command_args=["--repo", "--pr", "-v"])
        argv, _ = build_argv(sched, {})
        assert argv == ["kdev", "--repo", "--pr", "-v"]

    def test_rendered_template_starting_with_dash_rejected(self, monkeypatch):
        """A template whose SUBSTITUTED value starts with '-' would let
        trigger_context content masquerade as a new flag; must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_command_args=["--pr", "{{pr_number}}"])
        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(sched, {"pr_number": "--rm-rf"})

    def test_rendered_template_bad_charset_rejected(self, monkeypatch):
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_command_args=["{{payload}}"])
        with pytest.raises(ValueError, match="characters not allowed"):
            build_argv(sched, {"payload": "safe; rm -rf /"})

    def test_command_extra_args_rejected(self, monkeypatch):
        """action_extra_args is not the mechanism for command argv -- reject
        loudly rather than silently dropping it or double-appending."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        sched = _schedule(action_extra_args=["stray"])
        with pytest.raises(ValueError, match="action_extra_args"):
            build_argv(sched, {})


# ---------------------------------------------------------------------------
# subprocess — action_command allow-list gate
# ---------------------------------------------------------------------------


class TestCommandAllowlist:
    def test_non_allowlisted_command_refused(self, monkeypatch):
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "other-tool")
        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            build_argv(_schedule(action_command="kdev"), {})

    def test_empty_allowlist_refuses_everything(self, monkeypatch):
        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            build_argv(_schedule(), {})

    def test_blank_allowlist_string_refuses_everything(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "")
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            build_argv(_schedule(), {})

    def test_allowlisted_command_builds_exact_argv(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev,other-tool")
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(), {})
        assert tmp is None
        assert argv == ["kdev", "review-pr"]

    def test_allowlist_membership_exact_not_substring(self, monkeypatch):
        """'kdevx' on the allow-list must not permit spawning 'kdev'."""
        monkeypatch.setenv(ALLOWLIST_ENV, "kdevx")
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            build_argv(_schedule(action_command="kdev"), {})

    def test_spawn_time_recheck_refuses_when_allowlist_changed(self, monkeypatch):
        """Allow-listed at build_argv call #1, removed from the allow-list
        before call #2 -- the second (spawn-time) call must re-observe the
        environment and refuse, since build_argv never caches the allow-list."""
        from lionagi.studio.scheduler.subprocess import build_argv

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        argv, _ = build_argv(_schedule(), {})
        assert argv[0] == "kdev"

        monkeypatch.setenv(ALLOWLIST_ENV, "some-other-tool")
        with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
            build_argv(_schedule(), {})

    def test_spawn_and_wait_rechecks_allowlist_at_actual_exec_boundary(self, monkeypatch):
        """Regression: build_argv's re-check happens well before the process
        is actually spawned -- callers (scheduler engine, worker, on-demand
        launches) run awaited DB work between building argv and calling
        spawn_and_wait, and an await is a scheduling point. Revoking the
        allow-list during that window must still stop the spawn: build argv
        while allow-listed, revoke, then call spawn_and_wait directly and
        assert asyncio.create_subprocess_exec is never invoked and a
        ValueError propagates."""
        from lionagi.studio.scheduler.subprocess import build_argv, spawn_and_wait

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        argv, tmp_path = build_argv(_schedule(), {})
        assert argv[0] == "kdev"

        monkeypatch.delenv(ALLOWLIST_ENV, raising=False)

        exec_mock = AsyncMock(side_effect=AssertionError("subprocess must not spawn"))
        with patch("asyncio.create_subprocess_exec", exec_mock):
            with pytest.raises(ValueError, match="not in LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"):
                asyncio.run(
                    spawn_and_wait(argv, "inv-recheck", tmp_path=tmp_path, action_kind="command")
                )
        exec_mock.assert_not_called()

    def test_missing_action_command_raises(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_command is required"):
            build_argv(_schedule(action_command=""), {})

    def test_action_command_with_path_separator_rejected(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "bin/kdev")
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="path separators"):
            build_argv(_schedule(action_command="bin/kdev"), {})

    def test_action_command_leading_dash_rejected(self, monkeypatch):
        monkeypatch.setenv(ALLOWLIST_ENV, "--kdev")
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_command="--kdev"), {})


# ---------------------------------------------------------------------------
# services.schedules — build/validation-time refusal
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CLI — --action-kind accepts 'command'
# ---------------------------------------------------------------------------


class TestCliActionKindChoices:
    def test_action_kind_choices_include_command(self):
        import argparse

        from lionagi.studio.cli import add_schedule_subparser

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
        assert args.action_command_args == '["review-pr"]'


# ---------------------------------------------------------------------------
# worker.default_execute — the worker/task-application execution path
# ---------------------------------------------------------------------------


class TestWorkerDefaultExecuteCommandKind:
    def test_skips_li_resolution_for_command_kind(self, monkeypatch):
        """default_execute (worker.py's own build_argv caller, independent
        of engine.py) must also skip resolve_li_executable() for kind='command'."""
        from lionagi.studio.scheduler import worker

        monkeypatch.setenv(ALLOWLIST_ENV, "kdev")
        row = {
            "action_kind": "command",
            "action_args": {"action_command": "kdev", "action_command_args": ["review-pr"]},
        }

        async def _go():
            with (
                patch.object(
                    worker._subprocess,
                    "resolve_li_executable",
                    return_value=(None, "must not be called for kind='command'"),
                ) as resolve_mock,
                patch.object(
                    worker._subprocess, "spawn_and_wait", new=AsyncMock(return_value=(0, ""))
                ) as spawn_mock,
            ):
                exit_code, _stderr = await worker.default_execute(row)
            resolve_mock.assert_not_called()
            spawn_mock.assert_awaited_once()
            argv = spawn_mock.await_args.args[0]
            assert argv == ["kdev", "review-pr"]
            return exit_code

        assert asyncio.run(_go()) == 0

    def test_still_resolves_li_for_agent_kind(self, monkeypatch):
        """Non-command kinds are unaffected: resolve_li_executable() is still
        required and a failure still short-circuits with a clean error."""
        from lionagi.studio.scheduler import worker

        row = {
            "action_kind": "agent",
            "action_args": {"action_model": "sonnet", "action_prompt": "hi"},
        }

        async def _go():
            with patch.object(
                worker._subprocess,
                "resolve_li_executable",
                return_value=(None, "no li on PATH"),
            ) as resolve_mock:
                exit_code, stderr = await worker.default_execute(row)
            resolve_mock.assert_called_once()
            return exit_code, stderr

        exit_code, stderr = asyncio.run(_go())
        assert exit_code == 1
        assert "cannot resolve li executable" in stderr

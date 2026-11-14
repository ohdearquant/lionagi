# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for argument injection hardening in build_argv (CWE-88): model, extra-args, identifiers, sentinel."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

# Helpers


def _schedule(**kwargs) -> dict:
    base = {
        "id": "sched-inj",
        "name": "injection-test",
        "trigger_type": "cron",
        "action_kind": "agent",
        "action_model": "sonnet",
        "action_prompt": "hello world",
        "action_agent": None,
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
    }
    base.update(kwargs)
    return base


# subprocess.build_argv — action_model validation


# subprocess.build_argv — action_extra_args validation


# subprocess.build_argv — identifier fields (action_agent/project/playbook)


# build_argv structural: -- sentinel and positional ordering


# service layer — create_schedule validation


class TestCreateScheduleInjectionRejection:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_create_with_model_flag_raises_value_error(self):
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-model",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "--bypass",
            "action_prompt": "hello",
        }

        with pytest.raises(ValueError, match="starts with '-'"):
            self._run(create_schedule(data))

    def test_create_with_yolo_model_raises(self):
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-yolo",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "--yolo",
        }

        with pytest.raises(ValueError, match="starts with '-'"):
            self._run(create_schedule(data))

    def test_create_with_extra_args_flag_raises_value_error(self):
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-extra",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_extra_args": ["--bypass"],
        }

        with pytest.raises(ValueError, match="starts with '-'"):
            self._run(create_schedule(data))

    def test_create_with_agent_flag_raises(self):
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-agent",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_agent": "--bypass",
        }

        with pytest.raises(ValueError, match="action_agent"):
            self._run(create_schedule(data))

    def test_create_with_project_flag_raises(self):
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-proj",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_project": "--yolo",
        }

        with pytest.raises(ValueError, match="action_project"):
            self._run(create_schedule(data))

    def test_create_valid_model_and_extra_does_not_raise(self):
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            data = {
                "name": "good-sched",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "agent",
                "action_model": "claude-sonnet-4-6",
                "action_extra_args": ["my-task"],
            }
            result = self._run(
                __import__(
                    "lionagi.studio.services.schedules", fromlist=["create_schedule"]
                ).create_schedule(data)
            )
        assert "id" in result


# service layer — update_schedule (PATCH) validation


class TestUpdateScheduleInjectionRejection:
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
            "id": "sid-patch",
            "name": "patch-test",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_extra_args": [],
        }
        base.update(over)
        return base

    def test_patch_model_flag_raises(self):
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_model": "--bypass"})

    def test_patch_model_yolo_raises(self):
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_model": "--yolo"})

    def test_patch_extra_args_flag_raises(self):
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_extra_args": ["--bypass"]})

    def test_patch_extra_args_yolo_raises(self):
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_extra_args": ["--yolo"]})

    def test_patch_agent_flag_raises(self):
        with pytest.raises(ValueError, match="action_agent"):
            self._run_update(self._existing(), {"action_agent": "--bypass"})

    def test_patch_project_flag_raises(self):
        with pytest.raises(ValueError, match="action_project"):
            self._run_update(self._existing(), {"action_project": "--yolo"})

    def test_patch_valid_fields_does_not_raise(self):
        result = self._run_update(
            self._existing(),
            {"action_model": "gpt-4", "action_extra_args": ["my-task"]},
        )
        assert result is True

    def test_patch_db_write_not_called_on_rejection(self):
        """When validation fails the DB write must not be reached."""
        from lionagi.studio.services.schedules import update_schedule

        write_called = {"value": False}

        class _MockDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get_schedule(self, sid):
                return {
                    "id": "sid-x",
                    "name": "x",
                    "trigger_type": "cron",
                    "action_kind": "agent",
                    "action_model": "sonnet",
                    "action_extra_args": [],
                }

            async def update_schedule(self, sid, **kw):
                write_called["value"] = True

        async def _go():
            with patch(
                "lionagi.studio.services.schedules.StateDB",
                return_value=_MockDB(),
            ):
                await update_schedule("sid-x", {"action_model": "--bypass"})

        with pytest.raises(ValueError):
            asyncio.run(_go())

        assert not write_called["value"], "DB write must not be called when validation rejects"


# action_prompt == '--' sentinel rejection


class TestBuildArgvPromptSentinelRejection:
    """build_argv must reject action_prompt exactly equal to '--'.

    The literal end-of-options token '--' is consumed by argparse as a separator
    rather than reaching the runner as prompt text.  Any other prompt content —
    including '--bypass', '--verbose', '-- --', '-- trailing' — is permitted.
    """

    def test_service_create_prompt_double_dash_raises(self):
        """create_schedule rejects action_prompt == '--'."""
        from lionagi.studio.services.schedules import create_schedule

        data = {
            "name": "bad-prompt-sentinel",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_prompt": "--",
        }
        with pytest.raises(ValueError, match="'--'"):
            asyncio.run(create_schedule(data))

    def test_service_update_prompt_double_dash_raises(self):
        """update_schedule rejects action_prompt == '--' in PATCH."""
        from unittest.mock import AsyncMock, patch

        from lionagi.studio.services.schedules import update_schedule

        existing = {
            "id": "sid-p",
            "name": "p",
            "trigger_type": "cron",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_extra_args": [],
        }

        class _MockDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get_schedule(self, sid):
                return existing

            async def update_schedule(self, sid, **kw):
                pass

        async def _go():
            with patch(
                "lionagi.studio.services.schedules.StateDB",
                return_value=_MockDB(),
            ):
                await update_schedule("sid-p", {"action_prompt": "--"})

        with pytest.raises(ValueError, match="'--'"):
            asyncio.run(_go())


# cli/main.py — pre-parse verbose scan must be sentinel-aware


class TestMainVerboseScanSentinelAware:
    """The pre-argparse --verbose scan in main() must only inspect tokens
    before the '--' sentinel.  A scheduled action_prompt='--verbose' must
    not flip verbose mode.

    These tests check the fix directly: the pre-sentinel slice of argv must
    NOT contain '--verbose' when it appears after the '--' sentinel, and MUST
    contain it when it appears before the sentinel (normal human usage).

    Note: -v/--verbose is a subcommand flag (added by add_common_cli_args),
    not a global li flag.  Human invocation is `li agent --verbose sonnet hi`,
    NOT `li -v agent sonnet hi` (the latter is unrecognised by the top-level
    parser).  The pre-parse scan sees all of argv (before the fix); the fix
    restricts it to the pre-sentinel slice.
    """

    def test_pre_sentinel_slice_includes_verbose_before_sentinel(self):
        """Human `li agent --verbose sonnet hello` (no '--' sentinel):
        the pre-sentinel slice IS all of argv, so verbose=True is correct."""
        # Without a '--' sentinel in the argv, the full argv is pre_sentinel.
        cli_argv = ["agent", "--verbose", "sonnet", "hello"]

        try:
            sep_idx = cli_argv.index("--")
            pre_sentinel = cli_argv[:sep_idx]
        except ValueError:
            pre_sentinel = cli_argv

        verbose = "-v" in pre_sentinel or "--verbose" in pre_sentinel
        assert verbose, (
            f"Expected verbose=True for 'agent --verbose sonnet hello'. "
            f"pre_sentinel={pre_sentinel!r}"
        )

    def test_scheduled_verbose_prompt_reaches_runner_as_value(self):
        """End-to-end: action_prompt='--verbose' reaches _run_agent as the prompt
        value with verbose=False (the argparse-parsed verbose, not the pre-scan)."""
        from unittest.mock import patch as _patch

        import lionhub.serve.cli.agent as agent_mod
        from lionhub.orchestration.scheduler.subprocess import build_argv
        from lionhub.serve.cli.main import main

        captured = {}

        async def _fake_run_agent(model_str, prompt, *, verbose=False, **kwargs):
            captured["prompt"] = prompt
            captured["verbose"] = verbose
            return "out", "provider", "branch", "completed", "sess-001"

        sched = {
            "id": "t",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_prompt": "--verbose",
            "action_agent": None,
            "action_project": None,
            "action_extra_args": [],
        }
        full_argv, _ = build_argv(sched, {})
        cli_argv = full_argv[3:]

        with _patch.object(agent_mod, "_run_agent", _fake_run_agent):
            rc = main(cli_argv)

        assert rc == 0, f"Expected rc=0, got {rc}"
        assert captured.get("prompt") == "--verbose", (
            f"Expected '--verbose' as prompt value, got {captured.get('prompt')!r}"
        )
        assert captured.get("verbose") is False, (
            f"Expected verbose=False (argparse-parsed), got {captured.get('verbose')!r}"
        )


# build_argv — template injection: rendered prompt must be validated post-render
# (order-of-operations bypass)


# CWE-918 github_repo path manipulation — validator unit tests


# CWE-918 github_repo — service boundary (create and update)


class TestGithubRepoServiceValidation:
    """create_schedule and update_schedule must reject invalid github_repo at write time."""

    def _run(self, coro):
        return asyncio.run(coro)

    # create_schedule

    def test_create_path_traversal_raises(self):
        """create_schedule raises ValueError for '../../other-endpoint'."""
        from lionagi.studio.services.schedules import create_schedule

        with pytest.raises(ValueError, match="github_repo"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-repo-traversal",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "../../other-endpoint",
                    }
                )
            )

    def test_create_extra_slash_raises(self):
        """create_schedule raises ValueError for 'owner/name/extra'."""
        from lionagi.studio.services.schedules import create_schedule

        with pytest.raises(ValueError, match="github_repo"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-repo-extra-slash",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "owner/name/extra",
                    }
                )
            )

    def test_create_no_slash_raises(self):
        """create_schedule raises ValueError for 'owner' (no slash)."""
        from lionagi.studio.services.schedules import create_schedule

        with pytest.raises(ValueError, match="github_repo"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-repo-no-slash",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "owner",
                    }
                )
            )

    def test_create_leading_dash_raises(self):
        """create_schedule raises ValueError for '-owner/repo'."""
        from lionagi.studio.services.schedules import create_schedule

        with pytest.raises(ValueError, match="github_repo"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-repo-dash",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "-owner/repo",
                    }
                )
            )

    def test_create_percent_encoded_raises(self):
        """create_schedule raises ValueError for '%2e%2e/repo'."""
        from lionagi.studio.services.schedules import create_schedule

        with pytest.raises(ValueError, match="github_repo"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-repo-pct",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "%2e%2e/repo",
                    }
                )
            )

    def test_create_empty_string_raises(self):
        """create_schedule raises ValueError for empty github_repo string."""
        from lionagi.studio.services.schedules import create_schedule

        with pytest.raises(ValueError, match="github_repo"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-repo-empty",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "",
                    }
                )
            )

    def test_create_valid_repo_does_not_raise(self):
        """create_schedule with 'owner/repo.name-x_1' passes validation."""
        from unittest.mock import AsyncMock, patch

        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = self._run(
                __import__(
                    "lionagi.studio.services.schedules", fromlist=["create_schedule"]
                ).create_schedule(
                    {
                        "name": "good-repo",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "owner/repo.name-x_1",
                    }
                )
            )
        assert "id" in result

    # update_schedule

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
            "id": "sid-gh",
            "name": "gh-test",
            "trigger_type": "github_poll",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_extra_args": [],
        }
        base.update(over)
        return base

    def test_patch_path_traversal_raises(self):
        """PATCH github_repo='../../other-endpoint' raises ValueError."""
        with pytest.raises(ValueError, match="github_repo"):
            self._run_update(self._existing(), {"github_repo": "../../other-endpoint"})

    def test_patch_extra_slash_raises(self):
        """PATCH github_repo='owner/name/extra' raises ValueError."""
        with pytest.raises(ValueError, match="github_repo"):
            self._run_update(self._existing(), {"github_repo": "owner/name/extra"})

    def test_patch_no_slash_raises(self):
        """PATCH github_repo='owner' (no slash) raises ValueError."""
        with pytest.raises(ValueError, match="github_repo"):
            self._run_update(self._existing(), {"github_repo": "owner"})

    def test_patch_leading_dash_raises(self):
        """PATCH github_repo='-owner/repo' raises ValueError."""
        with pytest.raises(ValueError, match="github_repo"):
            self._run_update(self._existing(), {"github_repo": "-owner/repo"})

    def test_patch_valid_repo_does_not_raise(self):
        """PATCH github_repo='octocat/hello-world' passes validation."""
        result = self._run_update(self._existing(), {"github_repo": "octocat/hello-world"})
        assert result is True

    def test_patch_dot_prefix_repo_accepted(self):
        """PATCH github_repo='github/.github' must be accepted (valid GitHub repo)."""
        result = self._run_update(self._existing(), {"github_repo": "github/.github"})
        assert result is True

    def test_create_dot_prefix_repo_accepted(self):
        """create_schedule with github_repo='github/.github' must pass validation."""
        from unittest.mock import AsyncMock, patch

        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = self._run(
                __import__(
                    "lionagi.studio.services.schedules", fromlist=["create_schedule"]
                ).create_schedule(
                    {
                        "name": "dot-github-repo",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": "github/.github",
                    }
                )
            )
        assert "id" in result

    def test_create_1000_char_owner_raises(self):
        """create_schedule raises ValueError when owner segment is 1000 chars."""
        from lionagi.studio.services.schedules import create_schedule

        long_owner = "a" * 1000
        with pytest.raises(ValueError, match="owner segment"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-long-owner",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": f"{long_owner}/repo",
                    }
                )
            )

    def test_create_1000_char_repo_raises(self):
        """create_schedule raises ValueError when repo name segment is 1000 chars."""
        from lionagi.studio.services.schedules import create_schedule

        long_name = "a" * 1000
        with pytest.raises(ValueError, match="repo name segment"):
            self._run(
                create_schedule(
                    {
                        "name": "bad-long-repo",
                        "trigger_type": "github_poll",
                        "action_kind": "agent",
                        "action_model": "sonnet",
                        "github_repo": f"owner/{long_name}",
                    }
                )
            )

    def test_patch_1000_char_owner_raises(self):
        """PATCH github_repo with 1000-char owner raises ValueError."""
        long_owner = "a" * 1000
        with pytest.raises(ValueError, match="owner segment"):
            self._run_update(self._existing(), {"github_repo": f"{long_owner}/repo"})

    def test_patch_1000_char_repo_raises(self):
        """PATCH github_repo with 1000-char repo name raises ValueError."""
        long_name = "a" * 1000
        with pytest.raises(ValueError, match="repo name segment"):
            self._run_update(self._existing(), {"github_repo": f"owner/{long_name}"})

    def test_patch_bad_stored_value_raises_on_unrelated_patch(self):
        """PATCH of an unrelated field must raise when effective github_repo is invalid.

        This covers the case where a schedule was inserted with a bad github_repo
        (e.g. via direct DB import) and a later unrelated PATCH would previously
        silently preserve the stale-invalid value.
        """
        # Seed existing schedule with a bad stored github_repo
        existing_with_bad_repo = self._existing(github_repo="../../bad-stored")
        # PATCH an unrelated field -- the effective github_repo is still bad
        with pytest.raises(ValueError, match="owner/name|traversal|exactly one"):
            self._run_update(existing_with_bad_repo, {"description": "innocuous update"})

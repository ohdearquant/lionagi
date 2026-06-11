# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for argument injection hardening in build_argv (CWE-88).

Covers action_model and action_extra_args flag-injection rejection at:
  1. The subprocess layer (build_argv defensive validation).
  2. The service layer (create_schedule / update_schedule boundary).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _schedule(**kwargs) -> dict:
    base = {
        "id": "sched-inj",
        "name": "injection-test",
        "trigger_type": "cron",
        "action_kind": "agent",
        "action_model": "sonnet",
        "action_prompt": "hello",
        "action_agent": None,
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# subprocess.build_argv — action_model validation
# ---------------------------------------------------------------------------


class TestBuildArgvActionModelInjection:
    """build_argv must reject action_model values that inject CLI flags."""

    def test_model_starting_with_dash_raises(self):
        """action_model starting with '-' must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--bypass"), {})

    def test_model_bypass_flag_raises(self):
        """Literal --bypass must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--bypass"), {})

    def test_model_yolo_flag_raises(self):
        """Literal --yolo must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--yolo"), {})

    def test_model_project_flag_raises(self):
        """Literal --project must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--project"), {})

    def test_model_short_flag_raises(self):
        """Single-dash flag (-m) must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="-m"), {})

    def test_model_invalid_chars_raises(self):
        """Semicolons and spaces in action_model must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="characters not allowed"):
            build_argv(_schedule(action_model="gpt-4; rm -rf /"), {})

    def test_model_empty_string_accepted(self):
        """Empty action_model is allowed (means 'no model specified')."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=""), {})
        assert tmp is None
        assert "uv" in argv

    def test_model_none_accepted(self):
        """None action_model is allowed."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=None), {})
        assert tmp is None

    def test_model_valid_identifiers_accepted(self):
        """Legitimate model identifiers must pass without error."""
        from lionagi.studio.scheduler.subprocess import build_argv

        valid_models = [
            "gpt-4",
            "claude-sonnet-4-6",
            "claude_code/sonnet",
            "openai/gpt-4.1-mini",
            "anthropic:claude-3-5-sonnet-20241022",
            "us.anthropic.claude-3-5-sonnet",
            "provider/model:version",
        ]
        for model in valid_models:
            argv, tmp = build_argv(_schedule(action_model=model), {})
            assert model in argv, f"model {model!r} should appear in argv"
            if tmp:
                import os

                os.unlink(tmp)


# ---------------------------------------------------------------------------
# subprocess.build_argv — action_extra_args validation
# ---------------------------------------------------------------------------


class TestBuildArgvExtraArgsInjection:
    """build_argv must reject action_extra_args elements that inject CLI flags."""

    def test_extra_bypass_flag_raises(self):
        """'--bypass' in action_extra_args must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["--bypass"]), {})

    def test_extra_yolo_flag_raises(self):
        """'--yolo' in action_extra_args must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["--yolo"]), {})

    def test_extra_short_flag_raises(self):
        """'-v' in action_extra_args must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["-v"]), {})

    def test_extra_flag_in_mixed_list_raises(self):
        """A flag mixed with legitimate tokens must be caught."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["my-task", "--bypass", "arg2"]), {})

    def test_extra_names_the_offending_element(self):
        """The error message must include the offending token."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="--yolo"):
            build_argv(_schedule(action_extra_args=["ok-token", "--yolo"]), {})

    def test_extra_empty_list_accepted(self):
        """Empty action_extra_args is valid."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_extra_args=[]), {})
        assert "uv" in argv
        assert tmp is None

    def test_extra_none_accepted(self):
        """None action_extra_args is valid."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_extra_args=None), {})
        assert "uv" in argv
        assert tmp is None

    def test_extra_positional_tokens_accepted(self):
        """Non-flag positional tokens must pass and appear in argv."""
        from lionagi.studio.scheduler.subprocess import build_argv

        tokens = ["my-playbook", "some_task", "123"]
        argv, tmp = build_argv(_schedule(action_extra_args=tokens), {})
        for tok in tokens:
            assert tok in argv, f"token {tok!r} should be in argv"
        assert tmp is None


# ---------------------------------------------------------------------------
# service layer — create_schedule validation
# ---------------------------------------------------------------------------


class TestCreateScheduleInjectionRejection:
    """create_schedule must reject flag-injection in action_model and action_extra_args."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_create_with_model_flag_raises_value_error(self):
        """create_schedule raises ValueError when action_model starts with '-'."""
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
        """create_schedule rejects --yolo in action_model."""
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
        """create_schedule raises ValueError when action_extra_args contains a flag."""
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

    def test_create_valid_model_and_extra_does_not_raise(self, monkeypatch):
        """create_schedule with safe values proceeds past validation."""
        from lionagi.studio.services.schedules import create_schedule

        async def _fake_create(schedule):
            pass

        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            data = {
                "name": "good-sched",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "claude-sonnet-4-6",
                "action_extra_args": ["my-task"],
            }
            result = self._run(create_schedule(data))
        assert "id" in result


# ---------------------------------------------------------------------------
# service layer — update_schedule (PATCH) validation
# ---------------------------------------------------------------------------


class TestUpdateScheduleInjectionRejection:
    """update_schedule must reject flag-injection in patched action_model and action_extra_args."""

    def _mock_db(self, existing: dict):
        """Return a context-manager mock that returns *existing* from get_schedule."""

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
        """PATCH action_model='--bypass' raises ValueError."""
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_model": "--bypass"})

    def test_patch_model_yolo_raises(self):
        """PATCH action_model='--yolo' raises ValueError."""
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_model": "--yolo"})

    def test_patch_extra_args_flag_raises(self):
        """PATCH action_extra_args=['--bypass'] raises ValueError."""
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_extra_args": ["--bypass"]})

    def test_patch_extra_args_yolo_raises(self):
        """PATCH action_extra_args=['--yolo'] raises ValueError."""
        with pytest.raises(ValueError, match="starts with '-'"):
            self._run_update(self._existing(), {"action_extra_args": ["--yolo"]})

    def test_patch_valid_fields_does_not_raise(self):
        """PATCH with safe values proceeds past validation."""
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

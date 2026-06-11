# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for argument injection hardening in build_argv (CWE-88).

Covers action_model and action_extra_args flag-injection rejection at:
  1. The subprocess layer (build_argv defensive validation).
  2. The service layer (create_schedule / update_schedule boundary).
  3. action_agent / action_project / action_playbook identifier validation.
  4. Structural: -- sentinel and flow_yaml prompt-drop assertions.
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
        "action_prompt": "hello world",
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
# subprocess.build_argv — identifier fields (action_agent/project/playbook)
# ---------------------------------------------------------------------------


class TestBuildArgvIdentifierInjection:
    """build_argv must reject leading-dash values in identifier fields."""

    def test_action_agent_dash_prefix_raises(self):
        """action_agent starting with '-' must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_agent"):
            build_argv(_schedule(action_agent="--bypass"), {})

    def test_action_project_dash_prefix_raises(self):
        """action_project starting with '-' must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_project"):
            build_argv(_schedule(action_project="--yolo"), {})

    def test_action_playbook_dash_prefix_raises(self):
        """action_playbook starting with '-' must raise ValueError in play kind."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_playbook"):
            build_argv(
                _schedule(action_kind="play", action_playbook="--bypass"),
                {},
            )

    def test_action_agent_valid_accepted(self):
        """A valid agent name must pass."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_agent="my-agent"), {})
        assert "my-agent" in argv
        assert tmp is None

    def test_action_project_valid_accepted(self):
        """A valid project name must pass."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_project="my-project"), {})
        assert "--project" in argv
        assert "my-project" in argv
        assert tmp is None


# ---------------------------------------------------------------------------
# build_argv structural: -- sentinel and positional ordering
# ---------------------------------------------------------------------------


class TestBuildArgvSentinelStructure:
    """build_argv must emit a '--' sentinel before positionals so freeform
    prompt text cannot be parsed as CLI flags by argparse."""

    def test_agent_argv_has_sentinel_before_prompt(self):
        """agent kind: '--' appears before the model and prompt positionals."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="agent", action_prompt="--bypass"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        # model and prompt must come AFTER the sentinel
        assert "sonnet" in argv[sentinel_idx + 1 :]
        assert "--bypass" in argv[sentinel_idx + 1 :]
        # --bypass must NOT appear before the sentinel (no flag injection)
        assert "--bypass" not in argv[:sentinel_idx]

    def test_flow_argv_has_sentinel_before_prompt(self):
        """flow kind: '--' appears before model and prompt positionals."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="flow", action_prompt="--yolo"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        assert "--yolo" in argv[sentinel_idx + 1 :]
        assert "--yolo" not in argv[:sentinel_idx]

    def test_fanout_argv_has_sentinel_before_prompt(self):
        """fanout kind: '--' appears before model and prompt positionals."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="fanout", action_prompt="--fast"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        assert "--fast" in argv[sentinel_idx + 1 :]
        assert "--fast" not in argv[:sentinel_idx]

    def test_flow_yaml_has_no_prompt_positional(self):
        """flow_yaml kind must NOT include the prompt as a positional in argv.

        The YAML file supplies the prompt.  Including the prompt positional
        would open a second injection surface for action_prompt.
        """
        import os

        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "sy",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": "--bypass",  # hostile prompt — must NOT appear as flag
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "prompt: yaml-supplied\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            # The hostile prompt must not appear at all in the argv — it is
            # ignored because the YAML file supplies the prompt for flow_yaml.
            assert "--bypass" not in argv, (
                "action_prompt must not be included in flow_yaml argv "
                f"(prompt injection still reachable): {argv}"
            )
            # -f must appear (pointing to the yaml temp file)
            assert "-f" in argv
            # '--' sentinel must be present
            assert "--" in argv
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_flow_yaml_named_flags_before_sentinel(self):
        """flow_yaml: -f <path> must appear BEFORE the '--' sentinel."""
        import os

        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "sy",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": "hello",
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "prompt: yaml\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            sentinel_idx = argv.index("--")
            f_idx = argv.index("-f")
            assert f_idx < sentinel_idx, f"-f must appear before '--' sentinel: argv={argv}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_project_flag_placed_before_sentinel(self):
        """--project <name> must appear before the '--' sentinel in agent kind."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="agent", action_project="my-proj"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        proj_idx = argv.index("--project")
        assert proj_idx < sentinel_idx, (
            "--project must be before '--' sentinel so it is not misinterpreted"
        )


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

    def test_create_with_agent_flag_raises(self):
        """create_schedule rejects action_agent starting with '-'."""
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
        """create_schedule rejects action_project starting with '-'."""
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
        """create_schedule with safe values proceeds past validation."""
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
            result = self._run(
                __import__(
                    "lionagi.studio.services.schedules", fromlist=["create_schedule"]
                ).create_schedule(data)
            )
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

    def test_patch_agent_flag_raises(self):
        """PATCH action_agent='--bypass' raises ValueError."""
        with pytest.raises(ValueError, match="action_agent"):
            self._run_update(self._existing(), {"action_agent": "--bypass"})

    def test_patch_project_flag_raises(self):
        """PATCH action_project='--yolo' raises ValueError."""
        with pytest.raises(ValueError, match="action_project"):
            self._run_update(self._existing(), {"action_project": "--yolo"})

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


# ---------------------------------------------------------------------------
# action_prompt == '--' sentinel rejection (codex round 2)
# ---------------------------------------------------------------------------


class TestBuildArgvPromptSentinelRejection:
    """build_argv must reject action_prompt exactly equal to '--'.

    The literal end-of-options token '--' is consumed by argparse as a separator
    rather than reaching the runner as prompt text.  Any other prompt content —
    including '--bypass', '--verbose', '-- --', '-- trailing' — is permitted.
    """

    def test_prompt_double_dash_raises(self):
        """action_prompt='--' must raise ValueError (silently eaten by argparse)."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_prompt="--"), {})

    def test_prompt_double_dash_in_flow_kind_raises(self):
        """flow kind also rejects prompt == '--'."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_kind="flow", action_prompt="--"), {})

    def test_prompt_double_dash_in_fanout_kind_raises(self):
        """fanout kind also rejects prompt == '--'."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_kind="fanout", action_prompt="--"), {})

    def test_prompt_double_dash_prefix_allowed(self):
        """'-- --' (starts with -- but not exactly --) must be accepted."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_prompt="-- --"), {})
        assert tmp is None
        # Value appears after sentinel in argv
        assert "-- --" in argv

    def test_prompt_double_dash_with_trailing_text_allowed(self):
        """'-- some trailing text' must be accepted and appear in argv."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_prompt="-- some trailing"), {})
        assert tmp is None
        assert "-- some trailing" in argv

    def test_prompt_bypass_still_allowed(self):
        """'--bypass' as prompt must still pass (structural -- fix handles it)."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_prompt="--bypass"), {})
        assert tmp is None
        sentinel_idx = argv.index("--")
        assert "--bypass" in argv[sentinel_idx + 1 :]

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


# ---------------------------------------------------------------------------
# cli/main.py — pre-parse verbose scan must be sentinel-aware (codex round 2)
# ---------------------------------------------------------------------------


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

    def test_pre_sentinel_slice_excludes_verbose_after_sentinel(self):
        """Built argv for agent kind with action_prompt='--verbose':
        the tokens BEFORE '--' must not include '--verbose'."""
        from lionagi.studio.scheduler.subprocess import build_argv

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
        cli_argv = full_argv[3:]  # strip 'uv run li'

        # Reproduce the sentinel-aware scan from main()
        try:
            sep_idx = cli_argv.index("--")
            pre_sentinel = cli_argv[:sep_idx]
        except ValueError:
            pre_sentinel = cli_argv

        verbose = "-v" in pre_sentinel or "--verbose" in pre_sentinel
        assert not verbose, (
            f"pre-sentinel scan incorrectly set verbose=True. "
            f"pre_sentinel={pre_sentinel!r}, full argv={cli_argv!r}"
        )
        # '--verbose' must be AFTER the sentinel (in the positionals section)
        assert "--verbose" in cli_argv[cli_argv.index("--") + 1 :], (
            f"'--verbose' should appear as prompt value after '--'. argv={cli_argv!r}"
        )

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

        import lionagi.cli.agent as agent_mod
        from lionagi.cli.main import main
        from lionagi.studio.scheduler.subprocess import build_argv

        captured = {}

        async def _fake_run_agent(model_str, prompt, *, verbose=False, **kwargs):
            captured["prompt"] = prompt
            captured["verbose"] = verbose
            return "out", "provider", "branch", "completed"

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

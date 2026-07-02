# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for argument injection hardening in build_argv (CWE-88): model, extra-args, identifiers, sentinel."""

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
    def test_model_starting_with_dash_raises(self):
        """action_model starting with '-' must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--bypass"), {})

    def test_model_bypass_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--bypass"), {})

    def test_model_yolo_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--yolo"), {})

    def test_model_project_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="--project"), {})

    def test_model_short_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model="-m"), {})

    def test_model_invalid_chars_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="characters not allowed"):
            build_argv(_schedule(action_model="gpt-4; rm -rf /"), {})

    def test_model_empty_string_accepted(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=""), {})
        assert tmp is None
        assert "uv" in argv

    def test_model_none_accepted(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=None), {})
        assert tmp is None

    def test_model_empty_string_omits_positional_agent_kind(self):
        """Regression: an empty action_model must not be forwarded as a blank
        model positional for kind='agent'. `li agent` treats a single
        positional as the prompt and falls through to the --agent profile's
        default model; a blank model positional instead overrides that
        default and crashes Branch init (EndpointConfig: provider '')."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model="", action_agent="researcher"), {})
        assert tmp is None
        assert "--" in argv
        positionals = argv[argv.index("--") + 1 :]
        assert positionals == ["hello world"], (
            f"expected only the prompt after '--', got {positionals!r}; an "
            "empty-string model positional would override the --agent "
            "profile's default model"
        )

    def test_model_none_omits_positional_agent_kind(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=None, action_agent="researcher"), {})
        positionals = argv[argv.index("--") + 1 :]
        assert positionals == ["hello world"]

    def test_model_set_still_includes_positional_agent_kind(self):
        """Non-empty action_model must still be forwarded (no regression)."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model="sonnet", action_agent="researcher"), {})
        positionals = argv[argv.index("--") + 1 :]
        assert positionals == ["sonnet", "hello world"]

    def test_model_valid_identifiers_accepted(self):
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
    def test_extra_bypass_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["--bypass"]), {})

    def test_extra_yolo_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["--yolo"]), {})

    def test_extra_short_flag_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["-v"]), {})

    def test_extra_flag_in_mixed_list_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["my-task", "--bypass", "arg2"]), {})

    def test_extra_names_the_offending_element(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="--yolo"):
            build_argv(_schedule(action_extra_args=["ok-token", "--yolo"]), {})

    def test_extra_empty_list_accepted(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_extra_args=[]), {})
        assert "uv" in argv
        assert tmp is None

    def test_extra_none_accepted(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_extra_args=None), {})
        assert "uv" in argv
        assert tmp is None

    def test_extra_positional_tokens_accepted(self):
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
    def test_action_agent_dash_prefix_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_agent"):
            build_argv(_schedule(action_agent="--bypass"), {})

    def test_action_project_dash_prefix_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_project"):
            build_argv(_schedule(action_project="--yolo"), {})

    def test_action_playbook_dash_prefix_raises(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_playbook"):
            build_argv(
                _schedule(action_kind="play", action_playbook="--bypass"),
                {},
            )

    def test_action_agent_valid_accepted(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_agent="my-agent"), {})
        assert "my-agent" in argv
        assert tmp is None

    def test_action_project_valid_accepted(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_project="my-project"), {})
        assert "--project" in argv
        assert "my-project" in argv
        assert tmp is None


# ---------------------------------------------------------------------------
# build_argv structural: -- sentinel and positional ordering
# ---------------------------------------------------------------------------


class TestBuildArgvSentinelStructure:
    def test_agent_argv_has_sentinel_before_prompt(self):
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
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="flow", action_prompt="--yolo"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        assert "--yolo" in argv[sentinel_idx + 1 :]
        assert "--yolo" not in argv[:sentinel_idx]

    def test_fanout_argv_has_sentinel_before_prompt(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="fanout", action_prompt="--fast"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        assert "--fast" in argv[sentinel_idx + 1 :]
        assert "--fast" not in argv[:sentinel_idx]

    def test_flow_yaml_has_no_prompt_positional(self):
        """flow_yaml kind: prompt omitted from argv; YAML file supplies it instead."""
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


# ---------------------------------------------------------------------------
# action_prompt == '--' sentinel rejection
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
# cli/main.py — pre-parse verbose scan must be sentinel-aware
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


# ---------------------------------------------------------------------------
# build_argv — template injection: rendered prompt must be validated post-render
# (order-of-operations bypass)
# ---------------------------------------------------------------------------


class TestBuildArgvTemplateInjection:
    """build_argv must validate action_prompt AFTER _render_template, not before.

    A stored prompt like '{{payload}}' passes pre-render validation.  If
    trigger_context supplies {"payload": "--"}, the rendered value is the
    forbidden sentinel.  The fix: _validate_prompt runs on the rendered value,
    so such template reconstruction is caught at spawn time.
    """

    def test_template_renders_sentinel_raises_agent(self):
        """agent kind: '{{payload}}' + context {"payload": "--"} must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(
                _schedule(action_kind="agent", action_prompt="{{payload}}"),
                {"payload": "--"},
            )

    def test_template_renders_sentinel_raises_flow(self):
        """flow kind: '{{payload}}' + context {"payload": "--"} must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(
                _schedule(action_kind="flow", action_prompt="{{payload}}"),
                {"payload": "--"},
            )

    def test_template_renders_sentinel_raises_fanout(self):
        """fanout kind: '{{payload}}' + context {"payload": "--"} must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(
                _schedule(action_kind="fanout", action_prompt="{{payload}}"),
                {"payload": "--"},
            )

    def test_template_renders_safe_value_passes(self):
        """'{{payload}}' + context {"payload": "hello"} must build argv normally."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(
            _schedule(action_kind="agent", action_prompt="{{payload}}"),
            {"payload": "hello"},
        )
        assert tmp is None
        sentinel_idx = argv.index("--")
        assert "hello" in argv[sentinel_idx + 1 :]

    def test_template_renders_bypass_flag_safe(self):
        """'{{payload}}' + context {"payload": "--bypass"} reaches argv as value,
        not as a flag.  (The structural -- fix handles leading-dash prompts;
        only the exact '--' singleton is forbidden.)"""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp = build_argv(
            _schedule(action_kind="agent", action_prompt="{{payload}}"),
            {"payload": "--bypass"},
        )
        assert tmp is None
        sentinel_idx = argv.index("--")
        assert "--bypass" in argv[sentinel_idx + 1 :]

    def test_template_literal_sentinel_in_stored_prompt_raises(self):
        """A stored prompt that IS literally '--' (no template) is still rejected —
        pre-render path unchanged."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_prompt="--"), {})


# ---------------------------------------------------------------------------
# CWE-918 github_repo path manipulation — validator unit tests
# ---------------------------------------------------------------------------


class TestGithubRepoValidatorUnit:
    """_validate_github_repo rejects values that would manipulate the API path."""

    def test_path_traversal_raises(self):
        """'../../other-endpoint' must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("../../other-endpoint")

    def test_extra_slash_segment_raises(self):
        """'owner/name/extra' (more than one slash) must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("owner/name/extra")

    def test_no_slash_raises(self):
        """'owner' (missing slash) must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("owner")

    def test_leading_dash_in_owner_raises(self):
        """'-owner/repo' (leading dash in owner segment) must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("-owner/repo")

    def test_percent_encoded_traversal_raises(self):
        """'%2e%2e/repo' (URL-encoded dots) must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("%2e%2e/repo")

    def test_empty_string_raises(self):
        """Empty string must raise ValueError (no owner/name structure)."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="github_repo"):
            _validate_github_repo("")

    def test_valid_repo_accepted(self):
        """'owner/repo.name-x_1' is a legitimate GitHub repo and must pass."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        _validate_github_repo("owner/repo.name-x_1")  # must not raise

    def test_valid_simple_repo_accepted(self):
        """'octocat/hello-world' is a common GitHub repo and must pass."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        _validate_github_repo("octocat/hello-world")  # must not raise

    def test_dot_prefix_repo_accepted(self):
        """'github/.github' must be accepted -- repos starting with '.' are valid
        (verified: https://api.github.com/repos/github/.github returns 200)."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        _validate_github_repo("github/.github")  # must not raise

    def test_single_char_owner_accepted(self):
        """'a/repo' — a single-char owner is valid GitHub grammar and must pass."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        _validate_github_repo("a/repo")  # must not raise

    def test_dotted_repo_names_accepted(self):
        """Repo names that are dotted but not traversal singletons are accepted.

        'owner/...', 'owner/a..b', 'owner/.git' remain a single URL path segment
        (not '.'/'..'), so they are path-safe and must not be over-rejected.
        """
        from lionagi.studio.scheduler.github import _validate_github_repo

        _validate_github_repo("owner/...")  # must not raise
        _validate_github_repo("owner/a..b")  # must not raise
        _validate_github_repo("owner/.git")  # must not raise

    def test_dot_singleton_repo_raises(self):
        """'owner/.' is a path-traversal singleton and must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="traversal"):
            _validate_github_repo("owner/.")

    def test_dotdot_singleton_repo_raises(self):
        """'owner/..' is a path-traversal singleton and must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        with pytest.raises(ValueError, match="traversal"):
            _validate_github_repo("owner/..")

    def test_owner_too_long_raises(self):
        """Owner segment > 39 chars must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        long_owner = "a" * 40
        with pytest.raises(ValueError, match="owner segment"):
            _validate_github_repo(f"{long_owner}/repo")

    def test_repo_name_too_long_raises(self):
        """Repo name segment > 100 chars must raise ValueError."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        long_name = "a" * 101
        with pytest.raises(ValueError, match="repo name segment"):
            _validate_github_repo(f"owner/{long_name}")

    def test_owner_exactly_max_length_accepted(self):
        """Owner segment of exactly 39 chars must be accepted."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        owner = "a" * 39
        _validate_github_repo(f"{owner}/repo")  # must not raise

    def test_repo_name_exactly_max_length_accepted(self):
        """Repo name of exactly 100 chars must be accepted."""
        from lionagi.studio.scheduler.github import _validate_github_repo

        name = "a" * 100
        _validate_github_repo(f"owner/{name}")  # must not raise


# ---------------------------------------------------------------------------
# CWE-918 github_repo — service boundary (create and update)
# ---------------------------------------------------------------------------


class TestGithubRepoServiceValidation:
    """create_schedule and update_schedule must reject invalid github_repo at write time."""

    def _run(self, coro):
        return asyncio.run(coro)

    # -- create_schedule --

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

    # -- update_schedule --

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

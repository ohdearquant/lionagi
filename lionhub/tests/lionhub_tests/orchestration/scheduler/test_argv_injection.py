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


class TestBuildArgvActionModelInjection:
    @pytest.mark.parametrize(
        "action_model",
        ("--bypass", "--yolo", "--project", "-m"),
        ids=("bypass", "yolo", "project", "short-flag"),
    )
    def test_model_starting_with_dash_raises(self, action_model):
        """action_model starting with '-' must raise ValueError."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_model=action_model), {})

    def test_model_invalid_chars_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="characters not allowed"):
            build_argv(_schedule(action_model="gpt-4; rm -rf /"), {})

    def test_model_empty_string_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=""), {})
        assert tmp is None
        assert "uv" in argv

    def test_model_none_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=None), {})
        assert tmp is None

    def test_model_empty_string_omits_positional_agent_kind(self):
        """Regression: an empty action_model must not be forwarded as a blank
        model positional for kind='agent'. `li agent` treats a single
        positional as the prompt and falls through to the --agent profile's
        default model; a blank model positional instead overrides that
        default and crashes Branch init (EndpointConfig: provider '')."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model=None, action_agent="researcher"), {})
        positionals = argv[argv.index("--") + 1 :]
        assert positionals == ["hello world"]

    def test_model_set_still_includes_positional_agent_kind(self):
        """Non-empty action_model must still be forwarded (no regression)."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_model="sonnet", action_agent="researcher"), {})
        positionals = argv[argv.index("--") + 1 :]
        assert positionals == ["sonnet", "hello world"]

    def test_model_empty_with_extra_args_raises(self):
        """Regression: when kind='agent' omits the model positional (falsy
        action_model), a non-empty action_extra_args token collapses the CLI
        down to exactly 2 positionals -- the same arity as the historical
        [model, prompt] shape -- so `li agent`'s model/prompt resolver
        silently reparses the real prompt as MODEL and the extra-args token
        as PROMPT instead of failing loudly. This combination must raise at
        build time."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_extra_args"):
            build_argv(
                _schedule(
                    action_model="",
                    action_agent="researcher",
                    action_extra_args=["strict"],
                ),
                {},
            )

    def test_model_none_with_extra_args_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_extra_args"):
            build_argv(
                _schedule(
                    action_model=None,
                    action_agent="researcher",
                    action_extra_args=["strict"],
                ),
                {},
            )

    def test_model_set_with_extra_args_still_accepted(self):
        """Non-empty action_model + action_extra_args is unaffected: the
        positional count is unambiguous (model, prompt) regardless of the
        extra tokens appended after them."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(
            _schedule(
                action_model="sonnet",
                action_agent="researcher",
                action_extra_args=["strict"],
            ),
            {},
        )
        positionals = argv[argv.index("--") + 1 :]
        assert positionals == ["sonnet", "hello world", "strict"]

    def test_model_with_a_trailing_newline_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="characters not allowed"):
            build_argv(_schedule(action_model="sonnet\n"), {})

    def test_model_valid_identifiers_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

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


# subprocess.build_argv — action_extra_args validation


class TestBuildArgvExtraArgsInjection:
    def test_extra_bypass_flag_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["--bypass"]), {})

    def test_extra_yolo_flag_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["--yolo"]), {})

    def test_extra_short_flag_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["-v"]), {})

    def test_extra_flag_in_mixed_list_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="starts with '-'"):
            build_argv(_schedule(action_extra_args=["my-task", "--bypass", "arg2"]), {})

    def test_extra_names_the_offending_element(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="--yolo"):
            build_argv(_schedule(action_extra_args=["ok-token", "--yolo"]), {})

    def test_extra_empty_list_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_extra_args=[]), {})
        assert "uv" in argv
        assert tmp is None

    def test_extra_none_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_extra_args=None), {})
        assert "uv" in argv
        assert tmp is None

    def test_extra_positional_tokens_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        tokens = ["my-playbook", "some_task", "123"]
        argv, tmp = build_argv(_schedule(action_extra_args=tokens), {})
        for tok in tokens:
            assert tok in argv, f"token {tok!r} should be in argv"
        assert tmp is None


# subprocess.build_argv — identifier fields (action_agent/project/playbook)


class TestBuildArgvIdentifierInjection:
    def test_action_agent_dash_prefix_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_agent"):
            build_argv(_schedule(action_agent="--bypass"), {})

    def test_action_project_dash_prefix_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_project"):
            build_argv(_schedule(action_project="--yolo"), {})

    def test_action_playbook_dash_prefix_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_playbook"):
            build_argv(
                _schedule(action_kind="play", action_playbook="--bypass"),
                {},
            )

    def test_action_agent_valid_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_agent="my-agent"), {})
        assert "my-agent" in argv
        assert tmp is None

    def test_action_agent_with_a_trailing_newline_raises(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="action_agent"):
            build_argv(_schedule(action_agent="my-agent\n"), {})

    def test_action_project_valid_accepted(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_project="my-project"), {})
        assert "--project" in argv
        assert "my-project" in argv
        assert tmp is None


# build_argv structural: -- sentinel and positional ordering


class TestBuildArgvSentinelStructure:
    def test_agent_argv_has_sentinel_before_prompt(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="flow", action_prompt="--yolo"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        assert "--yolo" in argv[sentinel_idx + 1 :]
        assert "--yolo" not in argv[:sentinel_idx]

    def test_fanout_argv_has_sentinel_before_prompt(self):
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="fanout", action_prompt="--fast"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        assert "--fast" in argv[sentinel_idx + 1 :]
        assert "--fast" not in argv[:sentinel_idx]

    def test_flow_yaml_has_no_prompt_positional(self):
        """flow_yaml kind: prompt omitted from argv; YAML file supplies it instead."""
        import os

        from lionhub.orchestration.scheduler.subprocess import build_argv

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

        from lionhub.orchestration.scheduler.subprocess import build_argv

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

    def test_flow_yaml_empty_model_omits_positional(self):
        """flow_yaml with no action_model: the model positional is omitted so
        the YAML file's own model/agent defaults apply. An explicit blank
        positional would parse as model='' and suppress the file defaults."""
        import os

        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "sy",
            "action_kind": "flow_yaml",
            "action_model": "",
            "action_prompt": None,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "model: claude-code/opus-4-7\nprompt: yaml prompt\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            assert "" not in argv, f"blank positional leaked into argv: {argv}"
            assert argv[-1] == "--", f"argv must end at the sentinel when model unset: {argv}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_flow_yaml_model_merged_into_spec(self):
        """flow_yaml with a model: merged into the spec file, never a positional.
        A lone model positional is reclassified as prompt text by `li o flow -f`
        whenever the file carries its own model/agent, so the file must win."""
        import os

        import yaml

        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "sy",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": None,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "model: claude-code/opus-4-7\nprompt: yaml prompt\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            assert "sonnet" not in argv, f"model leaked into argv: {argv}"
            assert argv[-1] == "--", f"argv must end at the sentinel: {argv}"
            with open(tmp_path) as fh:
                spec = yaml.safe_load(fh)
            assert spec["model"] == "sonnet"
            assert spec["prompt"] == "yaml prompt"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_flow_yaml_extra_args_rejected(self):
        """flow_yaml emits no positionals at all, so extra args would be
        soaked into `li o flow`'s optional model/prompt slots and silently
        override the model merged into the spec — build_argv rejects them
        outright so no invocation row is created."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "sy",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": None,
            "action_project": None,
            "action_extra_args": ["extra-model", "extra-prompt"],
            "action_flow_yaml": "model: file-model\nprompt: yaml prompt\n",
        }
        with pytest.raises(ValueError, match="action_extra_args"):
            build_argv(sched, {})

    def test_flow_yaml_unparseable_spec_written_unchanged(self):
        """flow_yaml with a model but a broken spec: no merge, no positional;
        the file is written as-is so `li o flow` reports its own parse error."""
        import os

        from lionhub.orchestration.scheduler.subprocess import build_argv

        broken = "prompt: [unclosed\n"
        sched = {
            "id": "sy",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": None,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": broken,
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            assert "sonnet" not in argv, f"model leaked into argv: {argv}"
            with open(tmp_path) as fh:
                assert fh.read() == broken
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_project_flag_placed_before_sentinel(self):
        """--project <name> must appear before the '--' sentinel in agent kind."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_kind="agent", action_project="my-proj"), {})
        assert tmp is None
        assert "--" in argv
        sentinel_idx = argv.index("--")
        proj_idx = argv.index("--project")
        assert proj_idx < sentinel_idx, (
            "--project must be before '--' sentinel so it is not misinterpreted"
        )


# service layer — create_schedule validation


# service layer — update_schedule (PATCH) validation


# action_prompt == '--' sentinel rejection


class TestBuildArgvPromptSentinelRejection:
    """build_argv must reject action_prompt exactly equal to '--'.

    The literal end-of-options token '--' is consumed by argparse as a separator
    rather than reaching the runner as prompt text.  Any other prompt content —
    including '--bypass', '--verbose', '-- --', '-- trailing' — is permitted.
    """

    def test_prompt_double_dash_raises(self):
        """Stored '--' stays forbidden after rendering and cannot be lost to argparse."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_prompt="--"), {})

    def test_prompt_double_dash_in_flow_kind_raises(self):
        """flow kind also rejects prompt == '--'."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_kind="flow", action_prompt="--"), {})

    def test_prompt_double_dash_in_fanout_kind_raises(self):
        """fanout kind also rejects prompt == '--'."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(_schedule(action_kind="fanout", action_prompt="--"), {})

    def test_prompt_double_dash_prefix_allowed(self):
        """'-- --' (starts with -- but not exactly --) must be accepted."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_prompt="-- --"), {})
        assert tmp is None
        # Value appears after sentinel in argv
        assert "-- --" in argv

    def test_prompt_double_dash_with_trailing_text_allowed(self):
        """'-- some trailing text' must be accepted and appear in argv."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_prompt="-- some trailing"), {})
        assert tmp is None
        assert "-- some trailing" in argv

    def test_prompt_bypass_still_allowed(self):
        """'--bypass' as prompt must still pass (structural -- fix handles it)."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(_schedule(action_prompt="--bypass"), {})
        assert tmp is None
        sentinel_idx = argv.index("--")
        assert "--bypass" in argv[sentinel_idx + 1 :]


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

    def test_pre_sentinel_slice_excludes_verbose_after_sentinel(self):
        """Built argv for agent kind with action_prompt='--verbose':
        the tokens BEFORE '--' must not include '--verbose'."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

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


# build_argv — template injection: rendered prompt must be validated post-render
# (order-of-operations bypass)


class TestBuildArgvTemplateInjection:
    """build_argv must validate action_prompt AFTER _render_template, not before.

    A stored prompt like '{{payload}}' passes pre-render validation.  If
    trigger_context supplies {"payload": "--"}, the rendered value is the
    forbidden sentinel.  The fix: _validate_prompt runs on the rendered value,
    so such template reconstruction is caught at spawn time.
    """

    def test_template_renders_sentinel_raises_agent(self):
        """agent kind: '{{payload}}' + context {"payload": "--"} must raise ValueError."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(
                _schedule(action_kind="agent", action_prompt="{{payload}}"),
                {"payload": "--"},
            )

    def test_template_renders_sentinel_raises_flow(self):
        """flow kind: '{{payload}}' + context {"payload": "--"} must raise ValueError."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(
                _schedule(action_kind="flow", action_prompt="{{payload}}"),
                {"payload": "--"},
            )

    def test_template_renders_sentinel_raises_fanout(self):
        """fanout kind: '{{payload}}' + context {"payload": "--"} must raise ValueError."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="'--'"):
            build_argv(
                _schedule(action_kind="fanout", action_prompt="{{payload}}"),
                {"payload": "--"},
            )

    def test_template_renders_safe_value_passes(self):
        """'{{payload}}' + context {"payload": "hello"} must build argv normally."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
        from lionhub.orchestration.scheduler.subprocess import build_argv

        argv, tmp = build_argv(
            _schedule(action_kind="agent", action_prompt="{{payload}}"),
            {"payload": "--bypass"},
        )
        assert tmp is None
        sentinel_idx = argv.index("--")
        assert "--bypass" in argv[sentinel_idx + 1 :]


# CWE-918 github_repo path manipulation — validator unit tests


# CWE-918 github_repo — service boundary (create and update)

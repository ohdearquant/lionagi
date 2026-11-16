# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Parser-level regression tests: hostile action_prompt values must arrive as VALUE, not toggle flags (CWE-88)."""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

# Shared capture store — reset before each parametrized test invocation

_CAPTURED: dict[str, Any] = {}


def _reset() -> None:
    _CAPTURED.clear()


# Fake run functions
# Each captures the positional and keyword arguments it receives so tests can
# assert on flag values without executing any network/agent logic.


async def _fake_run_agent(
    model_str: str | None,
    prompt: str,
    *,
    bypass: bool = False,
    yolo: bool = False,
    fast: bool = False,
    verbose: bool = False,
    **kwargs: Any,
) -> tuple[str, str, str, str, str | None]:
    _CAPTURED["agent"] = {
        "model_str": model_str,
        "prompt": prompt,
        "bypass": bypass,
        "yolo": yolo,
        "fast": fast,
        "verbose": verbose,
    }
    return "output", "provider", "branch-id", "completed", "sess-001"


async def _fake_run_flow(
    model_spec: str,
    prompt: str,
    *,
    bypass: bool = False,
    yolo: bool = False,
    fast: bool = False,
    verbose: bool = False,
    **kwargs: Any,
) -> tuple[str, str]:
    _CAPTURED["flow"] = {
        "model_spec": model_spec,
        "prompt": prompt,
        "bypass": bypass,
        "yolo": yolo,
        "fast": fast,
        "verbose": verbose,
    }
    return "output", "completed"


async def _fake_run_fanout(
    model_spec: str,
    prompt: str,
    *,
    bypass: bool = False,
    yolo: bool = False,
    fast: bool = False,
    verbose: bool = False,
    **kwargs: Any,
) -> tuple[str, str]:
    _CAPTURED["fanout"] = {
        "model_spec": model_spec,
        "prompt": prompt,
        "bypass": bypass,
        "yolo": yolo,
        "fast": fast,
        "verbose": verbose,
    }
    return "output", "completed"


# Helper: run main() with argv (strips 'uv run li' wrapper from build_argv output)


def _run_main_with_argv(argv: list[str]) -> int:
    """Call lionhub.serve.cli.main.main(argv) with run functions patched."""
    import lionhub.serve.cli.agent as agent_mod
    import lionhub.serve.cli.orchestrate as orch_mod
    import lionhub.serve.cli.orchestrate.fanout as fanout_mod
    import lionhub.serve.cli.orchestrate.flow as flow_mod
    from lionhub.serve.cli.main import main

    with (
        patch.object(agent_mod, "_run_agent", _fake_run_agent),
        patch.object(flow_mod, "_run_flow", _fake_run_flow),
        patch.object(orch_mod, "_run_flow", _fake_run_flow),
        patch.object(fanout_mod, "_run_fanout", _fake_run_fanout),
        patch.object(orch_mod, "_run_fanout", _fake_run_fanout),
    ):
        return main(argv)


def _argv_without_wrapper(argv: list[str]) -> list[str]:
    """Strip the leading 'uv', 'run', 'li' from build_argv output."""
    return argv[3:]


# agent kind

HOSTILE_PROMPTS = ["--bypass", "--yolo", "--fast", "--verbose"]


class TestAgentParserPromptInjection:
    """li agent: hostile action_prompt must arrive as VALUE, not toggle a flag."""

    def _build(self, prompt: str) -> list[str]:
        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "test",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_prompt": prompt,
            "action_agent": None,
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        return _argv_without_wrapper(argv)

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_hostile_prompt_is_value_not_flag(self, hostile: str) -> None:
        _reset()
        argv = self._build(hostile)
        # Sentinel must be present so positionals are protected
        assert "--" in argv, f"'--' sentinel missing from agent argv: {argv}"
        rc = _run_main_with_argv(argv)
        assert rc == 0, f"Expected rc=0 for argv={argv}, got {rc}"
        c = _CAPTURED.get("agent")
        assert c is not None, "run_agent was never called"
        assert c["prompt"] == hostile, (
            f"Hostile prompt {hostile!r} not received as prompt value; "
            f"got prompt={c['prompt']!r}. Likely parsed as a flag."
        )
        assert c["bypass"] is False, f"bypass=True after hostile prompt {hostile!r}"
        assert c["yolo"] is False, f"yolo=True after hostile prompt {hostile!r}"
        assert c["fast"] is False, f"fast=True after hostile prompt {hostile!r}"
        assert c["verbose"] is False, f"verbose=True after hostile prompt {hostile!r}"


class TestFanoutTerminalStatusExitCode:
    """li o fanout must propagate the completion-trust gate's terminal status
    to the process exit code — a demoted `completed_empty` leg exiting 0
    would silently break schedule on_success/on_fail chaining."""

    def _build(self) -> list[str]:
        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "test",
            "action_kind": "fanout",
            "action_model": "sonnet",
            "action_prompt": "do the thing",
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        return _argv_without_wrapper(argv)

    def test_completed_empty_exits_nonzero(self, monkeypatch) -> None:
        _reset()

        async def _fake_run_fanout_empty(*args, **kwargs) -> tuple[str, str]:
            return "output", "completed_empty"

        import lionhub.serve.cli.orchestrate as orch_mod
        import lionhub.serve.cli.orchestrate.fanout as fanout_mod
        from lionhub.orchestration.job import EXIT_CODE_BY_STATUS
        from lionhub.serve.cli.main import main

        monkeypatch.setattr(fanout_mod, "_run_fanout", _fake_run_fanout_empty)
        monkeypatch.setattr(orch_mod, "_run_fanout", _fake_run_fanout_empty)

        rc = main(self._build())
        assert rc == EXIT_CODE_BY_STATUS["completed_empty"]
        assert rc != 0

    def test_completed_exits_zero(self) -> None:
        _reset()
        rc = _run_main_with_argv(self._build())
        assert rc == 0


# flow kind


class TestFlowParserPromptInjection:
    """li o flow: hostile action_prompt must arrive as VALUE, not toggle a flag."""

    def _build(self, prompt: str) -> list[str]:
        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "test",
            "action_kind": "flow",
            "action_model": "sonnet",
            "action_prompt": prompt,
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        return _argv_without_wrapper(argv)

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_hostile_prompt_is_value_not_flag(self, hostile: str) -> None:
        _reset()
        argv = self._build(hostile)
        assert "--" in argv, f"'--' sentinel missing from flow argv: {argv}"
        rc = _run_main_with_argv(argv)
        assert rc == 0, f"Expected rc=0 for argv={argv}, got {rc}"
        c = _CAPTURED.get("flow")
        assert c is not None, "run_flow was never called"
        assert c["prompt"] == hostile, (
            f"Hostile prompt {hostile!r} not received as prompt value; got prompt={c['prompt']!r}."
        )
        assert c["bypass"] is False, f"bypass=True after hostile prompt {hostile!r}"
        assert c["yolo"] is False, f"yolo=True after hostile prompt {hostile!r}"
        assert c["fast"] is False, f"fast=True after hostile prompt {hostile!r}"
        assert c["verbose"] is False, f"verbose=True after hostile prompt {hostile!r}"


# fanout kind


class TestFanoutParserPromptInjection:
    """li o fanout: hostile action_prompt must arrive as VALUE, not toggle a flag."""

    def _build(self, prompt: str) -> list[str]:
        from lionhub.orchestration.scheduler.subprocess import build_argv

        sched = {
            "id": "test",
            "action_kind": "fanout",
            "action_model": "sonnet",
            "action_prompt": prompt,
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        return _argv_without_wrapper(argv)

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_hostile_prompt_is_value_not_flag(self, hostile: str) -> None:
        _reset()
        argv = self._build(hostile)
        assert "--" in argv, f"'--' sentinel missing from fanout argv: {argv}"
        rc = _run_main_with_argv(argv)
        assert rc == 0, f"Expected rc=0 for argv={argv}, got {rc}"
        c = _CAPTURED.get("fanout")
        assert c is not None, "run_fanout was never called"
        assert c["prompt"] == hostile, (
            f"Hostile prompt {hostile!r} not received as prompt value; got prompt={c['prompt']!r}."
        )
        assert c["bypass"] is False, f"bypass=True after hostile prompt {hostile!r}"
        assert c["yolo"] is False, f"yolo=True after hostile prompt {hostile!r}"
        assert c["fast"] is False, f"fast=True after hostile prompt {hostile!r}"
        assert c["verbose"] is False, f"verbose=True after hostile prompt {hostile!r}"


# flow_yaml kind — prompt EXCLUDED from argv


class TestFlowYamlParserPromptExclusion:
    """flow_yaml: hostile action_prompt must not appear in argv; YAML spec supplies the prompt."""

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_yaml_prompt_delivered_bypass_remains_false(self, hostile: str) -> None:
        """run_flow receives YAML prompt; bypass/yolo/fast all stay False."""
        import lionhub.serve.cli.orchestrate as orch_mod
        import lionhub.serve.cli.orchestrate.flow as flow_mod
        from lionhub.orchestration.scheduler.subprocess import build_argv
        from lionhub.serve.cli.main import main

        _reset()

        # Use the generated temp file directly: the spec content is fully
        # determined by action_flow_yaml plus the merged-in action_model.
        sched = {
            "id": "test",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": hostile,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "prompt: yaml-supplied-prompt\n",
        }
        full_argv, tmp_yaml = build_argv(sched, {})
        try:
            cli_argv = _argv_without_wrapper(full_argv)

            with (
                patch.object(flow_mod, "_run_flow", _fake_run_flow),
                patch.object(orch_mod, "_run_flow", _fake_run_flow),
            ):
                rc = main(cli_argv)

            assert rc == 0, f"Expected rc=0 for argv={cli_argv}, got {rc}"
            c = _CAPTURED.get("flow")
            assert c is not None, "run_flow was never called"
            assert c["prompt"] == "yaml-supplied-prompt", (
                f"Expected YAML prompt, got {c['prompt']!r}"
            )
            assert c["model_spec"] == "sonnet", (
                f"Expected the schedule's model to override the spec, got {c['model_spec']!r}"
            )
            assert c["bypass"] is False, (
                f"bypass toggled for flow_yaml with hostile action_prompt {hostile!r}"
            )
            assert c["yolo"] is False, (
                f"yolo toggled for flow_yaml with hostile action_prompt {hostile!r}"
            )
            assert c["fast"] is False, (
                f"fast toggled for flow_yaml with hostile action_prompt {hostile!r}"
            )
        finally:
            if tmp_yaml and os.path.exists(tmp_yaml):
                os.unlink(tmp_yaml)


# Sentinel placement sanity: verify '--' is before positionals in argv shape


# Regression pin: '-- --' and '-- trailing' pass through


# Parser-level: template-rendered sentinel rejection

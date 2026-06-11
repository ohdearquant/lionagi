# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Parser-level regression tests for CWE-88 fix (closes #1404, codex round 2).

These tests take the BUILT argv from build_argv() for each action_kind and run
it through the real lionagi.cli.main.main() parser with terminal run functions
patched.  They assert that hostile action_prompt values (--bypass, --yolo,
--fast, --verbose) arrive as the prompt VALUE rather than toggling the
corresponding boolean flags.

The structural fix: build_argv places a '--' end-of-options sentinel before
positionals for agent/flow/fanout, and drops the prompt positional entirely
for flow_yaml (the YAML file supplies the prompt via spec.get("prompt")).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Shared capture store — reset before each parametrized test invocation
# ---------------------------------------------------------------------------

_CAPTURED: dict[str, Any] = {}


def _reset() -> None:
    _CAPTURED.clear()


# ---------------------------------------------------------------------------
# Fake run functions
# Each captures the positional and keyword arguments it receives so tests can
# assert on flag values without executing any network/agent logic.
# ---------------------------------------------------------------------------


async def _fake_run_agent(
    model_str: str | None,
    prompt: str,
    *,
    bypass: bool = False,
    yolo: bool = False,
    fast: bool = False,
    verbose: bool = False,
    **kwargs: Any,
) -> tuple[str, str, str, str]:
    _CAPTURED["agent"] = {
        "model_str": model_str,
        "prompt": prompt,
        "bypass": bypass,
        "yolo": yolo,
        "fast": fast,
        "verbose": verbose,
    }
    return "output", "provider", "branch-id", "completed"


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
) -> str:
    _CAPTURED["fanout"] = {
        "model_spec": model_spec,
        "prompt": prompt,
        "bypass": bypass,
        "yolo": yolo,
        "fast": fast,
        "verbose": verbose,
    }
    return "output"


# ---------------------------------------------------------------------------
# Helper: run main() with argv (strips 'uv run li' wrapper from build_argv output)
# ---------------------------------------------------------------------------


def _run_main_with_argv(argv: list[str]) -> int:
    """Call lionagi.cli.main.main(argv) with run functions patched."""
    import lionagi.cli.agent as agent_mod
    import lionagi.cli.orchestrate as orch_mod
    import lionagi.cli.orchestrate.fanout as fanout_mod
    import lionagi.cli.orchestrate.flow as flow_mod
    from lionagi.cli.main import main

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


# ---------------------------------------------------------------------------
# agent kind
# ---------------------------------------------------------------------------

HOSTILE_PROMPTS = ["--bypass", "--yolo", "--fast", "--verbose"]


class TestAgentParserPromptInjection:
    """li agent: hostile action_prompt must arrive as VALUE, not toggle a flag."""

    def _build(self, prompt: str) -> list[str]:
        from lionagi.studio.scheduler.subprocess import build_argv

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


# ---------------------------------------------------------------------------
# flow kind
# ---------------------------------------------------------------------------


class TestFlowParserPromptInjection:
    """li o flow: hostile action_prompt must arrive as VALUE, not toggle a flag."""

    def _build(self, prompt: str) -> list[str]:
        from lionagi.studio.scheduler.subprocess import build_argv

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


# ---------------------------------------------------------------------------
# fanout kind
# ---------------------------------------------------------------------------


class TestFanoutParserPromptInjection:
    """li o fanout: hostile action_prompt must arrive as VALUE, not toggle a flag."""

    def _build(self, prompt: str) -> list[str]:
        from lionagi.studio.scheduler.subprocess import build_argv

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


# ---------------------------------------------------------------------------
# flow_yaml kind — prompt EXCLUDED from argv
# ---------------------------------------------------------------------------


class TestFlowYamlParserPromptExclusion:
    """flow_yaml: hostile action_prompt must not appear in argv at all.

    The YAML spec file supplies the prompt (spec.get('prompt') overwrites
    args.prompt at orchestrate/__init__.py ~line 431).  The built argv must
    not contain the hostile string, and bypass/yolo/fast must all be False
    when run through the real parser.
    """

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_hostile_prompt_absent_from_argv(self, hostile: str) -> None:
        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "test",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": hostile,
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "prompt: yaml-supplied\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            assert hostile not in argv, (
                f"Hostile action_prompt {hostile!r} must not appear in flow_yaml "
                f"argv at all. Got: {argv}"
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_yaml_prompt_delivered_bypass_remains_false(self, hostile: str) -> None:
        """run_flow receives YAML prompt; bypass/yolo/fast all stay False."""
        import lionagi.cli.orchestrate as orch_mod
        import lionagi.cli.orchestrate.flow as flow_mod
        from lionagi.cli.main import main
        from lionagi.studio.scheduler.subprocess import build_argv

        _reset()

        # Write a controlled YAML file so the content is predictable.
        fd, tmp_yaml = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-test-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("prompt: yaml-supplied-prompt\n")

            sched = {
                "id": "test",
                "action_kind": "flow_yaml",
                "action_model": "sonnet",
                "action_prompt": hostile,
                "action_project": None,
                "action_extra_args": [],
                "action_flow_yaml": "prompt: yaml-supplied-prompt\n",
            }
            full_argv, gen_tmp = build_argv(sched, {})
            # Discard the generated temp file; substitute our controlled one.
            if gen_tmp and os.path.exists(gen_tmp):
                os.unlink(gen_tmp)

            # Swap -f <generated> → -f <our yaml>
            f_idx = full_argv.index("-f")
            full_argv[f_idx + 1] = tmp_yaml

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
            if os.path.exists(tmp_yaml):
                os.unlink(tmp_yaml)


# ---------------------------------------------------------------------------
# Sentinel placement sanity: verify '--' is before positionals in argv shape
# ---------------------------------------------------------------------------


class TestSentinelPlacement:
    """Verify '--' appears before positionals in each action kind's argv."""

    def test_agent_sentinel_before_prompt(self) -> None:
        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "t",
            "action_kind": "agent",
            "action_model": "sonnet",
            "action_prompt": "hello",
            "action_agent": None,
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1] == "sonnet", (
            f"Expected model 'sonnet' after '--', got {argv[sep_idx + 1]!r}. Full argv: {argv}"
        )
        assert argv[sep_idx + 2] == "hello", (
            f"Expected prompt 'hello' after '--', got {argv[sep_idx + 2]!r}. Full argv: {argv}"
        )

    def test_flow_sentinel_before_prompt(self) -> None:
        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "t",
            "action_kind": "flow",
            "action_model": "sonnet",
            "action_prompt": "hello",
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1] == "sonnet"
        assert argv[sep_idx + 2] == "hello"

    def test_fanout_sentinel_before_prompt(self) -> None:
        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "t",
            "action_kind": "fanout",
            "action_model": "sonnet",
            "action_prompt": "hello",
            "action_project": None,
            "action_extra_args": [],
        }
        argv, _ = build_argv(sched, {})
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1] == "sonnet"
        assert argv[sep_idx + 2] == "hello"

    def test_flow_yaml_no_prompt_positional(self) -> None:
        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "t",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": "--bypass",  # hostile
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "prompt: p\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            assert "--bypass" not in argv, (
                f"Hostile prompt must not appear in flow_yaml argv: {argv}"
            )
            # Only model after sentinel — no prompt positional
            sep_idx = argv.index("--")
            after = argv[sep_idx + 1 :]
            assert after == ["sonnet"], (
                f"Expected only ['sonnet'] after '--' in flow_yaml, got {after}"
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_flow_yaml_file_flag_before_sentinel(self) -> None:
        from lionagi.studio.scheduler.subprocess import build_argv

        sched = {
            "id": "t",
            "action_kind": "flow_yaml",
            "action_model": "sonnet",
            "action_prompt": "irrelevant",
            "action_project": None,
            "action_extra_args": [],
            "action_flow_yaml": "prompt: p\n",
        }
        argv, tmp_path = build_argv(sched, {})
        try:
            f_idx = argv.index("-f")
            sep_idx = argv.index("--")
            assert f_idx < sep_idx, (
                f"-f ({f_idx}) must come before '--' ({sep_idx}). "
                f"Otherwise argparse may reject -f as an unrecognised positional. "
                f"argv={argv}"
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

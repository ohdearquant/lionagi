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


# agent kind

HOSTILE_PROMPTS = ["--bypass", "--yolo", "--fast", "--verbose"]


# flow kind


# fanout kind


# flow_yaml kind — prompt EXCLUDED from argv


class TestFlowYamlParserPromptExclusion:
    """flow_yaml: hostile action_prompt must not appear in argv; YAML spec supplies the prompt."""

    @pytest.mark.parametrize("hostile", HOSTILE_PROMPTS)
    def test_hostile_prompt_absent_from_argv(self, hostile: str) -> None:
        from lionhub.orchestration.scheduler.subprocess import build_argv

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


# Sentinel placement sanity: verify '--' is before positionals in argv shape


class TestSentinelPlacement:
    """Verify '--' appears before positionals in each action kind's argv."""

    def test_agent_sentinel_before_prompt(self) -> None:
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
        from lionhub.orchestration.scheduler.subprocess import build_argv

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
            # No positionals at all: the model is merged into the spec file
            # and the YAML supplies the prompt.
            sep_idx = argv.index("--")
            after = argv[sep_idx + 1 :]
            assert after == [], f"Expected nothing after '--' in flow_yaml, got {after}"
            assert "sonnet" not in argv, f"model leaked into argv: {argv}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_flow_yaml_file_flag_before_sentinel(self) -> None:
        from lionhub.orchestration.scheduler.subprocess import build_argv

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


# Regression pin: '-- --' and '-- trailing' pass through


class TestDoubleDashSentinelEdgeCases:
    """Pin that '-- --' and '-- trailing' reach the runner as prompt values (multi-token forms permitted)."""

    def test_double_dash_space_double_dash_passes_through(self) -> None:
        """action_prompt='-- --' must reach _run_agent as the prompt VALUE."""
        _reset()
        argv = _argv_without_wrapper(
            __import__(
                "lionhub.orchestration.scheduler.subprocess", fromlist=["build_argv"]
            ).build_argv(
                {
                    "id": "t",
                    "action_kind": "agent",
                    "action_model": "sonnet",
                    "action_prompt": "-- --",
                    "action_agent": None,
                    "action_project": None,
                    "action_extra_args": [],
                },
                {},
            )[0]
        )
        rc = _run_main_with_argv(argv)
        assert rc == 0, f"Expected rc=0, got {rc}. argv={argv}"
        c = _CAPTURED.get("agent")
        assert c is not None, "run_agent was not called"
        assert c["prompt"] == "-- --", f"Expected prompt='-- --', got {c['prompt']!r}"
        assert c["bypass"] is False

    def test_double_dash_trailing_text_passes_through(self) -> None:
        """action_prompt='-- some text' must reach _run_agent as the prompt VALUE."""
        _reset()
        from lionhub.orchestration.scheduler.subprocess import build_argv

        full_argv, _ = build_argv(
            {
                "id": "t",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_prompt": "-- some trailing",
                "action_agent": None,
                "action_project": None,
                "action_extra_args": [],
            },
            {},
        )
        argv = _argv_without_wrapper(full_argv)
        rc = _run_main_with_argv(argv)
        assert rc == 0, f"Expected rc=0, got {rc}. argv={argv}"
        c = _CAPTURED.get("agent")
        assert c is not None, "run_agent was not called"
        assert c["prompt"] == "-- some trailing", (
            f"Expected prompt='-- some trailing', got {c['prompt']!r}"
        )
        assert c["bypass"] is False

    def test_exact_double_dash_rejected_at_build_time(self) -> None:
        """action_prompt='--' must be rejected by build_argv before reaching argparse."""
        import pytest as _pytest

        from lionhub.orchestration.scheduler.subprocess import build_argv

        with _pytest.raises(ValueError, match="'--'"):
            build_argv(
                {
                    "id": "t",
                    "action_kind": "agent",
                    "action_model": "sonnet",
                    "action_prompt": "--",
                    "action_agent": None,
                    "action_project": None,
                    "action_extra_args": [],
                },
                {},
            )


# Parser-level: template-rendered sentinel rejection


class TestTemplateRenderedSentinelRejection:
    """build_argv must validate action_prompt AFTER template rendering so '{{payload}}'+sentinel fails."""

    def test_template_payload_renders_sentinel_agent_raises(self) -> None:
        import pytest as _pytest

        from lionhub.orchestration.scheduler.subprocess import build_argv

        with _pytest.raises(ValueError, match="'--'"):
            build_argv(
                {
                    "id": "t",
                    "action_kind": "agent",
                    "action_model": "sonnet",
                    "action_prompt": "{{payload}}",
                    "action_agent": None,
                    "action_project": None,
                    "action_extra_args": [],
                },
                {"payload": "--"},
            )

    def test_template_payload_renders_sentinel_flow_raises(self) -> None:
        import pytest as _pytest

        from lionhub.orchestration.scheduler.subprocess import build_argv

        with _pytest.raises(ValueError, match="'--'"):
            build_argv(
                {
                    "id": "t",
                    "action_kind": "flow",
                    "action_model": "sonnet",
                    "action_prompt": "{{payload}}",
                    "action_agent": None,
                    "action_project": None,
                    "action_extra_args": [],
                },
                {"payload": "--"},
            )

    def test_template_payload_renders_sentinel_fanout_raises(self) -> None:
        import pytest as _pytest

        from lionhub.orchestration.scheduler.subprocess import build_argv

        with _pytest.raises(ValueError, match="'--'"):
            build_argv(
                {
                    "id": "t",
                    "action_kind": "fanout",
                    "action_model": "sonnet",
                    "action_prompt": "{{payload}}",
                    "action_agent": None,
                    "action_project": None,
                    "action_extra_args": [],
                },
                {"payload": "--"},
            )

    def test_template_payload_safe_reaches_parser_correctly(self) -> None:
        """'{{payload}}' + {"payload": "hello"} renders to 'hello' and builds valid argv."""
        from lionhub.orchestration.scheduler.subprocess import build_argv

        full_argv, _ = build_argv(
            {
                "id": "t",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_prompt": "{{payload}}",
                "action_agent": None,
                "action_project": None,
                "action_extra_args": [],
            },
            {"payload": "hello world"},
        )
        # The rendered prompt must appear after the '--' sentinel
        cli_argv = full_argv[3:]  # strip 'uv run li'
        sep_idx = cli_argv.index("--")
        positionals = cli_argv[sep_idx + 1 :]
        assert "hello world" in positionals, (
            f"Expected rendered prompt 'hello world' in positionals {positionals!r}"
        )

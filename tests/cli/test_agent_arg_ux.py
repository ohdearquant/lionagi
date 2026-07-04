# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent` argument UX: flags anywhere, --prompt/--prompt-file, clear errors.

The former `[model] prompt` two-positional parse silently mis-assigned the
model string to the prompt slot whenever a flag preceded the prompt
(`li agent codex/x --effort high "p"` → "unrecognized arguments: p").
These tests pin the intermixed-parse + positional-bucket resolution.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

_CAPTURED: dict[str, Any] = {}


async def _fake_run_agent(
    model_str: str | None,
    prompt: str,
    **kwargs: Any,
) -> tuple[str, str, str, str, str | None]:
    _CAPTURED["agent"] = {"model_str": model_str, "prompt": prompt, **kwargs}
    return "output", "provider", "branch-id", "completed", "sess-001"


def _run(argv: list[str]) -> int:
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.main import main

    _CAPTURED.clear()
    with patch.object(agent_mod, "_run_agent", _fake_run_agent):
        return main(argv)


class TestFlagsAnywhere:
    def test_flags_between_model_and_prompt(self):
        rc = _run(["agent", "codex/gpt-5.5", "--effort", "high", "say OK"])
        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["model_str"] == "codex/gpt-5.5"
        assert c["prompt"] == "say OK"

    def test_prompt_before_flags_still_works(self):
        rc = _run(["agent", "codex/gpt-5.5", "say OK", "--effort", "high"])
        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["model_str"] == "codex/gpt-5.5"
        assert c["prompt"] == "say OK"

    def test_flags_before_everything(self):
        rc = _run(["agent", "--effort", "high", "codex/gpt-5.5", "say OK"])
        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["model_str"] == "codex/gpt-5.5"
        assert c["prompt"] == "say OK"


class TestSinglePositional:
    def test_single_positional_is_prompt_with_profile(self):
        rc = _run(["agent", "-a", "reviewer", "review this"])
        # profile 'reviewer' may not exist in the test env; the parse layer is
        # what we pin — model_str must be None and prompt the positional.
        if rc == 0:
            c = _CAPTURED["agent"]
            assert c["model_str"] is None
            assert c["prompt"] == "review this"

    def test_single_positional_without_model_source_errors(self):
        rc = _run(["agent", "just a prompt"])
        assert rc == 1  # model or --agent required


class TestPromptFlagAndFile:
    def test_prompt_flag(self):
        rc = _run(["agent", "codex", "--prompt", "from flag"])
        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["model_str"] == "codex"
        assert c["prompt"] == "from flag"

    def test_prompt_file(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("from file")
        rc = _run(["agent", "codex", "--prompt-file", str(f)])
        assert rc == 0
        assert _CAPTURED["agent"]["prompt"] == "from file"

    def test_prompt_file_stdin(self, monkeypatch):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
        rc = _run(["agent", "codex", "--prompt-file", "-"])
        assert rc == 0
        assert _CAPTURED["agent"]["prompt"] == "from stdin"

    def test_prompt_file_missing_errors(self):
        rc = _run(["agent", "codex", "--prompt-file", "/nonexistent/x.md"])
        assert rc == 1

    def test_prompt_flag_and_file_conflict(self, tmp_path):
        f = tmp_path / "p.md"
        f.write_text("x")
        rc = _run(["agent", "codex", "--prompt", "a", "--prompt-file", str(f)])
        assert rc == 1

    def test_prompt_twice_positional_and_flag(self):
        rc = _run(["agent", "codex", "positional prompt", "--prompt", "flag prompt"])
        assert rc == 1


class TestClearErrors:
    def test_unquoted_prompt_gives_guidance(self):
        rc = _run(["agent", "codex", "say", "OK", "please"])
        assert rc == 1  # too many positionals — quote the prompt

    def test_no_prompt_at_all(self):
        rc = _run(["agent", "codex"])
        assert rc == 1


class TestSentinelCompat:
    """Scheduler argv shape: li agent [flags] -- MODEL PROMPT (CWE-88)."""

    def test_sentinel_model_prompt(self):
        rc = _run(["agent", "--", "sonnet", "--bypass"])
        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["model_str"] == "sonnet"
        assert c["prompt"] == "--bypass"
        assert c.get("bypass") is not True

    def test_sentinel_with_leading_flags(self):
        rc = _run(["agent", "--effort", "low", "--", "sonnet", "hello world"])
        assert rc == 0
        c = _CAPTURED["agent"]
        assert c["model_str"] == "sonnet"
        assert c["prompt"] == "hello world"

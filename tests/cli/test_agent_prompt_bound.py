# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The single-agent CLI refuses oversized prompt files before starting a run."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS


async def _fake_run_agent(
    model: str | None, prompt: str, **kwargs: Any
) -> tuple[str, str, str, str, str]:
    return "output", model or "provider", "branch-id", "completed", "session-id"


def _run(argv: list[str]) -> int:
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.main import main

    with patch.object(agent_mod, "_run_agent", _fake_run_agent):
        return main(argv)


def test_agent_prompt_file_over_shared_limit_is_refused_before_run(tmp_path, capsys):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("x" * (MAX_SPEC_PROMPT_CHARS + 1))

    with patch("lionagi.cli.agent._run_agent", new_callable=AsyncMock) as run:
        from lionagi.cli.main import main

        assert main(["agent", "codex", "--prompt-file", str(prompt_file)]) == 1
        run.assert_not_called()

    assert str(MAX_SPEC_PROMPT_CHARS) in capsys.readouterr().err


def test_agent_prompt_at_shared_limit_is_accepted(tmp_path):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("x" * MAX_SPEC_PROMPT_CHARS)

    assert _run(["agent", "codex", "--prompt-file", str(prompt_file)]) == 0

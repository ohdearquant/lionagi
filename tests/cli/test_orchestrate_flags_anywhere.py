# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o flow` / `li o fanout` / `li play` argument UX: flags anywhere.

Mirrors tests/cli/test_agent_arg_ux.py for the equivalent `li agent`
flags-anywhere fix. Before this fix:

- `li o flow --dry-run "prompt"` silently lost the prompt: both `model` and
  `prompt` were separate `nargs="?"` positionals, so argparse's greedy
  left-to-right fill assigned the sole leftover token to `model`, leaving
  `prompt` at its default of None.
- `li o fanout MODEL --flag VALUE "prompt"` hard-rejected with "unrecognized
  arguments": a flag between the two positionals split them into two
  separate positional-matching groups that plain argparse can't reconcile.
- `li play --bypass NAME "prompt"` hard-rejected before even reaching
  argparse, in the `play` → `o flow -p NAME ...` sugar rewrite, which assumed
  NAME was always the first token after `play`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import yaml

import lionagi.cli.orchestrate as orch_mod
from lionagi.cli.main import main


def _run_with_flow_mock(argv: list[str]):
    with patch.object(
        orch_mod, "_run_flow", AsyncMock(return_value=("flow output", "completed"))
    ) as run_flow:
        code = main(argv)
    return code, run_flow


def _run_with_fanout_mock(argv: list[str]):
    with patch.object(
        orch_mod, "_run_fanout", AsyncMock(return_value=("fanout output", "completed"))
    ) as run_fanout:
        code = main(argv)
    return code, run_fanout


class TestFlowFlagsAnywhere:
    def test_flags_before_prompt_previously_lost_it(self):
        """The exact reported regression: --dry-run before the prompt used to
        silently drop it (misassigned to `model`), erroring "prompt is
        required" even though one was given."""
        code, run_flow = _run_with_flow_mock(
            ["o", "flow", "--dry-run", "some prompt", "--agent", "researcher"]
        )
        assert code == 0
        run_flow.assert_called_once()
        assert run_flow.call_args.kwargs["prompt"] == "some prompt"
        assert run_flow.call_args.kwargs["dry_run"] is True

    def test_flags_between_model_and_prompt(self):
        code, run_flow = _run_with_flow_mock(
            ["o", "flow", "codex", "--effort", "high", "do the thing"]
        )
        assert code == 0
        assert run_flow.call_args.kwargs["model_spec"] == "codex"
        assert run_flow.call_args.kwargs["prompt"] == "do the thing"

    def test_prompt_before_flags_still_works(self):
        code, run_flow = _run_with_flow_mock(
            ["o", "flow", "codex", "do the thing", "--effort", "high"]
        )
        assert code == 0
        assert run_flow.call_args.kwargs["model_spec"] == "codex"
        assert run_flow.call_args.kwargs["prompt"] == "do the thing"

    def test_prompt_verbatim_after_flags(self):
        """A prompt containing punctuation/dashes must survive untouched when
        it follows flags, including a leading '--' style token after the '--'
        sentinel (must not be reinterpreted as a flag, CWE-88)."""
        code, run_flow = _run_with_flow_mock(
            ["o", "flow", "--agent", "researcher", "--", "--not-a-real-flag verbatim"]
        )
        assert code == 0
        assert run_flow.call_args.kwargs["prompt"] == "--not-a-real-flag verbatim"

    def test_missing_prompt_still_errors_clearly(self, capsys):
        code = main(["o", "flow", "--agent", "researcher", "--dry-run"])
        assert code == 1
        assert "prompt is required" in capsys.readouterr().err

    def test_missing_model_still_errors_clearly(self, capsys):
        code = main(["o", "flow", "--dry-run", "some prompt"])
        assert code == 1
        assert "model or --agent is required" in capsys.readouterr().err


class TestFanoutFlagsAnywhere:
    def test_flag_between_model_and_prompt_previously_rejected(self):
        """The exact reported disease check: a flag splitting the two
        positionals used to error 'unrecognized arguments' outright."""
        code, run_fanout = _run_with_fanout_mock(
            ["o", "fanout", "codex", "--num-workers", "2", "some prompt"]
        )
        assert code == 0
        run_fanout.assert_called_once()
        assert run_fanout.call_args.kwargs["model_spec"] == "codex"
        assert run_fanout.call_args.kwargs["prompt"] == "some prompt"
        assert run_fanout.call_args.kwargs["num_workers"] == 2

    def test_flags_before_everything(self):
        code, run_fanout = _run_with_fanout_mock(
            ["o", "fanout", "--num-workers", "2", "--agent", "researcher", "some prompt"]
        )
        assert code == 0
        assert run_fanout.call_args.kwargs["prompt"] == "some prompt"
        assert run_fanout.call_args.kwargs["model_spec"] == ""

    def test_prompt_verbatim_after_flags(self):
        code, run_fanout = _run_with_fanout_mock(
            ["o", "fanout", "--agent", "researcher", "--", "--looks-like-a-flag"]
        )
        assert code == 0
        assert run_fanout.call_args.kwargs["prompt"] == "--looks-like-a-flag"

    def test_missing_prompt_errors_clearly(self, capsys):
        code = main(["o", "fanout", "--agent", "researcher"])
        assert code == 1
        assert "prompt is required" in capsys.readouterr().err


class TestPlayFlagsAnywhere:
    def _make_playbook(self, tmp_path, monkeypatch, **spec_fields):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        spec = {"model": "claude-code/opus-4-7", "prompt": "Do {input}"}
        spec.update(spec_fields)
        (playbooks_dir / "hello.playbook.yaml").write_text(yaml.dump(spec))
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_flags_before_name_previously_hard_rejected(self, tmp_path, monkeypatch, capsys):
        """The exact reported regression: `li play --bypass NAME "prompt"`
        used to hard-error with "li play NAME must come before flags"."""
        self._make_playbook(tmp_path, monkeypatch)
        code, run_flow = _run_with_flow_mock(["play", "--bypass", "hello", "a thing"])
        assert code == 0, capsys.readouterr().err
        assert run_flow.call_args.kwargs["prompt"] == "Do a thing"
        assert run_flow.call_args.kwargs["bypass"] is True

    def test_flags_between_name_and_prompt(self, tmp_path, monkeypatch):
        self._make_playbook(tmp_path, monkeypatch)
        code, run_flow = _run_with_flow_mock(["play", "hello", "--bypass", "a thing"])
        assert code == 0
        assert run_flow.call_args.kwargs["prompt"] == "Do a thing"
        assert run_flow.call_args.kwargs["bypass"] is True

    def test_name_first_still_works(self, tmp_path, monkeypatch):
        self._make_playbook(tmp_path, monkeypatch)
        code, run_flow = _run_with_flow_mock(["play", "hello", "a thing", "--bypass"])
        assert code == 0
        assert run_flow.call_args.kwargs["prompt"] == "Do a thing"

    def test_custom_playbook_arg_after_name_unaffected(self, tmp_path, monkeypatch):
        """Playbook arg interpolation (key=value args declared via the
        playbook's own `args:` schema) must keep working when NAME leads."""
        self._make_playbook(
            tmp_path,
            monkeypatch,
            args={"tabs": {"type": "int", "default": 2}},
            prompt="Run {tabs}. Task: {input}",
        )
        code, run_flow = _run_with_flow_mock(["play", "hello", "--tabs", "9", "do a thing"])
        assert code == 0
        assert run_flow.call_args.kwargs["prompt"] == "Run 9. Task: do a thing"

    def test_missing_name_errors_clearly(self, capsys):
        code = main(["play", "--bypass"])
        assert code == 1
        assert "playbook NAME is required" in capsys.readouterr().err

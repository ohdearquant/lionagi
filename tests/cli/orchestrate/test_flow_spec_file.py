# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li o flow -f spec.yaml` file-based flow specification: loading, interpolation, playbooks, and end-to-end dispatch."""

import argparse
import json
from unittest.mock import AsyncMock, patch

import yaml

from lionagi.cli.orchestrate import (
    _coerce_arg_value,
    _interpolate_prompt,
    _load_flow_spec,
    _parse_argument_hint,
    _resolve_playbook_path,
    add_orchestrate_subparser,
    inject_playbook_schema_into_parser,
    run_orchestrate,
)


def _parse_flow_args(argv: list[str]) -> argparse.Namespace:
    """Mimic the real CLI pipeline: pre-scan for playbook → inject flags → parse."""
    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    orch_parsers = add_orchestrate_subparser(subparsers)
    full_argv = ["o", "flow", *argv]
    inject_playbook_schema_into_parser(orch_parsers["flow"], full_argv)
    return parser.parse_args(full_argv)


class TestLoadFlowSpec:
    def test_yaml_spec(self, tmp_path):
        spec = {
            "agent": "orchestrator",
            "team_mode": "ws-terminal",
            "workers": 8,
            "effort": "xhigh",
            "prompt": "Implement the terminal component",
        }
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump(spec))
        result = _load_flow_spec(str(p))
        assert result["agent"] == "orchestrator"
        assert result["workers"] == 8
        assert result["effort"] == "xhigh"
        assert result["prompt"] == "Implement the terminal component"

    def test_json_spec(self, tmp_path):
        spec = {"model": "claude-code/opus-4-7", "prompt": "test task", "bare": True}
        p = tmp_path / "spec.json"
        p.write_text(json.dumps(spec))
        result = _load_flow_spec(str(p))
        assert result["model"] == "claude-code/opus-4-7"
        assert result["prompt"] == "test task"
        assert result["bare"] is True

    def test_yml_extension(self, tmp_path):
        spec = {"prompt": "hello"}
        p = tmp_path / "spec.yml"
        p.write_text(yaml.dump(spec))
        result = _load_flow_spec(str(p))
        assert result["prompt"] == "hello"

    def test_missing_file(self):
        result = _load_flow_spec("/nonexistent/path/spec.yaml")
        assert result is None

    def test_invalid_yaml(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text(": : : invalid")
        result = _load_flow_spec(str(p))
        assert result is None

    def test_empty_yaml(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        result = _load_flow_spec(str(p))
        assert result == {}

    def test_unknown_extension_tries_yaml_first(self, tmp_path):
        spec = {"prompt": "detect format"}
        p = tmp_path / "spec.txt"
        p.write_text(yaml.dump(spec))
        result = _load_flow_spec(str(p))
        assert result["prompt"] == "detect format"

    def test_json_content_with_yaml_extension(self, tmp_path):
        spec = {"model": "codex/gpt-5.5", "prompt": "json in yaml"}
        p = tmp_path / "spec.yaml"
        p.write_text(json.dumps(spec))
        result = _load_flow_spec(str(p))
        assert result == spec

    def test_scalar_spec_returns_none(self, tmp_path, caplog):
        p = tmp_path / "scalar.yaml"
        p.write_text("2\n")
        result = _load_flow_spec(str(p))
        assert result is None
        assert "spec file must contain a YAML/JSON object" in caplog.text

    def test_full_spec_fields(self, tmp_path):
        spec = {
            "agent": "orchestrator",
            "model": "claude-code/opus-4-7",
            "team_mode": "ws-terminal",
            "workers": 8,
            "critic_model": "claude-code/opus-4-7",
            "effort": "xhigh",
            "max_agents": 12,
            "bare": False,
            "dry_run": False,
            "save": "/tmp/flow-out",
            "prompt": "Build a CLI tool",
        }
        p = tmp_path / "full.yaml"
        p.write_text(yaml.dump(spec))
        result = _load_flow_spec(str(p))
        assert result == spec

    def test_lone_positional_overrides_prompt_when_spec_supplies_model(self, tmp_path, capsys):
        p = tmp_path / "spec.yaml"
        p.write_text(yaml.dump({"model": "claude-code/opus-4-7"}))
        args = _parse_flow_args(["-f", str(p), "Write the thing"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("flow output", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        run_flow.assert_called_once()
        assert run_flow.call_args.kwargs["model_spec"] == "claude-code/opus-4-7"
        assert run_flow.call_args.kwargs["prompt"] == "Write the thing"
        assert capsys.readouterr().out.strip() == "flow output"

    def test_positional_prompt_appends_to_file_prompt(self, tmp_path, capsys):
        p = tmp_path / "spec.yaml"
        p.write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "prompt": "Review the codebase for security issues.",
                }
            )
        )
        args = _parse_flow_args(["-f", str(p), "Focus on auth middleware"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        prompt = run_flow.call_args.kwargs["prompt"]
        assert prompt.startswith("Review the codebase for security issues.")
        assert prompt.endswith("Focus on auth middleware")

    def test_positional_prompt_fills_template_placeholder(self, tmp_path, capsys):
        p = tmp_path / "spec.yaml"
        p.write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "prompt": "Audit {input} for OWASP top 10 vulnerabilities.",
                }
            )
        )
        args = _parse_flow_args(["-f", str(p), "the auth service"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        prompt = run_flow.call_args.kwargs["prompt"]
        assert prompt == "Audit the auth service for OWASP top 10 vulnerabilities."

    def test_non_object_spec_fails_with_cli_error(self, tmp_path, caplog):
        p = tmp_path / "list.yaml"
        p.write_text("- item\n")
        args = _parse_flow_args(["-f", str(p)])

        code = run_orchestrate(args)

        assert code == 1
        assert "spec file must contain a YAML/JSON object" in caplog.text


# ── Playbook resolution ─────────────────────────────────────────────


class TestResolvePlaybookPath:
    def test_rejects_path_separator(self):
        p, err = _resolve_playbook_path("subdir/evil")
        assert p is None
        assert "bare identifier" in err

    def test_rejects_hidden_name(self):
        p, err = _resolve_playbook_path(".hidden")
        assert p is None
        assert "bare identifier" in err

    def test_not_found_gives_suggestions(self, monkeypatch, tmp_path):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "empaco.playbook.yaml").write_text("prompt: ok\n")
        (playbooks_dir / "chatgpt.playbook.yaml").write_text("prompt: ok\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        p, err = _resolve_playbook_path("nonexistent")
        assert p is None
        assert "not found" in err
        assert "empaco" in err or "chatgpt" in err

    def test_resolves_existing_playbook(self, monkeypatch, tmp_path):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        target = playbooks_dir / "rewrite.playbook.yaml"
        target.write_text("prompt: test\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        p, err = _resolve_playbook_path("rewrite")
        assert err is None
        assert str(p) == str(target)


# ── argument-hint parser ─────────────────────────────────────────────


class TestArgumentHintParser:
    def test_bool_flag(self):
        schema = _parse_argument_hint("[--poll]")
        assert schema == {"poll": {"type": "bool", "default": False}}

    def test_value_flag(self):
        schema = _parse_argument_hint("[--tabs N]")
        assert schema == {"tabs": {"type": "str", "default": None}}

    def test_mixed_flags(self):
        schema = _parse_argument_hint("[--tabs N] [--poll] [--harvest] [--status]")
        assert set(schema.keys()) == {"tabs", "poll", "harvest", "status"}
        assert schema["tabs"]["type"] == "str"
        assert schema["poll"]["type"] == "bool"
        assert schema["harvest"]["type"] == "bool"
        assert schema["status"]["type"] == "bool"

    def test_dashed_flag_converts_underscores(self):
        schema = _parse_argument_hint("[--dry-run]")
        assert "dry_run" in schema

    def test_empty_hint(self):
        assert _parse_argument_hint("") == {}
        assert _parse_argument_hint(None) == {}


# ── Type coercion ────────────────────────────────────────────────────


class TestCoerceArgValue:
    def test_int_coercion(self):
        v, err = _coerce_arg_value("n", "5", "int")
        assert err is None and v == 5

    def test_int_coercion_fails(self):
        v, err = _coerce_arg_value("n", "abc", "int")
        assert v is None and "int" in err

    def test_bool_coercion(self):
        v, err = _coerce_arg_value("flag", True, "bool")
        assert err is None and v is True

    def test_str_coercion(self):
        v, err = _coerce_arg_value("s", 42, "str")
        assert err is None and v == "42"

    def test_none_passthrough(self):
        v, err = _coerce_arg_value("n", None, "int")
        assert err is None and v is None


# ── Prompt interpolation ─────────────────────────────────────────────


class TestInterpolatePrompt:
    def test_input_placeholder(self):
        out = _interpolate_prompt("Review {input}.", "auth module", {})
        assert out == "Review auth module."

    def test_named_args_interpolated(self):
        out = _interpolate_prompt(
            "Run {tabs} sessions. Poll={poll}.",
            None,
            {"tabs": 5, "poll": True},
        )
        assert out == "Run 5 sessions. Poll=True."

    def test_mixed_input_and_args(self):
        out = _interpolate_prompt(
            "{tabs} agents audit {input}.",
            "the auth service",
            {"tabs": 3},
        )
        assert out == "3 agents audit the auth service."

    def test_no_placeholder_appends_positional(self):
        out = _interpolate_prompt("Fixed prompt body.", "extra context", {})
        assert out == "Fixed prompt body.\n\nextra context"

    def test_no_placeholder_no_positional(self):
        out = _interpolate_prompt("Fixed prompt body.", None, {})
        assert out == "Fixed prompt body."

    def test_missing_placeholder_left_literal(self):
        out = _interpolate_prompt("{missing} thing.", None, {})
        assert out == "{missing} thing."


# ── End-to-end via run_orchestrate ───────────────────────────────────


class TestPlaybookEndToEnd:
    def test_playbook_resolves_and_interpolates(self, monkeypatch, tmp_path, capsys):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "audit.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "args": {
                        "tabs": {
                            "type": "int",
                            "default": 3,
                            "help": "parallel sessions",
                        },
                        "poll": {"type": "bool", "default": False},
                    },
                    "prompt": "Run {tabs} sessions. Poll={poll}. Task: {input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "audit", "--tabs", "7", "--poll", "audit the auth service"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        prompt = run_flow.call_args.kwargs["prompt"]
        assert prompt == "Run 7 sessions. Poll=True. Task: audit the auth service"

    def test_playbook_defaults_used_when_cli_flag_omitted(self, monkeypatch, tmp_path):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "x.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "args": {"tabs": {"type": "int", "default": 3}},
                    "prompt": "Tabs={tabs} Input={input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "x", "work task"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["prompt"] == "Tabs=3 Input=work task"

    def test_argument_hint_fallback(self, monkeypatch, tmp_path):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "y.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "argument-hint": "[--tabs N] [--strict]",
                    "prompt": "T={tabs} S={strict}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "y", "--tabs", "4", "--strict", "task"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        # argument-hint values have str type (not int) — rendered as-is
        assert run_flow.call_args.kwargs["prompt"] == "T=4 S=True"

    def test_playbook_and_file_mutually_exclusive(self, monkeypatch, tmp_path, caplog):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "z.playbook.yaml").write_text(yaml.dump({"prompt": "ok"}))
        monkeypatch.setenv("HOME", str(tmp_path))
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.dump({"prompt": "other"}))
        args = _parse_flow_args(["-p", "z", "-f", str(spec_file)])

        code = run_orchestrate(args)
        assert code == 1
        assert "not both" in caplog.text

    def test_li_play_sugar_rewrites_to_flow(self, monkeypatch, tmp_path):
        from lionagi.cli.main import main as cli_main

        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "hello.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "args": {"tabs": {"type": "int", "default": 2}},
                    "prompt": "Run {tabs}. Task: {input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = cli_main(["play", "hello", "--tabs", "9", "do a thing"])

        assert code == 0
        assert run_flow.call_args.kwargs["prompt"] == "Run 9. Task: do a thing"

    def test_li_play_list_prints_available(self, monkeypatch, tmp_path, capsys):
        from lionagi.cli.main import main as cli_main

        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "alpha.playbook.yaml").write_text("prompt: x\n")
        (playbooks_dir / "beta.playbook.yaml").write_text("prompt: x\n")
        monkeypatch.setenv("HOME", str(tmp_path))

        code = cli_main(["play", "list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_li_play_without_args_usage(self, monkeypatch, tmp_path, capsys):
        from lionagi.cli.main import main as cli_main

        monkeypatch.setenv("HOME", str(tmp_path))
        code = cli_main(["play"])
        assert code == 1
        assert "Usage" in capsys.readouterr().out

    def test_playbook_arg_collision_does_not_leak_into_template(self, monkeypatch, tmp_path):
        """A playbook arg that collides with a built-in flag must NOT have
        its value read from the base argparse default during interpolation.
        The filtered schema (from parser injection) is authoritative.
        """
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        # `save` collides with the built-in --save flag.
        (playbooks_dir / "collider.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "args": {
                        "save": {
                            "type": "str",
                            "default": "PLAYBOOK_DEFAULT",
                            "help": "collides with built-in --save",
                        },
                    },
                    "prompt": "save={save} input={input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        # User passes --save /some/dir (built-in), NOT the playbook arg.
        save_dir = tmp_path / "artifacts"
        args = _parse_flow_args(["-p", "collider", "--save", str(save_dir), "do the thing"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        prompt = run_flow.call_args.kwargs["prompt"]
        # Before fix: prompt interpolated `{save}` to built-in args.save →
        # absolute path. After fix: `save` is filtered out of schema; the
        # placeholder stays literal OR is substituted from the playbook
        # default if run_orchestrate falls through. Assert the built-in
        # --save value did NOT leak into the template.
        assert str(save_dir) not in prompt, (
            "Built-in --save value leaked into template via collision-shadowed playbook arg"
        )

    def test_max_ops_flag_passes_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["claude-code/opus-4-7", "a task", "--max-ops", "12"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["max_ops"] == 12

    def test_max_agents_deprecated_alias_still_works(self, monkeypatch, tmp_path):
        """`--max-agents` must still set max_ops for backward compat."""
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["claude-code/opus-4-7", "a task", "--max-agents", "7"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["max_ops"] == 7

    def test_playbook_max_ops_spec_field(self, monkeypatch, tmp_path):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "capped.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "max_ops": 8,
                    "prompt": "Work on: {input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "capped", "a task"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["max_ops"] == 8

    def test_playbook_max_agents_deprecated_spec_still_works(self, monkeypatch, tmp_path):
        """Playbooks with legacy `max_agents:` field must still cap ops."""
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "legacy.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "max_agents": 5,
                    "prompt": "Do: {input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "legacy", "task"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["max_ops"] == 5

    def test_team_attach_flag_passes_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(
            [
                "claude-code/opus-4-7",
                "a task",
                "--team-attach",
                "review",
            ]
        )

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["team_attach"] == "review"
        # --team-mode should not be set
        assert run_flow.call_args.kwargs["team_name"] is None

    def test_team_mode_and_team_attach_mutex(self, monkeypatch, tmp_path):
        import logging

        err_logger = logging.getLogger("lionagi.cli.error")
        err_logger.handlers.clear()
        err_logger.propagate = True

        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(
            [
                "claude-code/opus-4-7",
                "a task",
                "--team-mode",
                "fresh",
                "--team-attach",
                "persistent",
            ]
        )

        code = run_orchestrate(args)
        assert code == 1

    def test_playbook_team_attach_field(self, monkeypatch, tmp_path):
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "chat.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "claude-code/opus-4-7",
                    "team_attach": "ongoing-chat",
                    "prompt": "Respond to: {input}",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "chat", "what time is it"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("ok", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert run_flow.call_args.kwargs["team_attach"] == "ongoing-chat"

    def test_team_attach_upsert_semantics(self, monkeypatch, tmp_path):
        """First attach creates; second attach by same name reuses same id.

        Validates at the _load_team lookup level (not full flow execution).
        """
        from lionagi.cli import team as team_module
        from lionagi.cli.orchestrate._common import _create_fanout_team

        # Point TEAMS_DIR at our scratch.
        teams_dir = tmp_path / "teams"
        teams_dir.mkdir()
        monkeypatch.setattr(team_module, "TEAMS_DIR", teams_dir)

        # First "attach" to non-existent → create.
        data1 = _create_fanout_team("review", ["orchestrator", "worker-1"])
        first_id = data1["id"]

        # Second "attach" → _load_team by name returns same record.
        data2 = team_module._load_team("review")
        assert data2["id"] == first_id
        assert data2["name"] == "review"

    def test_playbook_not_found_errors(self, monkeypatch, tmp_path):
        # Reset the error logger so caplog can capture through root propagation.
        import logging

        err_logger = logging.getLogger("lionagi.cli.error")
        err_logger.handlers.clear()
        err_logger.propagate = True

        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "ghost"])

        code = run_orchestrate(args)
        assert code == 1


# ── ADR-0064: artifacts: block pass-through ───────────────────────────────────


class TestPlaybookArtifactsPassThrough:
    def test_artifacts_block_passed_to_run_flow(self, monkeypatch, tmp_path):
        """A playbook with artifacts: block passes playbook_artifacts to _run_flow."""
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "research.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "codex/gpt-4o",
                    "prompt": "Research the topic.",
                    "artifacts": {
                        "expected": [
                            {"id": "report", "path": "report.md"},
                        ]
                    },
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "research", "do it"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        call_kwargs = run_flow.call_args.kwargs
        pa = call_kwargs.get("playbook_artifacts")
        assert pa is not None
        assert isinstance(pa, dict)
        assert pa["expected"][0]["id"] == "report"

    def test_no_artifacts_block_passes_none(self, monkeypatch, tmp_path):
        """A playbook without artifacts: passes None to _run_flow."""
        playbooks_dir = tmp_path / ".lionagi" / "playbooks"
        playbooks_dir.mkdir(parents=True)
        (playbooks_dir / "basic.playbook.yaml").write_text(
            yaml.dump(
                {
                    "model": "codex/gpt-4o",
                    "prompt": "Do the task.",
                }
            )
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        args = _parse_flow_args(["-p", "basic", "do it"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        call_kwargs = run_flow.call_args.kwargs
        pa = call_kwargs.get("playbook_artifacts")
        assert pa is None

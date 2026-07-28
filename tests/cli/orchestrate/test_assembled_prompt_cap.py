# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The prompt a run receives is not always the prompt that was measured.

A spec file's prompt is a template. What reaches the run is that template with
the caller's positional and the playbook's arguments substituted in, and both
of those arrive after the spec was validated — so a template comfortably under
the bound can carry a finished prompt of any size. A caller who names no spec
at all never meets the spec validator in the first place, and `li o fanout`
has no spec flag to meet it with.

Every test here is built so the template alone passes: an over-length template
would be refused by the spec check that already existed and would prove
nothing. Each one carries an under-bound assembly in the same test, because a
check that refuses everything also turns the over-length cases green.
"""

from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import yaml

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.cli.orchestrate import (
    add_orchestrate_subparser,
    inject_playbook_schema_into_parser,
    run_orchestrate,
)

# Short enough that the template is never the thing that fails.
TEMPLATE_BODY = "t" * 1000


def _parse_args(subcommand: str, argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="li")
    subparsers = parser.add_subparsers(dest="command", required=True)
    orch_parsers = add_orchestrate_subparser(subparsers)
    full_argv = ["o", subcommand, *argv]
    inject_playbook_schema_into_parser(orch_parsers["flow"], full_argv)
    return parser.parse_args(full_argv)


def _isolate(monkeypatch, tmp_path):
    """Nothing this suite runs may reach the real ~/.lionagi, and playbook
    discovery walks up from cwd as well as from home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _write_playbook(tmp_path, name: str, spec: dict) -> None:
    playbooks_dir = tmp_path / ".lionagi" / "playbooks"
    playbooks_dir.mkdir(parents=True, exist_ok=True)
    (playbooks_dir / f"{name}.playbook.yaml").write_text(yaml.dump(spec))


def _refused(caplog) -> bool:
    return "assembled prompt exceeds maximum length" in caplog.text


class TestFlowSpecInterpolation:
    """The template passes the spec check; the argument pushes the result past
    the bound after it."""

    def test_positional_appended_to_a_placeholderless_template_is_refused(
        self, monkeypatch, tmp_path, caplog
    ):
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.dump({"prompt": TEMPLATE_BODY}))
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["-f", str(spec_file), "y" * MAX_SPEC_PROMPT_CHARS])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 1
        assert _refused(caplog)
        assert run_flow.call_count == 0

    def test_the_same_template_with_a_short_positional_still_runs(
        self, monkeypatch, tmp_path, caplog
    ):
        """Positive control for the case above: identical template, ordinary
        argument. A check that refuses every assembly fails here."""
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.dump({"prompt": TEMPLATE_BODY}))
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["-f", str(spec_file), "audit the auth service"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert not _refused(caplog)
        assert run_flow.call_args.kwargs["prompt"].endswith("audit the auth service")

    def test_substituted_placeholder_is_refused(self, monkeypatch, tmp_path, caplog):
        """The other growth path: the positional lands inside {input} rather
        than being concatenated onto the end."""
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.dump({"prompt": f"{TEMPLATE_BODY} Task: {{input}}"}))
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["-f", str(spec_file), "y" * MAX_SPEC_PROMPT_CHARS])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 1
        assert _refused(caplog)
        assert run_flow.call_count == 0

    def test_a_playbook_argument_can_overflow_the_template_too(self, monkeypatch, tmp_path, caplog):
        """Growth does not need the positional: a declared playbook argument
        is substituted into the same template after the same check."""
        _write_playbook(
            tmp_path,
            "audit",
            {"args": {"scope": {"type": "str", "default": "auth"}}, "prompt": "Scope: {scope}"},
        )
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["-p", "audit", "--scope", "y" * MAX_SPEC_PROMPT_CHARS, "go"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 1
        assert _refused(caplog)
        assert run_flow.call_count == 0

    def test_the_same_playbook_with_an_ordinary_argument_still_runs(
        self, monkeypatch, tmp_path, caplog
    ):
        """Positive control for the playbook-argument case."""
        _write_playbook(
            tmp_path,
            "audit",
            {"args": {"scope": {"type": "str", "default": "auth"}}, "prompt": "Scope: {scope}"},
        )
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["-p", "audit", "--scope", "billing", "go"])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert not _refused(caplog)
        assert run_flow.call_args.kwargs["prompt"] == "Scope: billing"

    def test_the_error_names_the_bound_and_the_length(self, monkeypatch, tmp_path, caplog):
        """A refusal that does not say how far over it went leaves the caller
        guessing at what to cut."""
        spec_file = tmp_path / "spec.yaml"
        spec_file.write_text(yaml.dump({"prompt": TEMPLATE_BODY}))
        _isolate(monkeypatch, tmp_path)
        overflow = "y" * MAX_SPEC_PROMPT_CHARS
        args = _parse_args("flow", ["-f", str(spec_file), overflow])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ):
            code = run_orchestrate(args)

        assert code == 1
        assembled_len = len(TEMPLATE_BODY) + 2 + len(overflow)
        assert str(MAX_SPEC_PROMPT_CHARS) in caplog.text
        assert str(assembled_len) in caplog.text


class TestNoSpecFile:
    """Same input, no spec file: before this check the two invocation forms
    disagreed about whether the same text was bounded."""

    def test_inline_flow_prompt_is_refused(self, monkeypatch, tmp_path, caplog):
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["y" * (MAX_SPEC_PROMPT_CHARS + 1)])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 1
        assert _refused(caplog)
        assert run_flow.call_count == 0

    def test_an_inline_flow_prompt_at_the_bound_still_runs(self, monkeypatch, tmp_path, caplog):
        """Positive control, and the boundary: exactly at the bound is allowed,
        matching what the spec check has always accepted."""
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("flow", ["y" * MAX_SPEC_PROMPT_CHARS])

        with patch(
            "lionagi.cli.orchestrate._run_flow",
            AsyncMock(return_value=("done", "completed")),
        ) as run_flow:
            code = run_orchestrate(args)

        assert code == 0
        assert not _refused(caplog)
        assert len(run_flow.call_args.kwargs["prompt"]) == MAX_SPEC_PROMPT_CHARS


class TestFanout:
    """`li o fanout` has no spec flag at all, so its prompt had never met a
    bound on any path."""

    def test_inline_fanout_prompt_is_refused(self, monkeypatch, tmp_path, caplog):
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("fanout", ["y" * (MAX_SPEC_PROMPT_CHARS + 1)])

        with patch(
            "lionagi.cli.orchestrate._run_fanout",
            AsyncMock(return_value=("done", "completed")),
        ) as run_fanout:
            code = run_orchestrate(args)

        assert code == 1
        assert _refused(caplog)
        assert run_fanout.call_count == 0

    def test_an_ordinary_fanout_prompt_still_runs(self, monkeypatch, tmp_path, caplog):
        """Positive control."""
        _isolate(monkeypatch, tmp_path)
        args = _parse_args("fanout", ["review the parser"])

        with patch(
            "lionagi.cli.orchestrate._run_fanout",
            AsyncMock(return_value=("done", "completed")),
        ) as run_fanout:
            code = run_orchestrate(args)

        assert code == 0
        assert not _refused(caplog)
        assert run_fanout.call_args.kwargs["prompt"] == "review the parser"

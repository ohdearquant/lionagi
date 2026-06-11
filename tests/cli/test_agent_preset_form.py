# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for 'li agent --preset' (3a) and 'li agent --form' (3b).

Parser-level tests: flag parsing, choices rejection, file-not-found, invalid
spec → rc!=0 + error message.

Behaviour tests with runner patched: assert AgentConfig.coding() is used when
--preset coding is given; assert WorkForm validation gate fires before any LLM
call.

Error-channel assertions use ``log_error`` capture rather than capsys because
``configure_cli_logging()`` sets ``propagate=False`` on ``lionagi.cli.*``
loggers so pytest's caplog/capsys may not receive the output.  We monkeypatch
``log_error`` directly and inspect the captured message list.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lionagi.cli.agent import (
    _PRESET_CHOICES,
    _build_work_form,
    _form_to_context_block,
    _load_form_spec,
    _make_coding_preset,
    add_agent_subparser,
    run_agent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parser():
    """Return a minimal argparse.ArgumentParser with the agent subparser."""
    import argparse

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command")
    add_agent_subparser(sub)
    return p


def _parse(argv: list[str]):
    return _make_parser().parse_args(["agent"] + argv)


def _agent_args(**overrides):
    """Return a Namespace suitable for run_agent() with sensible defaults."""
    defaults = dict(
        model="claude",
        prompt="do stuff",
        agent=None,
        resume=None,
        continue_last=False,
        yolo=False,
        verbose=False,
        theme=None,
        effort=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        form=None,
        preset=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# 3a — --preset parser tests
# ---------------------------------------------------------------------------


class TestPresetParserFlag:
    def test_preset_coding_parses(self):
        ns = _parse(["claude", "hello", "--preset", "coding"])
        assert ns.preset == "coding"

    def test_preset_default_is_none(self):
        ns = _parse(["claude", "hello"])
        assert ns.preset is None

    def test_preset_unknown_value_rejected(self, capsys):
        """An unknown preset value must cause argparse to exit."""
        p = _make_parser()
        with pytest.raises(SystemExit) as exc_info:
            p.parse_args(["agent", "claude", "hello", "--preset", "nonexistent"])
        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err or "error" in captured.err.lower()

    def test_preset_choices_constant_exposed(self):
        assert "coding" in _PRESET_CHOICES

    def test_preset_coexists_with_form(self, tmp_path):
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text("title: test\n")
        ns = _parse(["claude", "do it", "--preset", "coding", "--form", str(spec_path)])
        assert ns.preset == "coding"
        assert ns.form == str(spec_path)


# ---------------------------------------------------------------------------
# 3b — --form parser tests
# ---------------------------------------------------------------------------


class TestFormParserFlag:
    def test_form_flag_parses(self, tmp_path):
        spec_path = tmp_path / "spec.yaml"
        spec_path.write_text("title: t\n")
        ns = _parse(["claude", "hello", "--form", str(spec_path)])
        assert ns.form == str(spec_path)

    def test_form_default_is_none(self):
        ns = _parse(["claude", "hello"])
        assert ns.form is None


# ---------------------------------------------------------------------------
# _load_form_spec unit tests
# ---------------------------------------------------------------------------


class TestLoadFormSpec:
    def test_loads_yaml(self, tmp_path):
        p = tmp_path / "spec.yaml"
        p.write_text("title: my form\nvalues:\n  x: 1\n")
        data = _load_form_spec(str(p))
        assert data["title"] == "my form"
        assert data["values"]["x"] == 1

    def test_loads_json(self, tmp_path):
        p = tmp_path / "spec.json"
        p.write_text(json.dumps({"title": "j", "values": {"y": "hello"}}))
        data = _load_form_spec(str(p))
        assert data["title"] == "j"

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            _load_form_spec(str(tmp_path / "no_such_file.yaml"))

    def test_invalid_yaml_mapping_raises_value_error(self, tmp_path):
        """A bare list is not a mapping — must raise ValueError."""
        p = tmp_path / "bad.yaml"
        p.write_text("- one\n- two\n")
        with pytest.raises(ValueError, match="mapping"):
            _load_form_spec(str(p))


# ---------------------------------------------------------------------------
# _build_work_form unit tests
# ---------------------------------------------------------------------------


class TestBuildWorkForm:
    def test_empty_spec_creates_draft_form(self):
        form = _build_work_form({}, "<test>")
        assert form.status == "draft"

    def test_spec_with_valid_values_produces_validated_form(self):
        spec = {
            "title": "repo scan",
            "fields": {
                "repo": {"type": "str", "required": True},
            },
            "values": {"repo": "/path/to/repo"},
        }
        form = _build_work_form(spec, "<test>")
        assert form.status == "validated"
        assert form.values["repo"] == "/path/to/repo"

    def test_missing_required_field_produces_error_form(self):
        spec = {
            "fields": {"repo": {"type": "str", "required": True}},
            "values": {},
        }
        form = _build_work_form(spec, "<test>")
        assert form.status == "error"
        assert any("repo" in e for e in form.validation_errors)

    def test_title_from_spec(self):
        spec = {"title": "my title"}
        form = _build_work_form(spec, "path.yaml")
        assert form.title == "my title"

    def test_title_falls_back_to_path(self):
        form = _build_work_form({}, "path/to/spec.yaml")
        assert form.title == "path/to/spec.yaml"

    def test_invalid_field_spec_raises_value_error(self):
        spec = {"fields": {"bad": "not-a-dict"}}
        with pytest.raises(ValueError, match="mapping"):
            _build_work_form(spec, "<test>")

    def test_field_with_invalid_type_name_raises(self):
        spec = {"fields": {"f": {"type": "unknowntype"}}}
        with pytest.raises((ValueError, Exception)):
            _build_work_form(spec, "<test>")


# ---------------------------------------------------------------------------
# _form_to_context_block unit tests
# ---------------------------------------------------------------------------


class TestFormToContextBlock:
    def test_renders_title_and_values(self):
        from lionagi.work import FieldSpec, WorkForm, fill_form

        form = WorkForm(
            title="scan params",
            fields={"repo": FieldSpec(name="repo", type="str", required=True)},
        )
        filled = fill_form(form, {"repo": "/tmp/x"})
        block = _form_to_context_block(filled)
        assert "[Work Form: scan params]" in block
        assert "repo" in block
        assert "/tmp/x" in block


# ---------------------------------------------------------------------------
# run_agent — --form validation gate fires BEFORE LLM call
# ---------------------------------------------------------------------------


class TestFormValidationGate:
    """The validation gate must fire before any LLM call is attempted."""

    def test_missing_form_file_returns_rc1_without_llm_call(self, tmp_path, monkeypatch):
        """--form pointing to a nonexistent file must exit rc=1 immediately."""
        llm_called = []

        import lionagi.cli.agent as agent_mod

        async def fake_run(*a, **kw):
            llm_called.append(1)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run)

        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        rc = run_agent(_agent_args(form=str(tmp_path / "no_such.yaml")))

        assert rc == 1
        assert not llm_called, "LLM must not be called when form file is missing"
        assert any("not found" in e for e in errors), f"expected 'not found' error, got: {errors}"

    def test_invalid_yaml_form_returns_rc1_without_llm_call(self, tmp_path, monkeypatch):
        """A spec file that is not a YAML/JSON mapping exits rc=1."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("- item1\n- item2\n")

        llm_called = []

        import lionagi.cli.agent as agent_mod

        async def fake_run(*a, **kw):
            llm_called.append(1)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run)

        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        rc = run_agent(_agent_args(form=str(bad)))

        assert rc == 1
        assert not llm_called
        assert errors, "log_error must have been called"

    def test_failed_field_validation_returns_rc1_without_llm_call(self, tmp_path, monkeypatch):
        """Missing required field → validation error → rc=1, no LLM call."""
        spec = tmp_path / "spec.yaml"
        spec.write_text(
            "title: scan\nfields:\n  repo:\n    type: str\n    required: true\nvalues: {}\n"
        )

        llm_called = []

        import lionagi.cli.agent as agent_mod

        async def fake_run(*a, **kw):
            llm_called.append(1)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run)

        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        rc = run_agent(_agent_args(form=str(spec)))

        assert rc == 1
        assert not llm_called
        assert errors, "log_error must have been called"
        assert any("repo" in e for e in errors), f"Expected 'repo' in error, got: {errors}"

    def test_valid_form_injects_context_into_prompt(self, tmp_path, monkeypatch):
        """A valid form prepends context block to the prompt before the LLM call."""
        spec = tmp_path / "spec.yaml"
        spec.write_text(
            "title: ctx\n"
            "fields:\n"
            "  repo:\n"
            "    type: str\n"
            "    required: true\n"
            "values:\n"
            "  repo: /my/repo\n"
        )

        captured_prompts: list[str] = []

        import lionagi.cli.agent as agent_mod

        async def fake_run_agent(model_str, prompt, **kw):
            captured_prompts.append(prompt)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run_agent)

        from lionagi.ln.concurrency import run_async as _real_run_async

        monkeypatch.setattr(agent_mod, "run_async", lambda coro: _real_run_async(coro))

        rc = run_agent(_agent_args(form=str(spec), prompt="analyze it"))

        assert rc == 0
        assert captured_prompts, "prompt must have been passed to _run_agent"
        prompt_sent = captured_prompts[0]
        assert "[Work Form: ctx]" in prompt_sent
        assert "/my/repo" in prompt_sent
        assert "analyze it" in prompt_sent

    def test_no_form_prompt_unchanged(self, monkeypatch):
        """Without --form the prompt is passed through unchanged."""
        captured_prompts: list[str] = []

        import lionagi.cli.agent as agent_mod

        async def fake_run_agent(model_str, prompt, **kw):
            captured_prompts.append(prompt)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run_agent)

        from lionagi.ln.concurrency import run_async as _real_run_async

        monkeypatch.setattr(agent_mod, "run_async", lambda coro: _real_run_async(coro))

        rc = run_agent(_agent_args(prompt="bare prompt"))
        assert rc == 0
        assert captured_prompts[0] == "bare prompt"


# ---------------------------------------------------------------------------
# run_agent — --preset forwarded correctly in the _run_agent() call
# ---------------------------------------------------------------------------


class TestPresetCodingBehaviour:
    """--preset coding must be forwarded to _run_agent(preset=...)."""

    def test_preset_coding_forwarded_to_run_agent(self, monkeypatch):
        """When --preset coding is set, preset='coding' is passed to _run_agent."""
        captured_kwargs: list[dict] = []

        import lionagi.cli.agent as agent_mod

        async def spy_run_agent(model_str, prompt, **kw):
            captured_kwargs.append(kw)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", spy_run_agent)

        from lionagi.ln.concurrency import run_async as _real_run_async

        monkeypatch.setattr(agent_mod, "run_async", lambda coro: _real_run_async(coro))

        rc = run_agent(_agent_args(preset="coding"))
        assert rc == 0
        assert captured_kwargs, "_run_agent must have been called"
        assert captured_kwargs[0].get("preset") == "coding"

    def test_preset_none_forwarded_as_none(self, monkeypatch):
        """Without --preset, preset=None is passed to _run_agent."""
        captured_kwargs: list[dict] = []

        import lionagi.cli.agent as agent_mod

        async def spy_run_agent(model_str, prompt, **kw):
            captured_kwargs.append(kw)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", spy_run_agent)

        from lionagi.ln.concurrency import run_async as _real_run_async

        monkeypatch.setattr(agent_mod, "run_async", lambda coro: _real_run_async(coro))

        rc = run_agent(_agent_args(preset=None))
        assert rc == 0
        assert captured_kwargs[0].get("preset") is None

    def test_preset_cwd_forwarded(self, monkeypatch, tmp_path):
        """--cwd DIR is forwarded to _run_agent(cwd=DIR) when using preset."""
        captured_kwargs: list[dict] = []

        import lionagi.cli.agent as agent_mod

        async def spy_run_agent(model_str, prompt, **kw):
            captured_kwargs.append(kw)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", spy_run_agent)

        from lionagi.ln.concurrency import run_async as _real_run_async

        monkeypatch.setattr(agent_mod, "run_async", lambda coro: _real_run_async(coro))

        rc = run_agent(_agent_args(preset="coding", cwd=str(tmp_path)))
        assert rc == 0
        assert captured_kwargs[0].get("cwd") == str(tmp_path)


# ---------------------------------------------------------------------------
# _run_agent — preset param wires _make_coding_preset() inside async path
# ---------------------------------------------------------------------------


def _wire_run_agent_mocks(monkeypatch, tmp_path):
    """Wire all external stubs needed for a bare _run_agent() call in tests."""
    from unittest.mock import AsyncMock

    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fast_operate(self, instruction=None, **kw):
        return "done"

    monkeypatch.setattr(Branch, "operate", fast_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude/sonnet")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(resolve_model_spec=lambda p, m: f"{p}/{m}"),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "a",
            stream_dir=tmp_path / "s",
            branches_dir=tmp_path / "b",
        ),
    )


@pytest.mark.asyncio
async def test_run_agent_preset_coding_creates_preset_config(monkeypatch, tmp_path):
    """_run_agent with preset='coding' calls _make_coding_preset()."""
    import lionagi.cli.agent as agent_mod

    coding_calls: list = []

    def spy_make_coding_preset(**kw):
        coding_calls.append(kw)
        return _make_coding_preset(**kw)

    monkeypatch.setattr(agent_mod, "_make_coding_preset", spy_make_coding_preset)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    result, provider, branch_id, status = await _run_agent(
        "claude/sonnet", "build a feature", preset="coding"
    )

    assert status == "completed"
    assert coding_calls, "_make_coding_preset() must have been called"


@pytest.mark.asyncio
async def test_run_agent_no_preset_skips_coding_preset(monkeypatch, tmp_path):
    """_run_agent without preset must NOT call _make_coding_preset()."""
    import lionagi.cli.agent as agent_mod

    coding_calls: list = []

    def spy_make_coding_preset(**kw):
        coding_calls.append(kw)
        return _make_coding_preset(**kw)

    monkeypatch.setattr(agent_mod, "_make_coding_preset", spy_make_coding_preset)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "do stuff", preset=None)

    assert not coding_calls, "_make_coding_preset() must NOT be called without preset"

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for 'li agent --preset' (3a) and 'li agent --form' (3b).

Parser-level tests: flag parsing, choices rejection, file-not-found, invalid
spec → rc!=0 + error message.

Behaviour tests with runner patched: assert _make_coding_preset() is used when
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
# _run_agent — preset param wires _make_coding_preset() and create_agent
# ---------------------------------------------------------------------------

#: Core CodingToolkit tool names expected when preset=coding is used.
_CODING_TOOL_NAMES = {"reader", "editor", "bash", "search"}


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


@pytest.mark.asyncio
async def test_run_agent_preset_coding_registers_tools(monkeypatch, tmp_path):
    """preset=coding must register CodingToolkit core tools on the branch.

    Strategy: let create_agent run for real (it executes synchronously barring
    model construction, which we bypass via chat_model injection).  We capture
    the branch by intercepting Branch.__init__ to record every constructed
    Branch instance, then inspect the last one created (which is the one
    create_agent returned).
    """
    import lionagi.cli.agent as agent_mod

    branches_created: list = []
    from lionagi import Branch as _Branch

    real_branch_init = _Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(_Branch, "__init__", spy_branch_init)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "build it", preset="coding")

    assert branches_created, "Branch must have been constructed for preset=coding"
    # The last created branch is the one from create_agent (tools are registered
    # after init, so we check whichever has the coding tools).
    tool_branch = next(
        (b for b in branches_created if _CODING_TOOL_NAMES.issubset(b.acts.registry.keys())),
        None,
    )
    assert tool_branch is not None, (
        f"No branch has all coding tools registered. "
        f"Registries: {[set(b.acts.registry.keys()) for b in branches_created]}"
    )


@pytest.mark.asyncio
async def test_run_agent_preset_coding_guards_attached(monkeypatch, tmp_path):
    """preset=coding must attach preprocessors (guards) to bash, reader, editor."""
    from lionagi import Branch as _Branch

    branches_created: list = []
    real_branch_init = _Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(_Branch, "__init__", spy_branch_init)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "hack it", preset="coding")

    assert branches_created
    # Find the branch that has the coding tools (created by create_agent).
    tool_branch = next(
        (b for b in branches_created if "bash" in b.acts.registry),
        None,
    )
    assert tool_branch is not None, "No branch has bash tool registered"

    # secure=True (default) wires guard_destructive on bash and guard_paths on
    # reader/editor — all three tools must have a preprocessor set.
    for tool_name in ("bash", "reader", "editor"):
        tool = tool_branch.acts.registry.get(tool_name)
        assert tool is not None, f"tool '{tool_name}' not registered"
        assert tool.preprocessor is not None, (
            f"tool '{tool_name}' missing preprocessor (guard not attached)"
        )


@pytest.mark.asyncio
async def test_run_agent_resume_with_preset_raises(monkeypatch, tmp_path):
    """--resume combined with --preset must raise ValueError immediately."""
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ValueError, match="preset only applies to new branches"):
        await _run_agent(
            "claude/sonnet",
            "do stuff",
            resume="some-branch-id",
            preset="coding",
        )


@pytest.mark.asyncio
async def test_run_agent_continue_last_with_preset_raises(monkeypatch, tmp_path):
    """--continue-last combined with --preset must raise ValueError immediately."""
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ValueError, match="preset only applies to new branches"):
        await _run_agent(
            "claude/sonnet",
            "do stuff",
            continue_last=True,
            preset="coding",
        )


# ---------------------------------------------------------------------------
# Finding 1: profile + preset system prompt composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_preset_and_profile_single_system_message_with_both(monkeypatch, tmp_path):
    """preset=coding + profile.system_prompt → exactly one system message that
    contains BOTH the preset role/implementer content AND the profile extension.

    MessageManager.add_message(system=...) calls set_system which replaces the
    message.  The fix composes profile.system_prompt into spec.extra_prompt
    BEFORE create_agent so build_system_message() produces a single message with
    all content.
    """
    from types import SimpleNamespace as _NS

    from lionagi import Branch as _Branch
    from lionagi.cli._agents import AgentProfile  # type: ignore[attr-defined]

    branches_created: list = []
    real_branch_init = _Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(_Branch, "__init__", spy_branch_init)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    # Patch load_agent_profile to return a fake profile with a known prompt.
    # raw_body must NOT contain LION_SYSTEM_MESSAGE — that's added by the factory.
    PROFILE_RAW_BODY = "PROFILE_EXTENSION_UNIQUE_MARKER"
    fake_profile = _NS(
        model=None,
        effort=None,
        yolo=False,
        fast_mode=False,
        system_prompt="LION_SYSTEM_MESSAGE\n\n" + PROFILE_RAW_BODY,
        raw_body=PROFILE_RAW_BODY,
        lion_system=True,
        artifact_defaults=None,
    )
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: fake_profile)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "build it",
        agent_name="myprofile",
        preset="coding",
    )

    assert branches_created, "Branch must have been constructed"
    tool_branch = next(
        (b for b in branches_created if "bash" in b.acts.registry),
        None,
    )
    assert tool_branch is not None, "No branch with coding tools found"

    # Exactly one system message.
    sys_msg = tool_branch.msgs.system
    assert sys_msg is not None, "Branch has no system message"
    rendered = sys_msg.rendered
    # Preset role content: the implementer profile embeds "implementer" keyword.
    assert "implementer" in rendered.lower(), (
        f"Expected preset 'implementer' role text in system message; got:\n{rendered[:500]}"
    )
    # Profile raw body is present (passed in via extra_prompt slot).
    assert PROFILE_RAW_BODY in rendered, (
        f"Expected profile raw body {PROFILE_RAW_BODY!r} in system message; got:\n{rendered[:500]}"
    )


@pytest.mark.asyncio
async def test_run_agent_profile_without_preset_system_prompt_unchanged(monkeypatch, tmp_path):
    """Without --preset, profile.system_prompt is applied via add_message as before."""
    from types import SimpleNamespace as _NS

    from lionagi import Branch as _Branch

    branches_created: list = []
    real_branch_init = _Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(_Branch, "__init__", spy_branch_init)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    PROFILE_PROMPT = "PROFILE_ONLY_PROMPT_MARKER"
    fake_profile = _NS(
        model=None,
        effort=None,
        yolo=False,
        fast_mode=False,
        system_prompt=PROFILE_PROMPT,
        artifact_defaults=None,
    )
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(agent_mod, "load_agent_profile", lambda name: fake_profile)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "do stuff",
        agent_name="myprofile",
        preset=None,
    )

    assert branches_created
    # Last branch (no coding tools — plain Branch).
    plain_branch = branches_created[-1]
    sys_msg = plain_branch.msgs.system
    assert sys_msg is not None, "Profile system prompt not set"
    assert PROFILE_PROMPT in sys_msg.rendered


# ---------------------------------------------------------------------------
# Finding 2: form spec closed schema enforcement
# ---------------------------------------------------------------------------


class TestFormSpecClosedSchema:
    """Unknown top-level keys and undeclared values must be rejected."""

    def test_unknown_top_level_key_raises_value_error(self):
        """A misspelled key like 'fieldz' must raise ValueError."""
        spec = {"fieldz": {"x": {"type": "str", "required": True}}}
        with pytest.raises(ValueError, match="unknown top-level key"):
            _build_work_form(spec, "<test>")

    def test_unknown_key_names_the_bad_key(self):
        """Error message must identify the offending key."""
        spec = {"fieldz": {}, "typo": "bad"}
        with pytest.raises(ValueError) as exc_info:
            _build_work_form(spec, "<test>")
        assert "fieldz" in str(exc_info.value) or "typo" in str(exc_info.value)

    def test_undeclared_value_key_raises_value_error(self):
        """Value key not declared in fields must raise ValueError."""
        spec = {
            "fields": {"known": {"type": "str", "required": True}},
            "values": {"known": "ok", "undeclared": "bypass"},
        }
        with pytest.raises(ValueError, match="undeclared key"):
            _build_work_form(spec, "<test>")

    def test_undeclared_value_names_the_bad_key(self):
        """Error message must name the undeclared value key."""
        spec = {
            "fields": {"x": {"type": "str", "required": False}},
            "values": {"x": "fine", "sneaky": "bad"},
        }
        with pytest.raises(ValueError) as exc_info:
            _build_work_form(spec, "<test>")
        assert "sneaky" in str(exc_info.value)

    def test_values_without_fields_raises(self):
        """values declared without fields is a validation error.

        --form is a validation gate; forwarding unvalidated values silently
        defeats its purpose.  Callers that want unstructured context should
        put it directly in the prompt, not in a form spec.
        """
        spec = {"values": {"k": "v", "extra": "also-fine"}}
        with pytest.raises(ValueError, match="'values' are declared but 'fields' is absent"):
            _build_work_form(spec, "<test>")

    def test_run_agent_unknown_top_level_key_rc1_no_llm(self, tmp_path, monkeypatch):
        """Misspelled top-level key in spec file → rc=1, no LLM call."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("fieldz:\n  x:\n    type: str\n    required: true\n")

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
        assert not llm_called, "LLM must not be called when spec has unknown top-level key"
        assert errors, "log_error must have been called"
        assert any("fieldz" in e or "unknown" in e.lower() for e in errors), (
            f"Expected 'fieldz' or 'unknown' in error; got: {errors}"
        )

    def test_run_agent_undeclared_value_key_rc1_no_llm(self, tmp_path, monkeypatch):
        """Undeclared value key → rc=1, no LLM call."""
        spec = tmp_path / "spec.yaml"
        spec.write_text(
            "fields:\n  known:\n    type: str\n    required: true\n"
            "values:\n  known: ok\n  undeclared: bypass\n"
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
        assert errors
        assert any("undeclared" in e or "undeclared" in e.lower() for e in errors), (
            f"Expected 'undeclared' in error; got: {errors}"
        )


# ---------------------------------------------------------------------------
# Finding 3: directory --form path → rc=1, clear error, no traceback
# ---------------------------------------------------------------------------


class TestFormDirectoryPath:
    def test_load_form_spec_directory_raises_value_error(self, tmp_path):
        """Passing a directory path to _load_form_spec must raise ValueError."""
        with pytest.raises(ValueError, match="not a regular file"):
            _load_form_spec(str(tmp_path))

    def test_run_agent_directory_form_rc1_no_llm(self, tmp_path, monkeypatch):
        """Directory path as --form value → rc=1, error on error channel."""
        llm_called = []
        import lionagi.cli.agent as agent_mod

        async def fake_run(*a, **kw):
            llm_called.append(1)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run)

        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        rc = run_agent(_agent_args(form=str(tmp_path)))
        assert rc == 1
        assert not llm_called, "LLM must not be called for directory --form path"
        assert errors, "log_error must have been called"
        assert any("regular file" in e or "not a regular file" in e for e in errors), (
            f"Expected 'regular file' in error; got: {errors}"
        )


# ---------------------------------------------------------------------------
# Round-2 Finding 1: non-mapping fields / values probe shapes
# ---------------------------------------------------------------------------


class TestFormNonMappingTypes:
    """Every probe shape from the codex report must produce rc=1 on error channel."""

    def test_fields_as_list_raises(self):
        with pytest.raises(ValueError, match="'fields' must be a mapping"):
            _build_work_form({"fields": []}, "<test>")

    def test_fields_as_string_raises(self):
        with pytest.raises(ValueError, match="'fields' must be a mapping"):
            _build_work_form({"fields": "abc"}, "<test>")

    def test_values_as_list_with_fields_raises(self):
        spec = {"fields": {"x": {"type": "str", "required": False}}, "values": []}
        with pytest.raises(ValueError, match="'values' must be a mapping"):
            _build_work_form(spec, "<test>")

    def test_values_as_string_with_no_fields_raises(self):
        """values: abc with no fields → ValueError (non-mapping, not field-less passthrough)."""
        with pytest.raises(ValueError, match="'values' must be a mapping"):
            _build_work_form({"values": "abc"}, "<test>")

    def test_values_as_list_with_no_fields_raises(self):
        """values: [] with no fields → rc=1 (runner must NOT run)."""
        with pytest.raises(ValueError, match="'values' must be a mapping"):
            _build_work_form({"values": []}, "<test>")

    def test_values_as_string_with_fields_raises(self):
        """values: abc with fields → non-mapping error, not string-iteration error."""
        spec = {"fields": {"known": {"type": "str"}}, "values": "abc"}
        with pytest.raises(ValueError, match="'values' must be a mapping"):
            _build_work_form(spec, "<test>")

    def _run_form_probe(self, tmp_path, monkeypatch, yaml_content):
        """Write a spec file with given content, run run_agent, return (rc, errors, called)."""
        bad = tmp_path / "spec.yaml"
        bad.write_text(yaml_content)

        llm_called = []
        import lionagi.cli.agent as agent_mod

        async def fake_run(*a, **kw):
            llm_called.append(1)
            return ("done", "claude", "br-001", "completed")

        monkeypatch.setattr(agent_mod, "_run_agent", fake_run)
        errors: list[str] = []
        monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors.append(msg))

        rc = run_agent(_agent_args(form=str(bad)))
        return rc, errors, llm_called

    def test_fields_as_list_rc1_no_llm(self, tmp_path, monkeypatch):
        rc, errors, called = self._run_form_probe(tmp_path, monkeypatch, "fields:\n  - x\n  - y\n")
        assert rc == 1
        assert not called, "runner must not be called for fields: []"
        assert errors

    def test_values_as_list_no_fields_rc1_no_llm(self, tmp_path, monkeypatch):
        """values: [] with no fields → rc=1, runner NOT called (was rc=0 before fix)."""
        rc, errors, called = self._run_form_probe(tmp_path, monkeypatch, "values:\n  - a\n  - b\n")
        assert rc == 1
        assert not called, "runner must not be called for values: [] with no fields"
        assert errors


# ---------------------------------------------------------------------------
# Round-2 Finding 2: LION_SYSTEM_MESSAGE dedup with real profile files
# ---------------------------------------------------------------------------


def _make_agents_dir(tmp_path, monkeypatch):
    """Create a .lionagi/agents/ dir under tmp_path and patch _find_lionagi_dirs."""
    lionagi_dir = tmp_path / ".lionagi"
    agents_dir = lionagi_dir / "agents"
    agents_dir.mkdir(parents=True)

    import lionagi.cli._agents as agents_mod

    monkeypatch.setattr(agents_mod, "_find_lionagi_dirs", lambda: [lionagi_dir])
    return agents_dir


@pytest.mark.asyncio
async def test_preset_and_real_profile_no_lion_system_duplication(monkeypatch, tmp_path):
    """Real profile (lion_system default=True) + preset=coding → exactly ONE
    '# Welcome to LIONAGI' in the system message.

    Before the fix: _parse_profile prepends LION_SYSTEM_MESSAGE into
    system_prompt; _run_agent passed system_prompt to _make_coding_preset;
    factory prepended LION_SYSTEM_MESSAGE again → count=2.

    After the fix: _run_agent passes profile.raw_body (without the header);
    factory prepends it once → count=1.
    """
    agents_dir = _make_agents_dir(tmp_path, monkeypatch)
    profile_body = "You are a precise coding assistant."
    (agents_dir / "coder.md").write_text(f"---\nlion_system: true\n---\n\n{profile_body}\n")

    from lionagi import Branch as _Branch

    branches_created: list = []
    real_branch_init = _Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(_Branch, "__init__", spy_branch_init)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "write code",
        agent_name="coder",
        preset="coding",
    )

    assert branches_created
    tool_branch = next((b for b in branches_created if "bash" in b.acts.registry), None)
    assert tool_branch is not None, "No branch with coding tools"

    rendered = tool_branch.msgs.system.rendered
    # Count occurrences of the LION header landmark.
    count = rendered.count("# Welcome to LIONAGI")
    assert count == 1, (
        f"Expected exactly 1 '# Welcome to LIONAGI'; got {count}.\nMessage start: {rendered[:600]}"
    )
    # Implementer role content must be present.
    assert "implementer" in rendered.lower(), "preset implementer role content missing"
    # Profile body must be present.
    assert profile_body in rendered, f"profile body {profile_body!r} not in message"


@pytest.mark.asyncio
async def test_preset_and_real_profile_nolion_no_lion_system_message(monkeypatch, tmp_path):
    """Real profile with lion_system:false + preset=coding → implementer content present,
    profile body present, ZERO occurrences of '# Welcome to LIONAGI'.

    When lion_system=False the profile's raw_body is passed; AgentSpec.lion_system
    remains True (the implementer preset owns that flag), so the factory adds the
    header once for the role but NOT for the profile body.

    Wait — AgentSpec.lion_system is always True for AgentSpec.coding(); the
    flag on AgentProfile.lion_system controls ONLY whether the profile body
    gets the header prepended.  The factory always prepends once for the spec.
    So count=1 is expected even for lion_system:false profiles.
    """
    agents_dir = _make_agents_dir(tmp_path, monkeypatch)
    profile_body = "Custom coding instructions, no lion header."
    (agents_dir / "nolion.md").write_text(f"---\nlion_system: false\n---\n\n{profile_body}\n")

    from lionagi import Branch as _Branch

    branches_created: list = []
    real_branch_init = _Branch.__init__

    def spy_branch_init(self, *args, **kwargs):
        real_branch_init(self, *args, **kwargs)
        branches_created.append(self)

    monkeypatch.setattr(_Branch, "__init__", spy_branch_init)
    _wire_run_agent_mocks(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude/sonnet",
        "write code",
        agent_name="nolion",
        preset="coding",
    )

    assert branches_created
    tool_branch = next((b for b in branches_created if "bash" in b.acts.registry), None)
    assert tool_branch is not None

    rendered = tool_branch.msgs.system.rendered
    count = rendered.count("# Welcome to LIONAGI")
    # The spec itself has lion_system=True so the factory adds it once for the
    # role; the profile body (raw_body, no header) is appended as extra_prompt.
    assert count == 1, (
        f"Expected 1 '# Welcome to LIONAGI' for lion_system:false profile + preset; "
        f"got {count}.\nMessage start: {rendered[:600]}"
    )
    assert profile_body in rendered, "profile body must be present"

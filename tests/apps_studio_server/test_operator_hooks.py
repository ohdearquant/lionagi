# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Hook library, Operator hook assembly, and the per-turn settings file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lionagi.studio.services import hooks_library as hl
from lionagi.studio.services import operator_hooks as oh


@pytest.fixture()
def hooks_paths(tmp_path, monkeypatch):
    library = tmp_path / "hooks" / "library.json"
    config = tmp_path / "operator_hooks.json"
    settings = tmp_path / "operator_hooks.settings.json"
    monkeypatch.setattr(hl, "_LIBRARY_PATH", library)
    monkeypatch.setattr(oh, "_CONFIG_PATH", config)
    monkeypatch.setattr(oh, "_SETTINGS_PATH", settings)
    return library, config, settings


GUARD = {"description": "block dangerous commands", "command": "echo guard", "timeout": 10}
MEMORY = {"description": "memory injection", "command": "echo memory"}

ASSEMBLY = {
    "enabled": True,
    "attachments": [
        {"hook": "guard-commands", "event": "pre_tool", "matcher": "Bash"},
        {"hook": "memory-injection", "event": "prompt_submit"},
    ],
}


def seed_library():
    hl.upsert_hook("guard-commands", GUARD)
    hl.upsert_hook("memory-injection", MEMORY)


class TestLibrary:
    def test_absent_file_reads_empty(self, hooks_paths):
        assert hl.read_library() == {}

    def test_upsert_read_roundtrip(self, hooks_paths):
        hl.upsert_hook("guard-commands", GUARD)
        lib = hl.read_library()
        assert lib["guard-commands"]["command"] == "echo guard"
        assert lib["guard-commands"]["timeout"] == 10

    def test_delete(self, hooks_paths):
        seed_library()
        assert hl.delete_hook("guard-commands") is True
        assert hl.delete_hook("guard-commands") is False
        assert "guard-commands" not in hl.read_library()

    def test_broken_file_raises(self, hooks_paths):
        library, _, _ = hooks_paths
        library.parent.mkdir(parents=True, exist_ok=True)
        library.write_text("{not json")
        with pytest.raises(hl.HookLibraryError):
            hl.read_library()

    @pytest.mark.parametrize(
        "spec",
        [
            "not a dict",
            {},
            {"command": ""},
            {"command": "x", "extra": 1},
            {"command": "x", "timeout": "10"},
            {"command": "x", "description": 3},
        ],
    )
    def test_invalid_defs_raise(self, hooks_paths, spec):
        with pytest.raises(hl.HookLibraryError):
            hl.validate_hook_def("bad", spec)


class TestAttachments:
    @pytest.mark.parametrize(
        "bad",
        [
            "not a list",
            [{"hook": "x"}],
            [{"event": "pre_tool"}],
            [{"hook": "", "event": "pre_tool"}],
            [{"hook": "x", "event": "NotAnEvent"}],
            [{"hook": "x", "event": "pre_tool", "matcher": 3}],
            [{"hook": "x", "event": "pre_tool", "extra": 1}],
        ],
    )
    def test_invalid_attachments_raise(self, bad):
        with pytest.raises(hl.HookLibraryError):
            hl.validate_attachments(bad)

    def test_library_resolution_rejects_dangling_name(self, hooks_paths):
        seed_library()
        with pytest.raises(hl.HookLibraryError):
            hl.validate_attachments(
                [{"hook": "nope", "event": "pre_tool"}], library=hl.read_library()
            )

    def test_materialize_maps_neutral_events(self, hooks_paths):
        seed_library()
        block = hl.materialize_claude_hooks(ASSEMBLY["attachments"])
        assert set(block) == {"PreToolUse", "UserPromptSubmit"}
        pre = block["PreToolUse"][0]
        assert pre["matcher"] == "Bash"
        assert pre["hooks"] == [{"type": "command", "command": "echo guard", "timeout": 10}]
        prompt = block["UserPromptSubmit"][0]
        assert "matcher" not in prompt
        assert prompt["hooks"] == [{"type": "command", "command": "echo memory"}]

    def test_materialize_skips_dangling_hook(self, hooks_paths):
        seed_library()
        hl.delete_hook("memory-injection")
        block = hl.materialize_claude_hooks(ASSEMBLY["attachments"])
        assert set(block) == {"PreToolUse"}


class TestOperatorConfig:
    def test_absent_file_reads_empty_enabled(self, hooks_paths):
        assert oh.read_config() == {"enabled": True, "attachments": []}

    def test_write_requires_library_resolution(self, hooks_paths):
        with pytest.raises(oh.OperatorHooksError):
            oh.write_config(ASSEMBLY)
        seed_library()
        assert oh.write_config(ASSEMBLY)["attachments"] == ASSEMBLY["attachments"]

    def test_write_read_roundtrip(self, hooks_paths):
        seed_library()
        oh.write_config(ASSEMBLY)
        assert oh.read_config()["attachments"] == ASSEMBLY["attachments"]

    def test_broken_file_raises_at_read(self, hooks_paths):
        _, config, _ = hooks_paths
        config.write_text("{not json")
        with pytest.raises(oh.OperatorHooksError):
            oh.read_config()


class TestMaterializeSettings:
    def test_empty_config_is_inert(self, hooks_paths):
        assert oh.materialize_settings_file() is None

    def test_disabled_config_is_inert(self, hooks_paths):
        seed_library()
        oh.write_config({**ASSEMBLY, "enabled": False})
        assert oh.materialize_settings_file() is None

    def test_broken_config_is_inert_not_fatal(self, hooks_paths):
        _, config, _ = hooks_paths
        config.write_text("{not json")
        assert oh.materialize_settings_file() is None

    def test_all_attachments_dangling_is_inert(self, hooks_paths):
        seed_library()
        oh.write_config(ASSEMBLY)
        hl.delete_hook("guard-commands")
        hl.delete_hook("memory-injection")
        assert oh.materialize_settings_file() is None

    def test_materializes_resolved_hooks_block(self, hooks_paths):
        _, _, settings = hooks_paths
        seed_library()
        oh.write_config(ASSEMBLY)
        path = oh.materialize_settings_file()
        assert path == settings
        payload = json.loads(settings.read_text())
        assert set(payload) == {"hooks"}
        assert set(payload["hooks"]) == {"PreToolUse", "UserPromptSubmit"}


class TestEngineKwarg:
    def test_relative_path_under_execution_root(self, hooks_paths):
        _, _, settings = hooks_paths
        seed_library()
        oh.write_config(ASSEMBLY)
        from lionagi.studio.operator.engine import _operator_hooks_settings_kwarg

        kwarg = _operator_hooks_settings_kwarg(settings.parent)
        assert kwarg == {"settings": "operator_hooks.settings.json"}
        # The produced value must survive the request model's own path check.
        from lionagi.libs.path_safety import check_path_safe

        check_path_safe(kwarg["settings"], "settings")

    def test_unreachable_root_runs_hookless(self, hooks_paths, tmp_path):
        seed_library()
        oh.write_config(ASSEMBLY)
        from lionagi.studio.operator.engine import _operator_hooks_settings_kwarg

        other_root = tmp_path / "elsewhere"
        other_root.mkdir()
        assert _operator_hooks_settings_kwarg(other_root) == {}

    def test_no_config_yields_no_kwarg(self, hooks_paths):
        from lionagi.studio.operator.engine import _operator_hooks_settings_kwarg

        assert _operator_hooks_settings_kwarg(Path.home()) == {}


class TestRoutes:
    async def test_library_routes_roundtrip(self, hooks_paths):
        await hl.put_hook_def_route("guard-commands", GUARD)
        listing = await hl.list_hook_library_route()
        assert "guard-commands" in listing["hooks"]
        assert "pre_tool" in listing["events"]
        out = await hl.delete_hook_def_route("guard-commands")
        assert out == {"ok": True}

    async def test_library_put_rejects_invalid(self, hooks_paths):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await hl.put_hook_def_route("bad", {"command": ""})
        assert exc_info.value.status_code == 422

    async def test_library_delete_404(self, hooks_paths):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await hl.delete_hook_def_route("nope")
        assert exc_info.value.status_code == 404

    async def test_operator_get_reports_broken_config(self, hooks_paths):
        _, config, _ = hooks_paths
        config.write_text("{not json")
        out = await oh.get_operator_hooks_route()
        assert "error" in out

    async def test_operator_put_rejects_dangling_hook(self, hooks_paths):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await oh.put_operator_hooks_route(ASSEMBLY)
        assert exc_info.value.status_code == 422

    async def test_operator_put_get_roundtrip(self, hooks_paths):
        seed_library()
        put_out = await oh.put_operator_hooks_route(ASSEMBLY)
        get_out = await oh.get_operator_hooks_route()
        assert put_out["attachments"] == get_out["attachments"]
        assert get_out["enabled"] is True


class TestAgentProfileHooks:
    def test_agent_write_validates_and_projects_hooks(self, hooks_paths, tmp_path, monkeypatch):
        seed_library()
        from lionagi.studio.services import agents as agents_svc

        monkeypatch.setattr(agents_svc, "_AGENTS_ROOT", tmp_path / "agents")
        created = agents_svc.create_agent(
            "hooked",
            {
                "provider": "claude_code",
                "model": "sonnet",
                "system_prompt": "test agent",
                "hooks": [{"hook": "guard-commands", "event": "pre_tool"}],
            },
        )
        assert created["hooks"] == [{"hook": "guard-commands", "event": "pre_tool"}]

        updated = agents_svc.update_agent("hooked", {"hooks": []})
        assert "hooks" not in updated

    def test_agent_write_rejects_dangling_hook(self, hooks_paths, tmp_path, monkeypatch):
        from lionagi.studio.services import agents as agents_svc

        monkeypatch.setattr(agents_svc, "_AGENTS_ROOT", tmp_path / "agents")
        with pytest.raises(hl.HookLibraryError):
            agents_svc.create_agent(
                "hooked2",
                {
                    "provider": "claude_code",
                    "hooks": [{"hook": "nope", "event": "pre_tool"}],
                },
            )

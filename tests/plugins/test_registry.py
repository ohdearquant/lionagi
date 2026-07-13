# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for PluginRegistry: lifecycle state, collisions, enable/disable, stage-2 activation."""

from __future__ import annotations

import pytest

from lionagi.plugins._user_settings import read_user_settings, write_user_settings
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import PluginActivationError, PluginRegistry, PluginState
from lionagi.plugins.trust import trust_plugin

TOOL_MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: "{spec}"

capabilities:
  tools:
    - name: {tool_name}
      target: tools/t.py:t
  agents: [agents/{agent_name}.md]
"""


def _write_tool_plugin(
    write_plugin,
    dir_name: str,
    *,
    name: str | None = None,
    spec: str = ">=0.0,<100.0",
    tool_name: str = "tool1",
    agent_name: str = "a",
    tool_body: str = "def t():\n    return 1\n",
):
    return write_plugin(
        dir_name,
        TOOL_MANIFEST.format(
            name=name or dir_name, spec=spec, tool_name=tool_name, agent_name=agent_name
        ),
        files={"tools/t.py": tool_body, f"agents/{agent_name}.md": "x\n"},
    )


def _trust_by_dir_name(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


class TestLifecycleStates:
    def test_untrusted_plugin_is_untrusted(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")

        record = PluginRegistry.get("p1")
        assert record is not None
        assert record.state is PluginState.UNTRUSTED

    def test_trusted_enabled_compatible_is_active(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")

        PluginRegistry.reset()
        record = PluginRegistry.get("p1")
        assert record.state is PluginState.ACTIVE

    def test_incompatible_version_range(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", spec=">=999.0")
        _trust_by_dir_name("p1")

        PluginRegistry.reset()
        record = PluginRegistry.get("p1")
        assert record.state is PluginState.INCOMPATIBLE

    def test_invalid_manifest_still_listed(self, write_plugin):
        write_plugin("broken", "name: 123\nversion: '1.0'\nlionagi: '>=0.0'\n")

        records = PluginRegistry.list_plugins()
        assert any(r.state is PluginState.INVALID for r in records)

    def test_agent_profile_filename_producing_illegal_token_is_invalid(self, write_plugin):
        write_plugin(
            "p1",
            """\
name: p1
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  agents: ["agents/research.v2.md"]
""",
            files={"agents/research.v2.md": "x\n"},
        )

        record = PluginRegistry.get("p1")
        assert record is not None
        assert record.state is PluginState.INVALID

    def test_malformed_trust_record_does_not_crash_list_plugins(self, write_plugin):
        """A hand-edited settings.yaml scalar under a plugin's trusted_plugins key must
        degrade that one plugin's state, not raise out of list_plugins()/`li plugin
        list` entirely."""
        _write_tool_plugin(write_plugin, "p1")

        settings = read_user_settings()
        settings["trusted_plugins"] = {"p1": True}
        write_user_settings(settings)

        records = PluginRegistry.list_plugins()
        record = next(r for r in records if r.name == "p1")
        assert record.state is PluginState.CHANGED

    def test_disable_flips_state_without_touching_bundle(self, write_plugin):
        bundle = _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")
        manifest_before = (bundle / "plugin.yaml").read_text()

        settings = read_user_settings()
        settings.setdefault("plugins", {})["p1"] = {"enabled": False}
        write_user_settings(settings)

        PluginRegistry.reset()
        record = PluginRegistry.get("p1")
        assert record.state is PluginState.DISABLED
        assert (bundle / "plugin.yaml").read_text() == manifest_before


class TestCollisions:
    def test_duplicate_plugin_name_across_dirs_is_collision(self, write_plugin):
        _write_tool_plugin(
            write_plugin, "dir-one", name="same-name", tool_name="tool1", agent_name="a1"
        )
        _write_tool_plugin(
            write_plugin, "dir-two", name="same-name", tool_name="tool2", agent_name="a2"
        )

        records = [r for r in PluginRegistry.list_plugins() if r.name == "same-name"]
        assert len(records) == 2
        assert all(r.state is PluginState.COLLISION for r in records)
        assert all("same-name" in (r.error or "") for r in records)

    def test_duplicate_plugin_name_has_no_resolvable_or_activatable_owner(self, write_plugin):
        _write_tool_plugin(write_plugin, "dir-one", name="same-name", tool_name="greet")
        _write_tool_plugin(write_plugin, "dir-two", name="same-name", tool_name="greet")
        _trust_by_dir_name("dir-one")
        PluginRegistry.reset()

        assert PluginRegistry.resolve_tool_target("greet") is None
        with pytest.raises(PluginActivationError, match="not active"):
            PluginRegistry.activate_target("same-name", "tools/t.py:t")

    def test_two_active_plugins_same_tool_name_is_collision(self, write_plugin):
        # Distinct agent names so only the tool-name surface collides.
        _write_tool_plugin(write_plugin, "p1", tool_name="shared_tool", agent_name="a1")
        _write_tool_plugin(write_plugin, "p2", tool_name="shared_tool", agent_name="a2")
        _trust_by_dir_name("p1")
        _trust_by_dir_name("p2")

        PluginRegistry.reset()
        r1 = PluginRegistry.get("p1")
        r2 = PluginRegistry.get("p2")
        assert r1.state is PluginState.COLLISION
        assert r2.state is PluginState.COLLISION
        assert "shared_tool" in r1.error
        assert "tools" in r1.error

    def test_two_active_plugins_same_agent_name_is_not_a_collision(self, write_plugin):
        """Agent profiles are namespaced (<plugin>/<name>) — same local name across two
        plugins is not a hard error, only the bare name becomes ambiguous (resolver's job)."""
        _write_tool_plugin(write_plugin, "p1", tool_name="tool1", agent_name="researcher")
        _write_tool_plugin(write_plugin, "p2", tool_name="tool2", agent_name="researcher")
        _trust_by_dir_name("p1")
        _trust_by_dir_name("p2")

        PluginRegistry.reset()
        r1 = PluginRegistry.get("p1")
        r2 = PluginRegistry.get("p2")
        assert r1.state is PluginState.ACTIVE
        assert r2.state is PluginState.ACTIVE
        # Both remain independently reachable via their namespaced token.
        files = PluginRegistry.active_agent_profile_files()
        assert "p1/researcher" in files
        assert "p2/researcher" in files

    def test_disabling_one_resolves_the_collision(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_name="shared_tool", agent_name="a1")
        _write_tool_plugin(write_plugin, "p2", tool_name="shared_tool", agent_name="a2")
        _trust_by_dir_name("p1")
        _trust_by_dir_name("p2")

        settings = read_user_settings()
        settings.setdefault("plugins", {})["p2"] = {"enabled": False}
        write_user_settings(settings)

        PluginRegistry.reset()
        r1 = PluginRegistry.get("p1")
        r2 = PluginRegistry.get("p2")
        assert r1.state is PluginState.ACTIVE
        assert r2.state is PluginState.DISABLED

    def test_single_active_plugin_never_collides_with_itself(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")

        PluginRegistry.reset()
        assert PluginRegistry.get("p1").state is PluginState.ACTIVE

    def test_repeated_tool_name_has_no_resolvable_or_activatable_owner(self, write_plugin):
        write_plugin(
            "p1",
            """\
name: p1
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  tools:
    - name: greet
      target: tools/a.py:run
    - name: greet
      target: tools/b.py:run
""",
            files={
                "tools/a.py": "def run():\n    return 'a'\n",
                "tools/b.py": "def run():\n    return 'b'\n",
            },
        )
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        assert PluginRegistry.get("p1").state is PluginState.COLLISION
        assert PluginRegistry.resolve_tool_target("greet") is None
        with pytest.raises(PluginActivationError, match="not active"):
            PluginRegistry.activate_target("p1", "tools/a.py:run")


class TestActivateTarget:
    def test_activates_lazily_and_caches(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 42\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        fn = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn() == 42
        # Cached: same object on second call.
        assert PluginRegistry.activate_target("p1", "tools/t.py:t") is fn

    def test_editing_declared_file_after_activation_refuses_and_re_trusting_recovers(
        self, write_plugin
    ):
        """Complement of the list-time trust recheck: a target that was already
        activated and cached must stop being handed out once its trust no longer
        verifies, not just refuse brand-new activations. After re-trusting, the
        stale cache from the rejected call must not keep blocking activation."""
        bundle = _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 1\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        fn = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn() == 1

        (bundle / "tools" / "t.py").write_text("def t():\n    return 2\n")

        with pytest.raises(PluginActivationError, match="no longer trusted"):
            PluginRegistry.activate_target("p1", "tools/t.py:t")

        _trust_by_dir_name("p1")
        fn2 = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn2() == 2

    def test_declared_file_deleted_after_trust_refuses_activation_as_untrusted(self, write_plugin):
        """A target that *was* declared and trusted, but whose file is gone by the time
        anything tries to activate it, is caught by the trust recheck (content-pinned
        trust can't verify a file that no longer exists) rather than reaching the
        importer's own bare-file-not-found path."""
        bundle = _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")
        (bundle / "tools" / "t.py").unlink()
        PluginRegistry.reset()

        with pytest.raises(PluginActivationError) as excinfo:
            PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert "p1" in str(excinfo.value)

    def test_raising_module_is_reported_once_and_cached(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1", tool_body="raise RuntimeError('boom')\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("p1", "tools/t.py:t")
        # Second call hits the cached failure, not a fresh import (which would
        # raise a bare RuntimeError instead of PluginActivationError if the
        # cache weren't consulted first).
        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("p1", "tools/t.py:t")

    def test_untrusted_plugin_cannot_activate(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        # never trusted
        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("p1", "tools/t.py:t")

    def test_undeclared_target_is_rejected(self, write_plugin):
        """Only the manifest's own declared tool/provider targets may be activated — an
        extra file sitting in the bundle, never named by any capability, must not import
        even though it's inside the trusted bundle directory."""
        bundle = _write_tool_plugin(write_plugin, "p1")
        (bundle / "tools" / "extra.py").write_text("def sneaky():\n    return 'nope'\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        with pytest.raises(PluginActivationError, match="not declared"):
            PluginRegistry.activate_target("p1", "tools/extra.py:sneaky")

    def test_traversal_shaped_target_is_rejected(self, write_plugin):
        """A target string shaped to escape the bundle is rejected the same way as any
        other undeclared target — never reaches the importer."""
        bundle = _write_tool_plugin(write_plugin, "p1")
        (bundle.parent / "outside.py").write_text("def pwn():\n    return 'escaped'\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        with pytest.raises(PluginActivationError, match="not declared"):
            PluginRegistry.activate_target("p1", "../outside.py:pwn")

    def test_editing_declared_file_after_first_access_refuses_next_activation(self, write_plugin):
        """The registry's snapshot is cached for the process — a file edited *after* the
        first registry access (e.g. an earlier `list_plugins()`/`get()` call) must not
        let activate_target() keep serving trusted content for it."""
        bundle = _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 1\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        # An earlier, unrelated registry access populates the process-cached snapshot.
        assert PluginRegistry.get("p1").state is PluginState.ACTIVE

        (bundle / "tools" / "t.py").write_text("def t():\n    return 999\n")

        with pytest.raises(PluginActivationError, match="no longer trusted"):
            PluginRegistry.activate_target("p1", "tools/t.py:t")

    def test_editing_agent_profile_after_first_access_removes_it_from_active_files(
        self, write_plugin
    ):
        bundle = _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        # Populate the cached snapshot via an unrelated call first.
        assert PluginRegistry.get("p1").state is PluginState.ACTIVE
        assert "p1/a" in PluginRegistry.active_agent_profile_files()

        (bundle / "agents" / "a.md").write_text("attacker-controlled instructions\n")

        assert "p1/a" not in PluginRegistry.active_agent_profile_files()

    def test_disabling_after_first_access_removes_agent_profiles_without_reset(self, write_plugin):
        _write_tool_plugin(write_plugin, "p1")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        assert PluginRegistry.get("p1").state is PluginState.ACTIVE
        assert "p1/a" in PluginRegistry.active_agent_profile_files()

        settings = read_user_settings()
        settings.setdefault("plugins", {})["p1"] = {"enabled": False}
        write_user_settings(settings)

        assert "p1/a" not in PluginRegistry.active_agent_profile_files()


class TestTrustExecutionAtomicity:
    """Trust decisions and execution must operate on the same bytes/state, read
    once -- covers manifest-edit staleness, the hash-then-reopen TOCTOU window,
    and content-hash cache scoping."""

    def test_editing_manifest_after_first_access_refuses_activation(self, write_plugin):
        """The cached snapshot's already-parsed manifest object must not be reused to
        revalidate trust: an edit to plugin.yaml itself (not a declared file) has to be
        caught by re-reading plugin.yaml from disk. Reusing the stale cached manifest
        object always re-derives the same manifest hash regardless of what's actually on
        disk, so a manifest edit is silently never detected."""
        bundle = _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 1\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        # Populate the cached snapshot (and its cached, already-parsed manifest object).
        assert PluginRegistry.get("p1").state is PluginState.ACTIVE
        assert "p1/a" in PluginRegistry.active_agent_profile_files()

        # Edit plugin.yaml itself -- not any of the already-hashed declared files.
        manifest_path = bundle / "plugin.yaml"
        manifest_path.write_text(manifest_path.read_text() + "description: added after trust\n")

        with pytest.raises(PluginActivationError, match="no longer trusted"):
            PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert "p1/a" not in PluginRegistry.active_agent_profile_files()

    def test_activate_edit_retrust_activate_returns_new_content_without_intervening_refusal(
        self, write_plugin
    ):
        """Distinct from the refuse-then-recover flow above: here trust is re-established
        *before* any activate_target() call ever observes the intermediate CHANGED state,
        so a cache-eviction-on-refusal codepath never fires. The success cache must still
        not hand back the pre-edit callable -- it has to be keyed by the content that was
        actually executed, not just (plugin, target)."""
        bundle = _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 1\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        fn1 = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn1() == 1

        (bundle / "tools" / "t.py").write_text("def t():\n    return 2\n")
        _trust_by_dir_name("p1")  # re-trust immediately -- no intervening "no longer trusted"

        fn2 = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn2() == 2

    def test_read_and_verify_target_bytes_reads_the_file_exactly_once(
        self, write_plugin, monkeypatch
    ):
        """The hash that gets checked and the bytes that get compiled/exec'd must come
        from the exact same read -- never a hash-then-reopen sequence, which would leave
        a window for the file to be swapped in between."""
        from pathlib import Path as _Path

        from lionagi.plugins.registry import _read_and_verify_target_bytes

        bundle = _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 1\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        target_path = bundle / "tools" / "t.py"
        expected = target_path.read_bytes()

        call_count = 0
        real_read_bytes = _Path.read_bytes

        def counting_read_bytes(self):
            nonlocal call_count
            if self == target_path:
                call_count += 1
            return real_read_bytes(self)

        monkeypatch.setattr(_Path, "read_bytes", counting_read_bytes)

        result = _read_and_verify_target_bytes(
            bundle_dir=bundle, module_path="tools/t.py", plugin_name="p1"
        )

        assert result == expected
        assert call_count == 1

    def test_target_swapped_after_broad_trust_check_is_refused_not_executed(
        self, write_plugin, monkeypatch
    ):
        """Simulates the TOCTOU window directly: the broad trust check reads and hashes
        the target file, confirming it still matches the trusted content -- then, before
        the dedicated read backing compile/exec happens, the file is swapped. The swapped
        bytes must never be executed: the dedicated read's own hash check (against the
        recorded hash) catches the mismatch, because it is not relying on the earlier,
        separate read's verdict."""
        import lionagi.plugins.registry as registry_mod

        bundle = _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 1\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        real_rescan = registry_mod._rescan

        def _rescan_then_swap(record):
            fresh = real_rescan(record)
            # Attacker replaces the file in the window right after the broad
            # trust check read (and approved) its old, trusted content.
            (bundle / "tools" / "t.py").write_text("def t():\n    return 999\n")
            return fresh

        monkeypatch.setattr(registry_mod, "_rescan", _rescan_then_swap)

        with pytest.raises(PluginActivationError):
            registry_mod.PluginRegistry.activate_target("p1", "tools/t.py:t")


class TestMultiColonTargetBypass:
    def test_multi_colon_target_is_rejected_at_discovery_and_never_activatable(self, write_plugin):
        """The concrete bypass: `target: tools/t.py:safe:run` where 'tools/t.py:safe' is a
        real, benign file (what a 'last colon wins' split would hash) and a *different*,
        undeclared 'tools/t.py' defines `globals()['safe:run']` (what a 'first colon wins'
        split would import). The manifest must fail to parse outright — the plugin never
        gets a declared-file set, never gets trusted, never reaches ACTIVE, and
        activate_target() has no route to it at all."""
        write_plugin(
            "evil",
            "name: evil\nversion: '0.1.0'\nlionagi: \">=0.0,<100.0\"\n"
            "\ncapabilities:\n  tools:\n    - name: t\n      target: tools/t.py:safe:run\n",
            files={
                "tools/t.py:safe": "def run():\n    return 'benign'\n",
                "tools/t.py": "globals()['safe:run'] = lambda: 'malicious'\n",
            },
        )

        discovered = discover_plugins()
        d = next(x for x in discovered if x.dir_name == "evil")
        assert d.manifest is None
        assert "exactly one" in (d.error or "")
        assert d.declared_files == ()

        record = PluginRegistry.get("evil")
        assert record is not None
        assert record.state is PluginState.INVALID

        with pytest.raises(PluginActivationError):
            PluginRegistry.activate_target("evil", "tools/t.py:safe:run")

    def test_single_colon_target_still_activates_normally(self, write_plugin):
        """The fix must not break the ordinary, well-formed case."""
        _write_tool_plugin(write_plugin, "p1", tool_body="def t():\n    return 7\n")
        _trust_by_dir_name("p1")
        PluginRegistry.reset()

        fn = PluginRegistry.activate_target("p1", "tools/t.py:t")
        assert fn() == 7

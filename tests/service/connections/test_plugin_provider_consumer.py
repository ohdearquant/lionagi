# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin-provider consumer wiring in ``EndpointRegistry.match``.

On a provider-resolution miss, an active (trusted + enabled + still-trusted)
plugin's declared provider modules are imported and the match is re-run once
before falling back to the generic OpenAI-compatible endpoint (ADR-0088 D3).
A plugin that is untrusted, disabled, or whose declared files changed since
trust was recorded contributes nothing, and the fallback stays byte-for-byte
identical to today's behavior.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
from types import SimpleNamespace

import pytest

from lionagi.plugins._user_settings import (
    read_user_settings,
    user_settings_path,
    write_user_settings,
)
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import PluginActivationError, PluginRegistry, PluginState
from lionagi.plugins.trust import trust_plugin
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import EndpointRegistry

MANIFEST = """\
name: {name}
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  providers:
    - module: providers/endpoint.py
"""

PROVIDER_MODULE = """\
from lionagi.service.connections.registry import register_endpoint
from lionagi.service.connections.endpoint import Endpoint


@register_endpoint(provider="{provider}", endpoint="chat")
class PluginProviderEndpoint(Endpoint):
    pass
"""


def _clear_plugin_modules() -> None:
    """Drop every ``sys.modules`` entry left by ``PluginRegistry.activate_target``.

    ``PluginRegistry.reset()`` (already run around every test by the repo-wide
    ``_reset_plugin_registry`` autouse fixture) only clears the registry's own
    scan/activation caches -- the actual module object it installed into
    ``sys.modules`` stays there for the life of the worker process. Without
    this, a module imported by one test would still satisfy
    ``key in sys.modules`` in a later test in the same xdist worker even
    though that later test's plugin was never consulted, which is exactly
    the false-positive this file's "was it actually imported" assertions
    guard against.
    """
    for key in [k for k in sys.modules if k.startswith("_lionagi_plugin_")]:
        del sys.modules[key]


@pytest.fixture(autouse=True)
def _isolate_endpoint_registry():
    """Snapshot/restore ``EndpointRegistry``'s class-level entries around each test.

    A plugin provider module self-registers via ``@register_endpoint`` as a
    process-lifetime side effect of import, same as any built-in provider
    module -- without this, one test's plugin-registered entry would leak
    into every later test in this file (and, if xdist schedules another
    provider-routing test into the same worker, into that file too).
    """
    EndpointRegistry._ensure_loaded()
    saved_entries = list(EndpointRegistry._entries)
    saved_loaded = EndpointRegistry._loaded
    _clear_plugin_modules()
    yield
    EndpointRegistry._entries = saved_entries
    EndpointRegistry._loaded = saved_loaded
    _clear_plugin_modules()


def _trust(dir_name: str) -> None:
    d = next(x for x in discover_plugins() if x.dir_name == dir_name)
    trust_plugin(d)


def _module_key(plugin_name: str) -> str:
    """The exact ``sys.modules`` key ``PluginRegistry.activate_target`` uses for the
    provider module declared in this test file's manifests, so tests can assert
    it either was or was not actually imported (the real side-effect signal)."""
    return f"_lionagi_plugin_{plugin_name}__providers_endpoint.py"


def _write_provider_plugin(
    write_plugin,
    dir_name: str,
    *,
    name: str | None = None,
    provider: str = "acme-llm",
    trust: bool = True,
):
    plugin_name = name or dir_name
    bundle = write_plugin(
        dir_name,
        MANIFEST.format(name=plugin_name),
        files={"providers/endpoint.py": PROVIDER_MODULE.format(provider=provider)},
    )
    if trust:
        _trust(dir_name)
    return bundle


class TestPluginProviderHit:
    def test_active_plugin_supplies_the_missing_provider(self, write_plugin):
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "PluginProviderEndpoint"
        assert result.config.provider == "acme-llm"
        assert _module_key("web-research") in sys.modules

    def test_re_running_the_match_finds_the_now_registered_endpoint(self, write_plugin):
        """The second call (after the plugin module import) must return the concrete
        plugin-registered class, not the generic fallback that a naive one-shot scan
        would have produced before the import fired."""
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(first).__name__ == type(second).__name__ == "PluginProviderEndpoint"

    def test_changed_provider_file_withdraws_an_already_registered_endpoint(self, write_plugin):
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm"
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        (bundle / "providers" / "endpoint.py").write_text(
            PROVIDER_MODULE.format(provider="acme-llm") + "\n# changed after activation\n"
        )

        assert PluginRegistry.active_provider_targets() == []
        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(first).__name__ == "PluginProviderEndpoint"
        assert type(second).__name__ == "Endpoint"
        assert second.config.provider == "acme-llm"


class TestPluginProviderRevalidationCaching:
    """``_revalidate_plugin_entry`` must rescan (via
    ``PluginRegistry.activate_target``) on a genuine miss -- first
    resolution, or after the plugin's files changed -- but reuse that result
    on repeat ``match_endpoint`` hits, not re-run the full plugin-directory
    rescan + hash pass on every call for an endpoint that already activated
    cleanly."""

    def test_repeated_hits_do_not_rescan_after_first_revalidation(self, write_plugin, monkeypatch):
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        call_count = 0
        original_activate_target = PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            nonlocal call_count
            call_count += 1
            return original_activate_target(cls, plugin_name, target)

        monkeypatch.setattr(
            PluginRegistry, "activate_target", classmethod(counting_activate_target)
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(first).__name__ == "PluginProviderEndpoint"
        calls_after_first_resolution = call_count
        assert calls_after_first_resolution > 0

        for _ in range(5):
            repeat = match_endpoint(provider="acme-llm", endpoint="chat")
            assert type(repeat).__name__ == "PluginProviderEndpoint"

        assert call_count == calls_after_first_resolution, (
            "repeated match_endpoint() hits against an unchanged plugin "
            "endpoint must reuse the cached revalidation, not re-trigger "
            "PluginRegistry.activate_target's full rescan on every call"
        )

    def test_edited_target_after_a_cached_hit_forces_a_fresh_rescan(
        self, write_plugin, monkeypatch
    ):
        """A cache hit must never outlive an actual on-disk change: editing the
        declared target file after it was already cached as valid must still
        trigger a fresh activate_target() call on the very next match()."""
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm"
        )

        call_count = 0
        original_activate_target = PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            nonlocal call_count
            call_count += 1
            return original_activate_target(cls, plugin_name, target)

        monkeypatch.setattr(
            PluginRegistry, "activate_target", classmethod(counting_activate_target)
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(first).__name__ == "PluginProviderEndpoint"
        cached_hit = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(cached_hit).__name__ == "PluginProviderEndpoint"
        calls_before_edit = call_count

        (bundle / "providers" / "endpoint.py").write_text(
            PROVIDER_MODULE.format(provider="acme-llm") + "\n# changed after activation\n"
        )

        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert call_count > calls_before_edit
        assert type(second).__name__ == "Endpoint"

    def test_target_edited_then_mtime_restored_forces_a_fresh_rescan(
        self, write_plugin, monkeypatch
    ):
        """mtime alone cannot pin content: editing a target's bytes and then
        restoring its ORIGINAL mtime (e.g. ``os.utime`` after a backup
        restore, or a deliberate attempt to dodge an mtime-only staleness
        check) must not let the fast path keep serving the entry cached as
        valid before the edit. The very next match() must still revalidate
        and observe the edit, exactly as the live-edit case above does."""
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm"
        )
        target_path = bundle / "providers" / "endpoint.py"
        original_stat = target_path.stat()

        call_count = 0
        original_activate_target = PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            nonlocal call_count
            call_count += 1
            return original_activate_target(cls, plugin_name, target)

        monkeypatch.setattr(
            PluginRegistry, "activate_target", classmethod(counting_activate_target)
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(first).__name__ == "PluginProviderEndpoint"
        cached_hit = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(cached_hit).__name__ == "PluginProviderEndpoint"
        calls_before_attack = call_count

        target_path.write_text(
            PROVIDER_MODULE.format(provider="acme-llm") + "\n# changed after activation\n"
        )
        os.utime(target_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert target_path.stat().st_mtime_ns == original_stat.st_mtime_ns, (
            "test setup must actually restore the original mtime"
        )

        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert call_count > calls_before_attack, (
            "restoring a target file's mtime after editing its content must "
            "still force a fresh activate_target() revalidation on the next "
            "match() -- mtime alone is not a valid content-pinning signal"
        )
        assert type(second).__name__ == "Endpoint", (
            "the stale, edited-but-mtime-restored plugin entry must not be served after the edit"
        )

    def test_equal_length_edit_with_static_ctime_still_forces_a_fresh_rescan(
        self, write_plugin, monkeypatch
    ):
        """Neither mtime nor ctime alone is a portable content-pinning
        signal. mtime can be restored with ``os.utime()``; ctime is not even
        a metadata-change token everywhere -- current CPython documents
        ``st_ctime``/``st_ctime_ns`` as file *creation* time on Windows, so a
        content write or ``os.utime()`` never advances it there. An attacker
        (or a naive backup/restore tool) that performs a same-length
        in-place edit, restores the original mtime, and leaves ctime static
        must still be detected: the fast path must fall back to a content
        digest whenever its stat signature claims nothing changed, not trust
        the stat tuple by itself. This reproduces the Windows/static-ctime
        case on any host by freezing the ctime this test observes back to
        its pre-edit value after performing the edit.
        """
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm"
        )
        target_path = bundle / "providers" / "endpoint.py"
        original_stat = target_path.stat()

        call_count = 0
        original_activate_target = PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            nonlocal call_count
            call_count += 1
            return original_activate_target(cls, plugin_name, target)

        monkeypatch.setattr(
            PluginRegistry, "activate_target", classmethod(counting_activate_target)
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(first).__name__ == "PluginProviderEndpoint"
        cached_hit = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(cached_hit).__name__ == "PluginProviderEndpoint"
        calls_before_attack = call_count

        original_bytes = target_path.read_bytes()
        edited_bytes = original_bytes.replace(b"pass", b"PASS", 1)
        assert edited_bytes != original_bytes
        assert len(edited_bytes) == len(original_bytes), (
            "test setup must keep the edit equal-length so size cannot betray it"
        )
        target_path.write_bytes(edited_bytes)
        os.utime(target_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        edited_stat = target_path.stat()
        assert edited_stat.st_size == original_stat.st_size, (
            "test setup must actually preserve the original size"
        )
        assert edited_stat.st_mtime_ns == original_stat.st_mtime_ns, (
            "test setup must actually restore the original mtime"
        )
        assert edited_stat.st_ino == original_stat.st_ino, (
            "test setup must actually preserve the original inode"
        )

        # Freeze what this file reports for st_ctime_ns back to its pre-edit
        # value from here on, so this test reproduces the Windows/
        # static-ctime case regardless of what the host filesystem actually
        # does to ctime for an in-place write + os.utime().
        real_stat = pathlib.Path.stat
        frozen_ctime_ns = original_stat.st_ctime_ns

        def frozen_ctime_stat(self, *args, **kwargs):
            result = real_stat(self, *args, **kwargs)
            if self == target_path:
                return SimpleNamespace(
                    st_mtime_ns=result.st_mtime_ns,
                    st_ctime_ns=frozen_ctime_ns,
                    st_size=result.st_size,
                    st_ino=result.st_ino,
                )
            return result

        monkeypatch.setattr(pathlib.Path, "stat", frozen_ctime_stat)

        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert call_count > calls_before_attack, (
            "a same-length in-place edit whose mtime is restored and whose "
            "ctime never advances (the Windows case) must still force a "
            "fresh activate_target() revalidation on the next match() -- "
            "the fast path must confirm with a content digest whenever "
            "its stat signature alone claims nothing changed"
        )
        assert type(second).__name__ == "Endpoint", (
            "the stale, edited-but-signature-preserved plugin entry must not be served after the edit"
        )

    def test_edited_sibling_declared_file_forces_a_fresh_rescan(self, write_plugin, monkeypatch):
        bundle = write_plugin(
            "wr",
            (
                "name: web-research\n"
                'version: "0.1.0"\n'
                'lionagi: ">=0.0,<100.0"\n\n'
                "capabilities:\n"
                "  providers:\n"
                "    - module: providers/endpoint.py\n"
                "    - module: providers/sibling.py\n"
            ),
            files={
                "providers/endpoint.py": PROVIDER_MODULE.format(provider="acme-llm"),
                "providers/sibling.py": PROVIDER_MODULE.format(provider="acme-sibling"),
            },
        )
        _trust("wr")

        call_count = 0
        original_activate_target = PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            nonlocal call_count
            call_count += 1
            return original_activate_target(cls, plugin_name, target)

        monkeypatch.setattr(
            PluginRegistry, "activate_target", classmethod(counting_activate_target)
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        cached_hit = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(first).__name__ == type(cached_hit).__name__ == "PluginProviderEndpoint"
        calls_before_edit = call_count

        (bundle / "providers" / "sibling.py").write_text(
            PROVIDER_MODULE.format(provider="acme-sibling") + "\n# changed after activation\n"
        )

        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert call_count > calls_before_edit
        assert type(second).__name__ == "Endpoint"

    def test_edited_settings_forces_a_fresh_rescan(self, write_plugin, monkeypatch):
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        call_count = 0
        original_activate_target = PluginRegistry.activate_target.__func__

        def counting_activate_target(cls, plugin_name, target):
            nonlocal call_count
            call_count += 1
            return original_activate_target(cls, plugin_name, target)

        monkeypatch.setattr(
            PluginRegistry, "activate_target", classmethod(counting_activate_target)
        )

        first = match_endpoint(provider="acme-llm", endpoint="chat")
        cached_hit = match_endpoint(provider="acme-llm", endpoint="chat")
        assert type(first).__name__ == type(cached_hit).__name__ == "PluginProviderEndpoint"
        calls_before_edit = call_count

        settings_path = user_settings_path()
        previous_stat = settings_path.stat()
        settings = read_user_settings()
        settings.setdefault("plugins", {})["web-research"] = {"enabled": False}
        write_user_settings(settings)
        if settings_path.stat().st_mtime_ns == previous_stat.st_mtime_ns:
            os.utime(
                settings_path,
                ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns + 1_000_000_000),
            )

        second = match_endpoint(provider="acme-llm", endpoint="chat")

        assert call_count > calls_before_edit
        assert type(second).__name__ == "Endpoint"


class TestPluginProviderConsultConcurrency:
    def test_activation_and_collision_filtering_are_serialized(self, monkeypatch):
        first_inside_activation = threading.Event()
        release_first = threading.Event()
        second_attempting_consult = threading.Event()
        second_inside_activation = threading.Event()
        errors: list[BaseException] = []
        targets = {
            "registry-first": ("plugin-first", "providers/first.py", "thread-first-provider"),
            "registry-second": (
                "plugin-second",
                "providers/second.py",
                "thread-second-provider",
            ),
        }

        def active_provider_targets(cls):
            plugin_name, module, _ = targets[threading.current_thread().name]
            return [(plugin_name, module)]

        def activate_target(cls, plugin_name, module):
            _, _, provider = targets[threading.current_thread().name]
            module_name = f"_registry_concurrency_{plugin_name}"
            endpoint_cls = type("PluginProviderEndpoint", (), {"__module__": module_name})
            EndpointRegistry.register(provider=provider, endpoint="chat")(endpoint_cls)
            if threading.current_thread().name == "registry-first":
                first_inside_activation.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("timed out waiting to finish the first activation")
            else:
                second_inside_activation.set()
            return SimpleNamespace(__name__=module_name)

        monkeypatch.setattr(
            PluginRegistry, "active_provider_targets", classmethod(active_provider_targets)
        )
        monkeypatch.setattr(PluginRegistry, "activate_target", classmethod(activate_target))

        def consult(*, mark_attempt: bool = False):
            try:
                if mark_attempt:
                    second_attempting_consult.set()
                EndpointRegistry._consult_plugin_providers()
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=consult, name="registry-first", daemon=True)
        second = threading.Thread(
            target=consult,
            kwargs={"mark_attempt": True},
            name="registry-second",
            daemon=True,
        )
        first.start()
        assert first_inside_activation.wait(timeout=5)
        second.start()
        assert second_attempting_consult.wait(timeout=5)
        try:
            assert not second_inside_activation.wait(timeout=0.5), (
                "a second consultation entered activation before the first mutation completed"
            )
        finally:
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        registered = {entry.meta.provider for entry in EndpointRegistry._entries}
        assert {"thread-first-provider", "thread-second-provider"} <= registered


class TestPluginProviderMiss:
    def test_genuinely_unknown_provider_falls_back_identically(self, write_plugin):
        # An active plugin exists but declares an unrelated provider -- a miss
        # for a provider no plugin declares must produce the same fallback
        # shape as when no plugin is installed at all.
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")

        result = match_endpoint(provider="totally-unknown", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "totally-unknown"
        assert result.config.endpoint == "chat"
        assert result.config.auth_type == "bearer"
        assert result.config.content_type == "application/json"

    def test_no_plugins_installed_falls_back_identically(self, write_plugin):
        # `write_plugin` only gives us the isolated HOME; write nothing.
        result = match_endpoint(provider="totally-unknown", endpoint="")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "totally-unknown"
        assert result.config.endpoint == "chat/completions"
        assert result.config.auth_type == "bearer"
        assert result.config.content_type == "application/json"


class TestPluginProviderExclusion:
    def test_untrusted_plugin_is_not_consulted(self, write_plugin):
        _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm", trust=False
        )

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert result.config.provider == "acme-llm"
        assert _module_key("web-research") not in sys.modules

    def test_disabled_plugin_is_not_consulted(self, write_plugin):
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="acme-llm")
        settings = read_user_settings()
        settings.setdefault("plugins", {})["web-research"] = {"enabled": False}
        write_user_settings(settings)
        PluginRegistry.reset()

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert _module_key("web-research") not in sys.modules

    def test_changed_plugin_is_not_consulted(self, write_plugin):
        # Edit the declared provider file *after* trust was recorded but
        # before anything ever asked the registry about this plugin -- the
        # content-pinned hash mismatch reverts it to `changed` at discovery.
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-llm"
        )
        (bundle / "providers" / "endpoint.py").write_text(
            PROVIDER_MODULE.format(provider="acme-llm") + "\n# tampered after trust\n"
        )

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert _module_key("web-research") not in sys.modules

    def test_incompatible_version_range_is_not_consulted(self, write_plugin):
        bundle = write_plugin(
            "wr",
            (
                'name: web-research\nversion: "0.1.0"\nlionagi: ">=999.0"\n\n'
                "capabilities:\n  providers:\n    - module: providers/endpoint.py\n"
            ),
            files={"providers/endpoint.py": PROVIDER_MODULE.format(provider="acme-llm")},
        )
        _trust("wr")

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "Endpoint"
        assert _module_key("web-research") not in sys.modules


class TestPluginProviderStaleSnapshotRegression:
    def test_manifest_target_replacement_resolves_without_a_registry_reset(self, write_plugin):
        """``PluginRegistry._snapshot`` is process-cached; a plugin can be edited to
        declare a *different* provider module and re-trusted without anyone calling
        ``PluginRegistry.reset()``. The old target's own failure (no longer declared)
        must not poison resolution of the new one -- enumeration has to come from the
        same fresh rescan as the trust check, not the stale cached manifest."""
        bundle = _write_provider_plugin(
            write_plugin, "wr", name="web-research", provider="acme-old"
        )

        # Freeze the process-cached snapshot (state=ACTIVE, manifest declaring
        # providers/endpoint.py) via an unrelated call, before anything changes.
        assert PluginRegistry.get("web-research").state is PluginState.ACTIVE

        (bundle / "providers" / "endpoint_b.py").write_text(
            PROVIDER_MODULE.format(provider="acme-new")
        )
        (bundle / "plugin.yaml").write_text(
            MANIFEST.format(name="web-research").replace(
                "providers/endpoint.py", "providers/endpoint_b.py"
            )
        )
        _trust("wr")

        # The stale, no-longer-declared target fails on its own merits...
        with pytest.raises(PluginActivationError, match="not declared"):
            PluginRegistry.activate_target("web-research", "providers/endpoint.py")

        # ...but that failure must not block resolution of the provider the
        # manifest actually declares now, with no PluginRegistry.reset() in between.
        result = match_endpoint(provider="acme-new", endpoint="chat")

        assert type(result).__name__ == "PluginProviderEndpoint"
        assert result.config.provider == "acme-new"


class TestBuiltinProviderRestoration:
    def test_builtin_endpoint_remains_registered_after_plugin_provider_tests(self):
        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        result = match_endpoint(provider="openai", endpoint="chat")

        assert isinstance(result, OpenaiChatEndpoint)


class TestPluginProviderBuiltinCollision:
    """ADR-0088 D6: a plugin provider must never silently shadow an
    already-registered built-in. ``EndpointRegistry._reject_builtin_collisions``
    drops the colliding plugin entry and logs a named diagnostic instead."""

    def test_builtin_wins_when_a_plugin_declares_the_same_provider(self, write_plugin, caplog):
        import logging

        from lionagi.providers.openai.chat import OpenaiChatEndpoint

        # "openai" collides with a built-in; the plugin also declares an
        # unrelated provider so a genuine miss (below) triggers import of
        # both -- mirroring how a real plugin's provider module gets
        # imported regardless of which specific provider is being resolved.
        _write_provider_plugin(write_plugin, "wr", name="web-research", provider="openai")

        with caplog.at_level(logging.WARNING, logger="lionagi.service.connections.registry"):
            # A miss on an unrelated provider is what drives `match()` to
            # consult (and thus import) every active plugin provider target,
            # including the one that collides with "openai".
            match_endpoint(provider="totally-unrelated", endpoint="chat")

        assert "web-research" in caplog.text
        assert "openai" in caplog.text

        result = match_endpoint(provider="openai", endpoint="chat")
        assert isinstance(result, OpenaiChatEndpoint)

    def test_sibling_noncolliding_provider_in_the_same_manifest_still_resolves(self, write_plugin):
        """A collision on one declared provider must not take down a sibling,
        non-colliding provider declared by the same plugin manifest."""
        write_plugin(
            "wr",
            (
                "name: web-research\n"
                'version: "0.1.0"\n'
                'lionagi: ">=0.0,<100.0"\n\n'
                "capabilities:\n"
                "  providers:\n"
                "    - module: providers/colliding.py\n"
                "    - module: providers/ok.py\n"
            ),
            files={
                "providers/colliding.py": PROVIDER_MODULE.format(provider="openai"),
                "providers/ok.py": PROVIDER_MODULE.format(provider="acme-llm"),
            },
        )
        _trust("wr")

        result = match_endpoint(provider="acme-llm", endpoint="chat")

        assert type(result).__name__ == "PluginProviderEndpoint"
        assert result.config.provider == "acme-llm"

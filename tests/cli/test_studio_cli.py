# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li studio` CLI entry point (H-BE-2).

Verifies that invoking the studio command without the explicit `start`
subcommand does not raise AttributeError for missing --port / --host /
--frontend-mode / --no-frontend attributes on the argparse Namespace.

uvicorn.run is mocked so the server is never actually started.
"""

from __future__ import annotations

from unittest.mock import patch


def test_studio_bare_invocation_does_not_raise(monkeypatch):
    """``main(["studio"])`` must not raise AttributeError (H-BE-2).

    Without the explicit `start` subcommand, argparse never populates
    --port, --host, --frontend-mode, or --no-frontend on the Namespace.
    The fix uses getattr() with defaults so the dereference is safe.
    """
    # Prevent the real uvicorn server from starting.
    with patch("uvicorn.run") as mock_run:
        from lionagi.cli.main import main

        # Should complete without AttributeError or SystemExit.
        result = main(["studio"])

    assert result == 0
    mock_run.assert_called_once()


def test_studio_start_explicit_subcommand_does_not_raise(monkeypatch):
    """``main(["studio", "start"])`` must also work (regression guard)."""
    with patch("uvicorn.run") as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "start"])

    assert result == 0
    mock_run.assert_called_once()


def test_studio_start_with_port_flag(monkeypatch):
    """``main(["studio", "start", "--port", "9000"])`` passes port to uvicorn."""
    with patch("uvicorn.run") as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "start", "--port", "9000"])

    assert result == 0
    _, kwargs = mock_run.call_args
    assert kwargs.get("port") == 9000


def test_studio_bare_uses_default_port(monkeypatch):
    """Bare ``li studio`` must fall back to port 8765 (or env override)."""
    monkeypatch.delenv("LIONAGI_STUDIO_PORT", raising=False)
    with patch("uvicorn.run") as mock_run:
        from lionagi.cli.main import main

        result = main(["studio"])

    assert result == 0
    _, kwargs = mock_run.call_args
    assert kwargs.get("port") == 8765


# ─── #1201: studio cwd / module resolution fix ───


def test_find_repo_root_returns_path_from_source_checkout():
    """_find_repo_root returns a path when run from the source tree."""
    from lionagi.cli.studio import _find_repo_root

    root = _find_repo_root()
    # In CI / source checkout the apps/studio dir exists → root is not None.
    # In a pure wheel install it will be None — both are valid outcomes.
    if root is not None:
        assert (root / "apps" / "studio").is_dir()


def test_ensure_apps_importable_from_non_repo_cwd(tmp_path, monkeypatch):
    """_ensure_apps_importable returns False when outside the repo (no apps/ dir)."""
    import lionagi.cli.studio as studio_mod

    # Fake _find_repo_root to return None (simulating installed wheel).
    monkeypatch.setattr(studio_mod, "_find_repo_root", lambda: None)
    result = studio_mod._ensure_apps_importable()
    assert result is False


def test_ensure_apps_importable_adds_repo_root_to_sys_path(monkeypatch):
    """_ensure_apps_importable adds repo root to sys.path when in source tree."""
    import sys

    import lionagi.cli.studio as studio_mod

    fake_root = monkeypatch.getfixturevalue("tmp_path") if False else None
    # Use a real Path-like object to avoid monkeypatching Path.
    from pathlib import Path

    fake_root = Path("/tmp/fake-lion-repo")

    def fake_find_repo_root():
        return fake_root

    monkeypatch.setattr(studio_mod, "_find_repo_root", fake_find_repo_root)
    # Remove it if already present so we can observe the insertion.
    root_str = str(fake_root)
    if root_str in sys.path:
        sys.path.remove(root_str)

    result = studio_mod._ensure_apps_importable()
    assert result is True
    assert root_str in sys.path


# ─── _is_build_stale staleness predicate ─────────────────────────────────────


def _write_marker(frontend_dir):
    """Create dist/index.html — the Vite build marker."""
    dist = frontend_dir / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text("<!doctype html>")


def test_is_build_stale_returns_true_when_dist_absent(tmp_path):
    """dist/index.html absent → stale (no prior build)."""
    from lionagi.cli.studio import _is_build_stale

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_returns_false_when_no_source_newer_than_marker(tmp_path):
    """All source files older than dist/index.html → not stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    # Create source files first (older).
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "index.html").write_text("<html/>")

    # Give a small real gap so mtime ordering is reliable.
    time.sleep(0.02)

    _write_marker(tmp_path)

    assert _is_build_stale(tmp_path) is False


def test_is_build_stale_returns_true_when_source_file_newer_than_marker(tmp_path):
    """A source file newer than dist/index.html → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)

    time.sleep(0.02)

    # Source file written after (newer).
    (tmp_path / "package.json").write_text("{}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_detects_nested_source_change(tmp_path):
    """A file nested under src/ that is newer than dist/index.html → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)

    time.sleep(0.02)

    # Nested source file created after.
    routes_dir = tmp_path / "src" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "index.tsx").write_text("export const Route = null")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_ignores_unrelated_directories(tmp_path):
    """Files outside tracked source trees don't trigger a rebuild."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)

    time.sleep(0.02)

    # File in an untracked directory written after.
    other_dir = tmp_path / "public"
    other_dir.mkdir()
    (other_dir / "logo.svg").write_text("<svg/>")

    assert _is_build_stale(tmp_path) is False


def test_is_build_stale_vite_config_change_triggers_rebuild(tmp_path):
    """vite.config.mts newer than the marker → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)

    time.sleep(0.02)

    (tmp_path / "vite.config.mts").write_text("export default {}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_package_lock_change_triggers_rebuild(tmp_path):
    """package-lock.json newer than the marker → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "package-lock.json").write_text("{}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_tsconfig_change_triggers_rebuild(tmp_path):
    """tsconfig.json newer than the marker → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "tsconfig.json").write_text("{}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_tailwind_config_triggers_rebuild(tmp_path):
    """tailwind.config.ts newer than the marker → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "tailwind.config.ts").write_text("export default {}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_postcss_config_triggers_rebuild(tmp_path):
    """postcss.config.cjs newer than the marker → stale."""
    import time

    from lionagi.cli.studio import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "postcss.config.cjs").write_text("module.exports = {}")

    assert _is_build_stale(tmp_path) is True


# ─── _needs_npm_install tests ─────────────────────────────────────────────────


def test_needs_npm_install_when_node_modules_absent(tmp_path):
    """node_modules/ absent → install required."""
    from lionagi.cli.studio import _needs_npm_install

    assert _needs_npm_install(tmp_path) is True


def test_needs_npm_install_when_vite_bin_absent(tmp_path):
    """node_modules/ present but .bin/vite absent → install required."""
    from lionagi.cli.studio import _needs_npm_install

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".bin").mkdir()
    # vite binary intentionally not created

    assert _needs_npm_install(tmp_path) is True


def test_needs_npm_install_false_when_up_to_date(tmp_path):
    """node_modules/ with vite + package.json older than install marker → no install."""
    import time

    from lionagi.cli.studio import _needs_npm_install

    # Create package.json first (older)
    (tmp_path / "package.json").write_text("{}")
    time.sleep(0.02)

    # Create node_modules with vite and install marker (newer)
    nm = tmp_path / "node_modules"
    nm.mkdir()
    bin_dir = nm / ".bin"
    bin_dir.mkdir()
    (bin_dir / "vite").write_text("#!/bin/sh")
    (nm / ".package-lock.json").write_text("{}")

    assert _needs_npm_install(tmp_path) is False


def test_needs_npm_install_when_package_json_newer(tmp_path):
    """package.json newer than install marker → install required."""
    import time

    from lionagi.cli.studio import _needs_npm_install

    # Create install marker first (older)
    nm = tmp_path / "node_modules"
    nm.mkdir()
    bin_dir = nm / ".bin"
    bin_dir.mkdir()
    (bin_dir / "vite").write_text("#!/bin/sh")
    (nm / ".package-lock.json").write_text("{}")

    time.sleep(0.02)

    # package.json written after (newer)
    (tmp_path / "package.json").write_text("{}")

    assert _needs_npm_install(tmp_path) is True


def test_needs_npm_install_when_package_lock_newer(tmp_path):
    """package-lock.json newer than install marker → install required."""
    import time

    from lionagi.cli.studio import _needs_npm_install

    nm = tmp_path / "node_modules"
    nm.mkdir()
    bin_dir = nm / ".bin"
    bin_dir.mkdir()
    (bin_dir / "vite").write_text("#!/bin/sh")
    (nm / ".package-lock.json").write_text("{}")

    time.sleep(0.02)

    (tmp_path / "package-lock.json").write_text("{}")

    assert _needs_npm_install(tmp_path) is True


def test_ensure_frontend_built_installs_when_vite_missing(tmp_path, monkeypatch):
    """_ensure_frontend_built triggers npm install when .bin/vite is absent."""
    from unittest.mock import MagicMock, patch

    import lionagi.cli.studio as studio_mod

    # Set up a node_modules without vite (triggers _needs_npm_install)
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / ".bin").mkdir()
    # No vite binary

    install_calls = []
    build_calls = []

    def fake_run(cmd, **kwargs):
        if "install" in cmd:
            install_calls.append(cmd)
            # Simulate successful install: create vite binary + install marker
            (nm / ".bin" / "vite").write_text("#!/bin/sh")
            (nm / ".package-lock.json").write_text("{}")
        elif "vite" in cmd and "build" in cmd:
            build_calls.append(cmd)
            # Simulate successful build: create dist/index.html
            dist = tmp_path / "dist"
            dist.mkdir(exist_ok=True)
            (dist / "index.html").write_text("<!doctype html>")
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr(studio_mod.subprocess, "run", fake_run)

    result = studio_mod._ensure_frontend_built(tmp_path)

    assert result is True
    assert len(install_calls) == 1, "npm install must be called once"

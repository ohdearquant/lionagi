# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li studio` CLI entry point: bare invocation, start subcommand, port flags, and frontend build staleness."""

from __future__ import annotations

import contextlib
import os
from unittest.mock import patch


@contextlib.contextmanager
def _stubbed_serve():
    """Stub uvicorn.run and _ensure_frontend_built; restores env vars that the real CLI mutates (xdist isolation)."""
    saved = {k: os.environ.get(k) for k in ("LIONAGI_STUDIO_FRONTEND_DIST", "LIONAGI_STUDIO_HOST")}
    try:
        with (
            patch("uvicorn.run") as mock_run,
            patch("lionagi.studio.cli._ensure_frontend_built", return_value=False),
        ):
            yield mock_run
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_studio_bare_invocation_does_not_raise(monkeypatch):
    """``main(["studio"])`` must not raise AttributeError when argparse omits --port/--host/--frontend-mode."""
    # Prevent the real uvicorn server (and a real frontend build) from starting.
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        # Should complete without AttributeError or SystemExit.
        result = main(["studio"])

    assert result == 0
    mock_run.assert_called_once()


def test_studio_start_explicit_subcommand_does_not_raise(monkeypatch):
    """``main(["studio", "start"])`` must also work (regression guard)."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "start"])

    assert result == 0
    mock_run.assert_called_once()


def test_studio_start_with_port_flag(monkeypatch):
    """``main(["studio", "start", "--port", "9000"])`` passes port to uvicorn."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "start", "--port", "9000"])

    assert result == 0
    _, kwargs = mock_run.call_args
    assert kwargs.get("port") == 9000


def test_studio_bare_uses_default_port(monkeypatch):
    """Bare ``li studio`` must fall back to port 8765 (or env override)."""
    monkeypatch.delenv("LIONAGI_STUDIO_PORT", raising=False)
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio"])

    assert result == 0
    _, kwargs = mock_run.call_args
    assert kwargs.get("port") == 8765


# ─── frontend-mode flags: --web (default) / --docker / --no-frontend ────────


def test_studio_bare_defaults_to_hosted_web_mode(capsys):
    """Bare ``li studio`` prints the hosted URL and starts the backend only."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio"])

    assert result == 0
    mock_run.assert_called_once()
    out = capsys.readouterr().out
    assert "https://lion-studio.khive.ai" in out
    assert "127.0.0.1:8765" in out


def test_studio_web_flag_matches_default(capsys):
    """``li studio --web`` is explicit but behaves identically to bare invocation."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "--web"])

    assert result == 0
    mock_run.assert_called_once()
    assert "https://lion-studio.khive.ai" in capsys.readouterr().out


def test_studio_web_does_not_build_local_frontend():
    """--web must never call the local frontend builder."""
    with (
        patch("uvicorn.run"),
        patch("lionagi.studio.cli._ensure_frontend_built") as mock_build,
    ):
        from lionagi.cli.main import main

        result = main(["studio", "--web"])

    assert result == 0
    mock_build.assert_not_called()


def test_studio_web_opens_browser_when_interactive(monkeypatch):
    """A TTY session opens the hosted URL unless --no-open is set."""
    import lionagi.studio.cli as studio_cli

    monkeypatch.setattr(studio_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(studio_cli.sys.stdout, "isatty", lambda: True)
    with patch("webbrowser.open") as mock_open, patch("uvicorn.run"):
        from lionagi.cli.main import main

        result = main(["studio", "--web"])

    assert result == 0
    mock_open.assert_called_once_with("https://lion-studio.khive.ai")


def test_studio_web_no_open_flag_suppresses_browser(monkeypatch):
    """--no-open skips opening a browser even in an interactive session."""
    import lionagi.studio.cli as studio_cli

    monkeypatch.setattr(studio_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(studio_cli.sys.stdout, "isatty", lambda: True)
    with patch("webbrowser.open") as mock_open, patch("uvicorn.run"):
        from lionagi.cli.main import main

        result = main(["studio", "--web", "--no-open"])

    assert result == 0
    mock_open.assert_not_called()


def test_studio_no_frontend_flag_skips_hosted_messaging(capsys):
    """--no-frontend stays backend-only with no hosted-URL messaging."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "--no-frontend"])

    assert result == 0
    mock_run.assert_called_once()
    assert "lion-studio.khive.ai" not in capsys.readouterr().out


def test_studio_docker_flag_invokes_docker_path():
    """--docker dispatches to the Docker launch path, not the hosted or local one."""
    with (
        patch("lionagi.studio.cli._has_docker", return_value=True),
        patch("lionagi.studio.cli._start_docker", return_value=0) as mock_docker,
        patch("uvicorn.run"),
    ):
        from lionagi.cli.main import main

        result = main(["studio", "--docker"])

    assert result == 0
    mock_docker.assert_called_once()


def test_studio_docker_flag_without_docker_installed_errors(capsys):
    """--docker without the docker binary available fails loudly instead of falling back."""
    with patch("lionagi.studio.cli._has_docker", return_value=False), patch("uvicorn.run"):
        from lionagi.cli.main import main

        result = main(["studio", "--docker"])

    assert result == 1
    assert "Docker not found" in capsys.readouterr().err


def test_studio_mode_flags_are_mutually_exclusive():
    """Combining two mode flags (e.g. --web and --docker) is a usage error."""
    import pytest

    from lionagi.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(["studio", "--web", "--docker"])
    assert exc_info.value.code == 2


def test_studio_mode_flag_before_start_is_preserved():
    """`li studio --docker start` must take the Docker path (subparser defaults must not clobber parent flags)."""
    with (
        patch("lionagi.studio.cli._has_docker", return_value=True),
        patch("lionagi.studio.cli._start_docker", return_value=0) as mock_docker,
        patch("uvicorn.run"),
    ):
        from lionagi.cli.main import main

        result = main(["studio", "--docker", "start"])

    assert result == 0
    mock_docker.assert_called_once()


def test_studio_no_open_before_start_is_preserved():
    """`li studio --no-open start` must not open a browser."""
    with (
        _stubbed_serve(),
        patch("webbrowser.open") as mock_open,
        patch("sys.stdout.isatty", return_value=True),
    ):
        from lionagi.cli.main import main

        result = main(["studio", "--no-open", "start"])

    assert result == 0
    mock_open.assert_not_called()


def test_studio_port_before_start_is_preserved():
    """`li studio --port 9001 start` keeps the parent-level port."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "--port", "9001", "start"])

    assert result == 0
    assert mock_run.call_args.kwargs.get("port") == 9001


def test_studio_cross_level_mode_flags_are_mutually_exclusive():
    """Mode flags split across parser levels (`li studio --docker start --web`) must be rejected."""
    import pytest

    from lionagi.cli.main import main

    for argv in (
        ["studio", "--docker", "start", "--web"],
        ["studio", "--web", "start", "--docker"],
        ["studio", "--no-frontend", "start", "--dev"],
    ):
        with pytest.raises(SystemExit):
            main(argv)


# ─── studio cwd / module resolution ─────────────


def test_find_repo_root_returns_path_from_source_checkout():
    """_find_repo_root returns a path when run from the source tree."""
    from lionagi.studio.cli import _find_repo_root

    root = _find_repo_root()
    # In CI / source checkout the apps/studio dir exists → root is not None.
    # In a pure wheel install it will be None — both are valid outcomes.
    if root is not None:
        assert (root / "apps" / "studio").is_dir()


def test_ensure_apps_importable_from_non_repo_cwd(tmp_path, monkeypatch):
    """_ensure_apps_importable returns False when outside the repo (no apps/ dir)."""
    import lionagi.studio.cli as studio_mod

    # Fake _find_repo_root to return None (simulating installed wheel).
    monkeypatch.setattr(studio_mod, "_find_repo_root", lambda: None)
    result = studio_mod._ensure_apps_importable()
    assert result is False


def test_ensure_apps_importable_adds_repo_root_to_sys_path(monkeypatch):
    """_ensure_apps_importable adds repo root to sys.path when in source tree."""
    import sys

    import lionagi.studio.cli as studio_mod

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
    from lionagi.studio.cli import _is_build_stale

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_returns_false_when_no_source_newer_than_marker(tmp_path):
    """All source files older than dist/index.html → not stale."""
    import time

    from lionagi.studio.cli import _is_build_stale

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

    from lionagi.studio.cli import _is_build_stale

    _write_marker(tmp_path)

    time.sleep(0.02)

    # Source file written after (newer).
    (tmp_path / "package.json").write_text("{}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_detects_nested_source_change(tmp_path):
    """A file nested under src/ that is newer than dist/index.html → stale."""
    import time

    from lionagi.studio.cli import _is_build_stale

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

    from lionagi.studio.cli import _is_build_stale

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

    from lionagi.studio.cli import _is_build_stale

    _write_marker(tmp_path)

    time.sleep(0.02)

    (tmp_path / "vite.config.mts").write_text("export default {}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_package_lock_change_triggers_rebuild(tmp_path):
    """package-lock.json newer than the marker → stale."""
    import time

    from lionagi.studio.cli import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "package-lock.json").write_text("{}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_tsconfig_change_triggers_rebuild(tmp_path):
    """tsconfig.json newer than the marker → stale."""
    import time

    from lionagi.studio.cli import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "tsconfig.json").write_text("{}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_tailwind_config_triggers_rebuild(tmp_path):
    """tailwind.config.ts newer than the marker → stale."""
    import time

    from lionagi.studio.cli import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "tailwind.config.ts").write_text("export default {}")

    assert _is_build_stale(tmp_path) is True


def test_is_build_stale_postcss_config_triggers_rebuild(tmp_path):
    """postcss.config.cjs newer than the marker → stale."""
    import time

    from lionagi.studio.cli import _is_build_stale

    _write_marker(tmp_path)
    time.sleep(0.02)
    (tmp_path / "postcss.config.cjs").write_text("module.exports = {}")

    assert _is_build_stale(tmp_path) is True


# ─── _needs_npm_install tests ─────────────────────────────────────────────────


def test_needs_npm_install_when_node_modules_absent(tmp_path):
    """node_modules/ absent → install required."""
    from lionagi.studio.cli import _needs_npm_install

    assert _needs_npm_install(tmp_path) is True


def test_needs_npm_install_when_vite_bin_absent(tmp_path):
    """node_modules/ present but .bin/vite absent → install required."""
    from lionagi.studio.cli import _needs_npm_install

    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".bin").mkdir()
    # vite binary intentionally not created

    assert _needs_npm_install(tmp_path) is True


def test_needs_npm_install_false_when_up_to_date(tmp_path):
    """node_modules/ with vite + package.json older than install marker → no install."""
    import time

    from lionagi.studio.cli import _needs_npm_install

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

    from lionagi.studio.cli import _needs_npm_install

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

    from lionagi.studio.cli import _needs_npm_install

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

    import lionagi.studio.cli as studio_mod

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

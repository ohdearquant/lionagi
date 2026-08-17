# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li studio` CLI entry point: bare invocation, start subcommand, port flags, and frontend build staleness."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Real Vite fixture for the binding/host-selection tests below: they prove
# claims about actual Vite startup output rather than a hand-built fixture,
# which could hide a real parsing defect.
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "apps" / "studio" / "frontend"
_VITE_BIN = _FRONTEND_DIR / "node_modules" / ".bin" / "vite"
requires_real_vite = pytest.mark.skipif(
    not _VITE_BIN.exists(),
    reason="apps/studio/frontend/node_modules not installed; run `npm install` there first",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


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


# frontend-mode flags: --web (default) / --docker / --no-frontend


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
        patch("sys.stdin.isatty", return_value=True),
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


def test_studio_no_docker_flag_is_deprecated_warn_and_ignore(capsys):
    """--no-docker no longer exists as a mode; it must warn and fall through to hosted mode."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "--no-docker"])

    assert result == 0
    mock_run.assert_called_once()
    err = capsys.readouterr().err
    assert "--no-docker is deprecated and ignored" in err


def test_studio_no_docker_flag_before_start_is_preserved(capsys):
    """`li studio --no-docker start` still warns and preserves parent-level flag handling."""
    with _stubbed_serve() as mock_run:
        from lionagi.cli.main import main

        result = main(["studio", "--no-docker", "start"])

    assert result == 0
    mock_run.assert_called_once()
    assert "--no-docker is deprecated and ignored" in capsys.readouterr().err


def test_studio_no_docker_combines_with_a_real_mode_flag(capsys):
    """--no-docker must be ignorable alongside a real mode flag, not itself a mode.

    Pins that --no-docker lives outside the mode mutual-exclusion group: `--no-docker --docker`
    warns-and-ignores and still dispatches to the Docker path (a regression that moved the flag
    into the exclusion group would make this a usage error instead)."""
    with (
        patch("lionagi.studio.cli._has_docker", return_value=True),
        patch("lionagi.studio.cli._start_docker", return_value=0) as mock_docker,
        patch("uvicorn.run"),
    ):
        from lionagi.cli.main import main

        result = main(["studio", "--no-docker", "--docker"])

    assert result == 0
    mock_docker.assert_called_once()
    assert "--no-docker is deprecated and ignored" in capsys.readouterr().err


def test_studio_cross_level_mode_flags_are_mutually_exclusive():
    """Mode flags split across parser levels (`li studio --docker start --web`) must be rejected."""
    import pytest

    from lionagi.cli.main import main

    for argv in (
        ["studio", "--docker", "start", "--web"],
        ["studio", "--web", "start", "--docker"],
        ["studio", "--no-frontend", "start", "--dev"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            main(argv)
        assert exc_info.value.code == 2


# studio cwd / module resolution


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


# _is_build_stale staleness predicate


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


# _needs_npm_install tests


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


def test_vite_dev_argv_binds_the_same_host_used_for_the_printed_url():
    """The spawn argv and the printed URL must come from one host/port source of truth."""
    from lionagi.studio.cli import _vite_dev_argv

    argv = _vite_dev_argv(5174, "127.0.0.1")

    assert argv == ["npx", "vite", "--port", "5174", "--host", "127.0.0.1"]


def test_vite_dev_argv_uses_the_given_host_not_a_hardcoded_default():
    from lionagi.studio.cli import _vite_dev_argv

    argv = _vite_dev_argv(3000, "0.0.0.0")

    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "0.0.0.0"


def test_parse_vite_local_url_extracts_bound_address():
    from lionagi.studio.cli import _parse_vite_local_url

    line = "  ➜  Local:   http://127.0.0.1:5174/\n"

    assert _parse_vite_local_url(line) == "http://127.0.0.1:5174"


def test_parse_vite_local_url_reflects_an_auto_incremented_port():
    """Vite bumps to the next free port when the requested one is taken; the
    parsed URL must reflect that real port, not the one that was requested."""
    from lionagi.studio.cli import _parse_vite_local_url

    line = "  ➜  Local:   http://127.0.0.1:5175/\n"

    assert _parse_vite_local_url(line) == "http://127.0.0.1:5175"


def test_parse_vite_local_url_ignores_unrelated_lines():
    from lionagi.studio.cli import _parse_vite_local_url

    assert _parse_vite_local_url("  VITE v5.4.10  ready in 328 ms\n") is None
    assert _parse_vite_local_url("  ➜  Network: use --host to expose\n") is None
    assert _parse_vite_local_url("") is None


def test_parse_vite_local_url_strips_ansi_color_codes():
    """npx/Vite can color this line even with a piped (non-TTY) stdout; the
    escape codes sit right around `Local:` and the URL and must not defeat
    the match, or every colored run silently falls back to a guessed URL."""
    from lionagi.studio.cli import _parse_vite_local_url

    line = "  \x1b[32m➜\x1b[39m  \x1b[1mLocal\x1b[22m:   \x1b[36mhttp://127.0.0.1:5174/\x1b[39m\n"

    assert _parse_vite_local_url(line) == "http://127.0.0.1:5174"


def test_await_vite_ready_url_returns_the_parsed_startup_address():
    from unittest.mock import MagicMock

    from lionagi.studio.cli import _await_vite_ready_url

    proc = MagicMock()
    proc.stdout = iter(
        [
            "  VITE v5.4.10  ready in 328 ms\n",
            "  ➜  Local:   http://127.0.0.1:5175/\n",
        ]
    )

    url = _await_vite_ready_url(proc, host="127.0.0.1", timeout=5.0)

    assert url == "http://127.0.0.1:5175"


def test_await_vite_ready_url_returns_none_when_nothing_matches():
    """If Vite never logs a Local: line before the timeout (crash, unexpected
    output format), report failure honestly instead of guessing a URL nothing
    may be listening on."""
    from unittest.mock import MagicMock

    from lionagi.studio.cli import _await_vite_ready_url

    proc = MagicMock()
    proc.stdout = iter(["  some unrelated output\n"])

    url = _await_vite_ready_url(proc, host="127.0.0.1", timeout=1.0)

    assert url is None


def test_await_vite_ready_url_uses_the_first_of_multiple_network_lines(caplog):
    """Vite can print more than one Network: line (multiple interfaces);
    selection must be deterministic (the first one) and the caller must be
    told a choice was made rather than silently picking one."""
    from unittest.mock import MagicMock

    from lionagi.studio.cli import _await_vite_ready_url

    proc = MagicMock()
    proc.stdout = iter(
        [
            "  ➜  Local:   http://localhost:5175/\n",
            "  ➜  Network: http://192.168.1.10:5175/\n",
            "  ➜  Network: http://100.64.0.5:5175/\n",
        ]
    )

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        url = _await_vite_ready_url(proc, host="0.0.0.0", timeout=5.0)

    assert url == "http://192.168.1.10:5175"
    assert any("2 Network" in r.message for r in caplog.records)


def test_launch_vite_dev_passes_host_through_to_argv_and_returns_the_bound_url(
    tmp_path, monkeypatch
):
    """End-to-end (mocked Popen): the host given to _launch_vite_dev reaches the
    spawn argv, and the returned URL is the one parsed from Vite's own output.

    Wiring only — a non-loopback host selects `Network:`, not `Local:` (see
    the real-Vite tests for that host-selection claim proved against actual
    Vite output rather than a hand-built fixture)."""
    import lionagi.studio.cli as studio_mod

    captured_argv = {}

    class FakeProc:
        def __init__(self):
            self.stdout = iter(
                [
                    "  ➜  Local:   http://localhost:4001/\n",
                    "  ➜  Network: http://192.168.1.50:4001/\n",
                ]
            )

    def fake_popen(argv, **kwargs):
        captured_argv["argv"] = argv
        return FakeProc()

    monkeypatch.setattr(studio_mod.subprocess, "Popen", fake_popen)

    result = studio_mod._launch_vite_dev(tmp_path, 4000, host="0.0.0.0")

    assert result is not None
    proc, url = result
    assert isinstance(proc, FakeProc)
    assert captured_argv["argv"] == ["npx", "vite", "--port", "4000", "--host", "0.0.0.0"]
    assert url == "http://192.168.1.50:4001"


def test_start_local_propagates_selected_backend_to_vite(tmp_path, monkeypatch):
    """The ``--port`` value in scope at the CLI call site must reach the launcher."""
    import lionagi.studio.cli as studio_mod

    monkeypatch.setattr(studio_mod.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(studio_mod, "_ensure_apps_importable", lambda: True)
    with (
        patch("uvicorn.run"),
        patch.object(studio_mod, "_launch_vite_dev", return_value=None) as launch,
    ):
        result = studio_mod._start_local("127.0.0.1", 45241, 4000, tmp_path, dev_mode=True)

    assert result == 0
    launch.assert_called_once_with(
        tmp_path,
        4000,
        host="127.0.0.1",
        api_host="127.0.0.1",
        api_port=45241,
    )


def test_launch_vite_dev_passes_selected_backend_to_proxy_env(tmp_path, monkeypatch):
    """The backend selected by ``li studio --dev`` must become Vite's proxy target."""
    import lionagi.studio.cli as studio_mod

    captured_env = {}

    class FakeProc:
        stdout = iter(["  ➜  Local:   http://127.0.0.1:4000/\n"])

    def fake_popen(argv, **kwargs):
        captured_env.update(kwargs["env"])
        return FakeProc()

    monkeypatch.delenv("STUDIO_API_URL", raising=False)
    monkeypatch.setattr(studio_mod.subprocess, "Popen", fake_popen)

    result = studio_mod._launch_vite_dev(
        tmp_path,
        4000,
        host="127.0.0.1",
        api_host="127.0.0.1",
        api_port=45241,
    )

    assert result is not None
    assert captured_env["STUDIO_API_URL"] == "http://127.0.0.1:45241"


def test_launch_vite_dev_preserves_operator_proxy_override(tmp_path, monkeypatch):
    """An explicit operator target wins over the CLI-selected host and port."""
    import lionagi.studio.cli as studio_mod

    captured_env = {}

    class FakeProc:
        stdout = iter(["  ➜  Local:   http://127.0.0.1:4000/\n"])

    def fake_popen(argv, **kwargs):
        captured_env.update(kwargs["env"])
        return FakeProc()

    operator_target = "http://127.0.0.1:46117"
    monkeypatch.setenv("STUDIO_API_URL", operator_target)
    monkeypatch.setattr(studio_mod.subprocess, "Popen", fake_popen)

    result = studio_mod._launch_vite_dev(
        tmp_path,
        4000,
        host="127.0.0.1",
        api_host="127.0.0.1",
        api_port=45241,
    )

    assert result is not None
    assert captured_env["STUDIO_API_URL"] == operator_target


def test_launch_vite_dev_warns_and_returns_none_when_npx_missing(tmp_path, monkeypatch, capsys):
    import lionagi.studio.cli as studio_mod

    def fake_popen(argv, **kwargs):
        raise FileNotFoundError("npx not found")

    monkeypatch.setattr(studio_mod.subprocess, "Popen", fake_popen)

    result = studio_mod._launch_vite_dev(tmp_path, 4000, host="127.0.0.1")

    assert result is None
    assert "npx not found" in capsys.readouterr().err


@requires_real_vite
@pytest.mark.integration
@pytest.mark.slow
def test_vite_proxy_reaches_marker_on_cli_selected_non_default_port(monkeypatch):
    """A real Vite proxy must read the daemon selected by ``--port``, not 8765."""
    import json
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import lionagi.studio.cli as studio_mod

    marker = "studio-dev-port-3136"

    class MarkerHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"marker": marker}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    daemon = ThreadingHTTPServer(("127.0.0.1", 0), MarkerHandler)
    daemon_thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    daemon_thread.start()
    proc = None
    monkeypatch.delenv("STUDIO_API_URL", raising=False)
    try:
        api_port = daemon.server_address[1]
        assert api_port != 8765
        result = studio_mod._launch_vite_dev(
            _FRONTEND_DIR,
            _free_port(),
            host="127.0.0.1",
            api_host="127.0.0.1",
            api_port=api_port,
        )
        assert result is not None
        proc, url = result
        assert url is not None
        with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
            payload = json.load(response)
        assert payload == {"marker": marker}
    finally:
        _stop(proc)
        daemon.shutdown()
        daemon.server_close()
        daemon_thread.join(timeout=5)


@requires_real_vite
@pytest.mark.integration
@pytest.mark.slow
def test_launch_vite_dev_resolves_the_incremented_port_on_a_real_collision():
    """When the requested port is already bound, real Vite auto-increments to
    the next free one; the returned URL must carry that real port, not the
    one that was requested (a hand-built fixture can't prove auto-increment
    actually happens — this starts a real process against an occupied port)."""
    import lionagi.studio.cli as studio_mod

    requested_port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", requested_port))
    blocker.listen(1)
    proc = None
    try:
        result = studio_mod._launch_vite_dev(_FRONTEND_DIR, requested_port, host="127.0.0.1")
        assert result is not None
        proc, url = result
        assert url is not None
        bound_port = int(url.rsplit(":", 1)[1])
        assert bound_port > requested_port
    finally:
        blocker.close()
        _stop(proc)


@requires_real_vite
@pytest.mark.integration
@pytest.mark.slow
def test_launch_vite_dev_selects_the_local_url_for_a_loopback_host():
    """Real Vite launched with --host 127.0.0.1 only ever prints a Local:
    line; the loopback URL must be selected."""
    import lionagi.studio.cli as studio_mod

    proc = None
    try:
        result = studio_mod._launch_vite_dev(_FRONTEND_DIR, _free_port(), host="127.0.0.1")
        assert result is not None
        proc, url = result
        assert url is not None
        host = url.split("://", 1)[1].rsplit(":", 1)[0]
        assert host in ("127.0.0.1", "localhost")
    finally:
        _stop(proc)


@requires_real_vite
@pytest.mark.integration
@pytest.mark.slow
def test_launch_vite_dev_selects_a_lan_url_for_the_wildcard_host():
    """Real Vite launched with --host 0.0.0.0 prints both a loopback Local:
    line and one or more LAN Network: lines; the selected URL must be a
    Network: address, not the loopback Local: line (r1 finding: LAN binding
    was silently reduced to loopback because only Local: was ever parsed)."""
    import lionagi.studio.cli as studio_mod

    proc = None
    try:
        result = studio_mod._launch_vite_dev(_FRONTEND_DIR, _free_port(), host="0.0.0.0")
        assert result is not None
        proc, url = result
        assert url is not None
        host = url.split("://", 1)[1].rsplit(":", 1)[0]
        assert host not in ("127.0.0.1", "localhost")
    finally:
        _stop(proc)


def test_await_vite_ready_url_returns_fast_and_reports_exit_code_on_early_exit(caplog):
    """A child that exits immediately (crash, missing entry point, bad cwd)
    must not stall the readiness wait for the full timeout — the reader
    thread learns about EOF promptly, and the caller reports the exit code
    plus captured output instead of a generic "check the output" message
    with nothing to check."""
    from lionagi.studio.cli import _await_vite_ready_url

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "print('starting up'); import sys; sys.exit(3)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        url = _await_vite_ready_url(proc, host="127.0.0.1", timeout=10.0)
    elapsed = time.monotonic() - start
    proc.wait(timeout=5)

    assert url is None
    assert elapsed < 2.0, f"took {elapsed:.2f}s — must not stall for the full timeout"
    messages = [r.message for r in caplog.records]
    assert any("exit code 3" in m for m in messages), messages
    assert any("starting up" in m for m in messages), messages


def test_host_is_loopback_classifies_numeric_and_named_loopback_hosts():
    """The whole 127.0.0.0/8 block is loopback, not just 127.0.0.1, and so
    is any ::1 form — classification must be semantic (via `ipaddress`),
    not an exact-string allowlist."""
    from lionagi.studio.cli import _host_is_loopback

    assert _host_is_loopback("127.0.0.1") is True
    assert _host_is_loopback("127.0.0.2") is True
    assert _host_is_loopback("::1") is True
    assert _host_is_loopback("localhost") is True


def test_host_is_loopback_rejects_wildcard_lan_and_hostname():
    from lionagi.studio.cli import _host_is_loopback

    assert _host_is_loopback("0.0.0.0") is False
    assert _host_is_loopback("::") is False
    assert _host_is_loopback("192.168.1.5") is False
    assert _host_is_loopback("my-machine.example.com") is False


def test_launch_vite_dev_selects_the_local_url_for_a_non_default_loopback_host(
    tmp_path, monkeypatch
):
    """127.0.0.2 is a valid loopback address but not the exact string
    "127.0.0.1"; it must still route to the Local: parser and resolve to
    the address Vite actually printed."""
    import lionagi.studio.cli as studio_mod

    class FakeProc:
        def __init__(self):
            self.stdout = iter(["  ➜  Local:   http://127.0.0.2:4001/\n"])

    monkeypatch.setattr(studio_mod.subprocess, "Popen", lambda argv, **kwargs: FakeProc())

    result = studio_mod._launch_vite_dev(tmp_path, 4000, host="127.0.0.2")

    assert result is not None
    proc, url = result
    assert url == "http://127.0.0.2:4001"

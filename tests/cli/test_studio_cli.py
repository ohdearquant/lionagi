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

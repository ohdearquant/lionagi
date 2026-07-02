# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression test: `li schedule` CLI requests must hit routes Studio serves.

Guards against client/server path-prefix drift. Schedule routes are
registered via @studio_route("/schedules/...") and every registered route is
mounted at f"/api{route.path}" (see lionagi/studio/app.py), so the served
prefix is /api/schedules. The CLI's _api() helper previously built URLs as
f"{_base_url()}/schedules{path}" (missing the /api prefix), so every
`li schedule` subcommand 404'd against a running `li studio` daemon.

This exercises the real argparse + dispatch + _api() URL construction for
every CLI subcommand, stubbing only the socket boundary
(urllib.request.urlopen), then checks the resulting (method, path) against
the live FastAPI app's actual route table — so a future prefix or path
change on either side fails this test instead of silently 404ing at runtime.
"""

from __future__ import annotations

import argparse
from urllib.parse import urlsplit

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

# Import the live app at module level — before any fixture elsewhere in the
# test session might clear the studio route registry — mirroring the
# safe-import pattern in tests/apps_studio_server/test_route_registry.py.
from starlette.routing import Match  # noqa: E402

from lionagi.studio.app import app as _live_app  # noqa: E402


def _is_served(method: str, path: str) -> bool:
    """True if some route on the live app fully matches (method, path)."""
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in _live_app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return True
    return False


class _FakeResponse:
    """Stand-in for the `with urllib.request.urlopen(...) as resp:` context."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _run_and_capture(monkeypatch, args: argparse.Namespace) -> tuple[str, str]:
    """Run a real `li schedule` dispatch, capturing the (method, path) the
    CLI's _api() helper would have sent — without any real network I/O."""
    from lionagi.studio.cli import run_schedule

    captured: dict[str, str] = {}

    def _fake_urlopen(req, timeout=10):  # noqa: ARG001
        captured["method"] = req.get_method()
        captured["path"] = urlsplit(req.full_url).path
        return _FakeResponse(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    result = run_schedule(args)
    assert result == 0, f"dispatch failed unexpectedly: action={args.schedule_action}"
    return captured["method"], captured["path"]


def _parse(argv: list[str]) -> argparse.Namespace:
    from lionagi.studio.cli import add_schedule_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_schedule_subparser(sub)
    return parser.parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["schedule", "list"],
        ["schedule", "get", "sched-abc"],
        ["schedule", "create", "my-sched", "--cron", "0 * * * *", "--prompt", "ping"],
        ["schedule", "enable", "sched-abc"],
        ["schedule", "disable", "sched-abc"],
        ["schedule", "trigger", "sched-abc"],
        ["schedule", "delete", "sched-abc"],
        ["schedule", "runs", "sched-abc"],
    ],
    ids=["list", "get", "create", "enable", "disable", "trigger", "delete", "runs"],
)
def test_schedule_cli_paths_are_served(monkeypatch, argv):
    """Every `li schedule` subcommand's request must resolve against a real
    Studio route (method + path), not just look plausible."""
    args = _parse(argv)
    method, path = _run_and_capture(monkeypatch, args)
    assert _is_served(method, path), (
        f"li schedule {argv[1]} builds {method} {path}, which is not served "
        f"by the live Studio app — client/server route drift"
    )

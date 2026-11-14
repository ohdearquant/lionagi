# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Human schedule transport adapter behavior."""

from __future__ import annotations

import argparse
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lionagi.ln import _ssrf
from lionhub.serve.cli import schedule as schedule_cli


class _FailingUrlopen:
    def __init__(self, error: OSError) -> None:
        self.error = error
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float):
        self.calls.append((request.get_method(), request.full_url, timeout))
        raise self.error


def test_human_schedule_transport_keeps_connection_refused_restart_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    urlopen = _FailingUrlopen(urllib.error.URLError(ConnectionRefusedError("connection refused")))
    monkeypatch.setattr(_ssrf, "open_url", urlopen)

    assert schedule_cli._api("/") is None

    assert urlopen.calls == [
        ("GET", "http://127.0.0.1:8765/api/schedules/", 10),
    ]
    message = capsys.readouterr().err
    assert "Cannot reach Studio at http://127.0.0.1:8765" in message
    assert "is `li studio` running?" in message


def test_human_schedule_mutation_timeout_reports_uncertain_completion_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr(
        schedule_cli,
        "time",
        SimpleNamespace(monotonic=iter((100.0, 110.25)).__next__),
    )
    urlopen = _FailingUrlopen(TimeoutError("timed out"))
    monkeypatch.setattr(_ssrf, "open_url", urlopen)

    assert schedule_cli._api("/sched-1", method="DELETE") is None

    assert urlopen.calls == [
        ("DELETE", "http://127.0.0.1:8765/api/schedules/sched-1", 10),
    ]
    message = capsys.readouterr().err
    assert "timed out (elapsed 10.2s; limit 10s)" in message
    assert "may still have completed" in message
    assert "Cannot reach" not in message
    assert "`li studio`" not in message
    assert "restart" not in message.lower()

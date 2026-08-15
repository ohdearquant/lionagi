# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Schedule HTTP transport failures preserve the operator-safe distinction."""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from lionagi.cli.machine import MachineError


class _FailingUrlopen:
    def __init__(self, error: OSError) -> None:
        self.error = error
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float):
        self.calls.append((request.get_method(), request.full_url, timeout))
        raise self.error


class _SlowStudio(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []

    def do_DELETE(self) -> None:  # noqa: N802 — stdlib handler API
        self.requests.append(("DELETE", self.path))
        time.sleep(0.25)

    def log_message(self, *args: object) -> None:
        pass


def test_human_schedule_transport_keeps_connection_refused_restart_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lionagi.studio.cli as schedule_cli

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    urlopen = _FailingUrlopen(urllib.error.URLError(ConnectionRefusedError("connection refused")))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

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
    import lionagi.studio.cli as schedule_cli

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr(
        schedule_cli,
        "time",
        SimpleNamespace(monotonic=iter((100.0, 110.25)).__next__),
    )
    urlopen = _FailingUrlopen(TimeoutError("timed out"))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

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


def test_live_but_slow_studio_is_a_timeout_not_an_unreachable_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lionagi.studio.cli as schedule_cli

    _SlowStudio.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowStudio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LIONAGI_STUDIO_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(schedule_cli, "_SCHEDULE_API_TIMEOUT_SECONDS", 0.05)

    try:
        assert schedule_cli._api("/sched-1", method="DELETE") is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert _SlowStudio.requests == [("DELETE", "/api/schedules/sched-1")]
    message = capsys.readouterr().err
    assert "timed out (elapsed " in message
    assert "; limit 0.05s)" in message
    assert "may still have completed" in message
    assert "Cannot reach" not in message
    assert "`li studio`" not in message


def test_machine_schedule_transport_keeps_connection_refused_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lionagi.cli.machine_schedule as machine_schedule

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    urlopen = _FailingUrlopen(urllib.error.URLError(ConnectionRefusedError("connection refused")))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(MachineError) as raised:
        machine_schedule._studio("/")

    assert raised.value.kind == "unavailable"
    assert "could not reach Studio at http://127.0.0.1:8765" in str(raised.value)
    assert "nothing was read or written" in str(raised.value)
    assert raised.value.detail is None


def test_machine_schedule_mutation_timeout_is_structured_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lionagi.cli.machine_schedule as machine_schedule

    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr(
        machine_schedule,
        "time",
        SimpleNamespace(monotonic=iter((200.0, 215.5)).__next__),
    )
    urlopen = _FailingUrlopen(urllib.error.URLError(TimeoutError("timed out")))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(MachineError) as raised:
        machine_schedule._studio("/sched-1/disable", method="POST")

    assert urlopen.calls == [
        ("POST", "http://127.0.0.1:8765/api/schedules/sched-1/disable", 15.0),
    ]
    assert raised.value.kind == "unavailable"
    message = str(raised.value)
    assert "timed out (elapsed 15.5s; limit 15s)" in message
    assert "may still have completed" in message
    assert "could not reach" not in message
    assert "nothing was read or written" not in message
    assert "`li studio`" not in message
    assert "restart" not in message.lower()
    assert raised.value.detail == {
        "reason": "request_timeout",
        "method": "POST",
        "path": "/api/schedules/sched-1/disable",
        "elapsed_seconds": 15.5,
        "limit_seconds": 15.0,
        "completion": "unknown",
    }

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The CLI's Studio clients send the daemon's bearer when LIONAGI_STUDIO_AUTH_TOKEN is set,
never print it, and never carry it to the target of a redirect."""

from __future__ import annotations

import io
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lionagi.ln import _ssrf
from lionhub.orchestration.schedule.parsing import STUDIO_TOKEN_ENV, with_studio_bearer
from lionhub.serve.cli import doctor, machine_schedule
from lionhub.serve.cli import schedule as schedule_cli
from lionhub.serve.cli.machine import MachineError

TOKEN = "example-bearer-value"  # noqa: S105 — a test value, never a real credential


class _Recorder:
    """Stands in for open_url: records each request's bearer, then answers or raises."""

    def __init__(self, *, status: int = 200, body: bytes = b"[]") -> None:
        self.status = status
        self.body = body
        self.authorizations: list[str | None] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float):
        self.authorizations.append(request.get_header("Authorization"))
        if self.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                self.status,
                "refused",
                {},
                io.BytesIO(b'{"detail":"Unauthorized"}'),
            )
        response = io.BytesIO(self.body)
        response.status = self.status  # type: ignore[attr-defined]
        return response


def test_the_bearer_is_added_only_when_the_token_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    request = with_studio_bearer(urllib.request.Request("http://127.0.0.1:8765/api/schedules/"))
    assert request.unredirected_hdrs == {"Authorization": f"Bearer {TOKEN}"}
    assert "Authorization" not in request.headers

    for unset in ("", None):
        if unset is None:
            monkeypatch.delenv(STUDIO_TOKEN_ENV, raising=False)
        else:
            monkeypatch.setenv(STUDIO_TOKEN_ENV, unset)
        bare = with_studio_bearer(urllib.request.Request("http://127.0.0.1:8765/api/schedules/"))
        assert bare.get_header("Authorization") is None


@pytest.mark.parametrize("token", [TOKEN, None])
def test_every_studio_client_sends_the_bearer_exactly_when_set(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    if token:
        monkeypatch.setenv(STUDIO_TOKEN_ENV, token)
    else:
        monkeypatch.delenv(STUDIO_TOKEN_ENV, raising=False)
    recorder = _Recorder(body=b'{"status": "ready"}')
    monkeypatch.setattr(_ssrf, "open_url", recorder)

    schedule_cli._api("/")
    machine_schedule._studio("/")
    doctor._check_studio_daemon("http://127.0.0.1:8765/api/admin/readiness")

    expected = f"Bearer {token}" if token else None
    assert recorder.authorizations == [expected, expected, expected]


def test_a_401_names_the_variable_and_never_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(_ssrf, "open_url", _Recorder(status=401))

    assert schedule_cli._api("/") is None
    human = capsys.readouterr().err
    with pytest.raises(MachineError) as raised:
        machine_schedule._studio("/")
    verdict = doctor._check_studio_daemon("http://127.0.0.1:8765/api/admin/readiness")

    for text in (human, str(raised.value), verdict["detail"]):
        assert STUDIO_TOKEN_ENV in text
        assert TOKEN not in text
    assert verdict["status"] == "warn"


class _Redirecting(BaseHTTPRequestHandler):
    target = ""
    seen: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 — stdlib handler API
        self.seen.append(self.headers.get("Authorization"))
        self.send_response(302)
        self.send_header("Location", self.target)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class _Landing(BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def do_GET(self) -> None:  # noqa: N802 — stdlib handler API
        self.seen.append(self.headers.get("Authorization"))
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def test_the_bearer_does_not_follow_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    _Redirecting.seen, _Landing.seen = [], []
    landing = ThreadingHTTPServer(("127.0.0.1", 0), _Landing)
    first = ThreadingHTTPServer(("127.0.0.1", 0), _Redirecting)
    _Redirecting.target = f"http://127.0.0.1:{landing.server_port}/api/schedules/"
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (landing, first)]
    for thread in threads:
        thread.start()
    monkeypatch.setenv("LIONAGI_STUDIO_URL", f"http://127.0.0.1:{first.server_port}")
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    try:
        assert schedule_cli._api("/") == []
    finally:
        for server in (first, landing):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)

    assert _Redirecting.seen == [f"Bearer {TOKEN}"]
    assert _Landing.seen == [None]

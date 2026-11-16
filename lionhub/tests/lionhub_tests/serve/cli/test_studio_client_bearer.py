# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The CLI's Studio clients send the daemon's bearer when LIONAGI_STUDIO_AUTH_TOKEN is set,
only to the configured Studio, never print it, and never carry it to the target of a redirect."""

from __future__ import annotations

import ast
import io
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lionagi.ln import _ssrf
from lionhub.serve.cli import doctor, machine_schedule, studio_client
from lionhub.serve.cli import schedule as schedule_cli
from lionhub.serve.cli.machine import MachineError
from lionhub.serve.cli.studio_client import STUDIO_TOKEN_ENV, studio_request

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


@pytest.fixture(autouse=True)
def _default_studio(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LIONAGI_STUDIO_URL", "LIONAGI_STUDIO_HOST", "LIONAGI_STUDIO_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(studio_client, "_warned_cleartext_origins", set())


def test_the_bearer_is_an_unredirected_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    request = studio_request("http://127.0.0.1:8765/api/schedules/")
    assert request.unredirected_hdrs == {"Authorization": f"Bearer {TOKEN}"}
    assert "Authorization" not in request.headers


@pytest.mark.parametrize("unset", ["", None], ids=["empty", "absent"])
def test_no_bearer_without_a_token(monkeypatch: pytest.MonkeyPatch, unset: str | None) -> None:
    if unset is None:
        monkeypatch.delenv(STUDIO_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(STUDIO_TOKEN_ENV, unset)
    assert (
        studio_request("http://127.0.0.1:8765/api/schedules/").get_header("Authorization") is None
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8766/api/admin/readiness",
        "https://127.0.0.1:8765/api/admin/readiness",
        "http://localhost:8765/api/admin/readiness",
        "http://studio.example:8765/api/admin/readiness",
    ],
    ids=["other-port", "other-scheme", "other-spelling", "other-host"],
)
def test_the_bearer_goes_only_to_the_configured_studio(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    assert studio_request(url).get_header("Authorization") is None


def test_a_doctor_probe_of_another_address_carries_no_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "https://studio.example")
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    recorder = _Recorder(body=b'{"status": "ready"}')
    monkeypatch.setattr(_ssrf, "open_url", recorder)

    doctor._check_studio_daemon()

    assert recorder.authorizations == [None]


def test_cleartext_to_another_host_warns_once_and_names_no_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_URL", "http://studio.example:8765")
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)

    for _ in range(2):
        request = studio_request("http://studio.example:8765/api/schedules/")
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"

    [warning] = [r.getMessage() for r in caplog.records if "plain http" in r.getMessage()]
    assert STUDIO_TOKEN_ENV in warning
    assert TOKEN not in warning


@pytest.mark.parametrize(
    "base", ["http://127.0.0.1:8765", "http://localhost:8765", "https://studio.example"]
)
def test_loopback_or_https_sends_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, base: str
) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_URL", base)
    monkeypatch.setenv(STUDIO_TOKEN_ENV, TOKEN)
    assert studio_request(f"{base}/api/schedules/").get_header("Authorization") is not None
    assert not [r for r in caplog.records if "plain http" in r.getMessage()]


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


@pytest.mark.parametrize("status", [403, 500])
def test_doctor_reports_what_a_reachable_daemon_answered(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(_ssrf, "open_url", _Recorder(status=status))
    verdict = doctor._check_studio_daemon()
    assert verdict["status"] == "warn"
    assert f"returned HTTP {status}" in verdict["detail"]
    assert "unreachable" not in verdict["detail"]


_SRC = Path(studio_client.__file__).resolve().parents[3]
#: The one Studio route a client reads without the bearer: the daemon serves it to anyone.
_PUBLIC_HEALTH_PROBE = "lionhub/serve/cli/studio.py"


def _calls(tree: ast.AST) -> set[str]:
    """The dotted name of every call in *tree* (``urllib.request.Request``, ``open_url``)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            parts, func = [], node.func
            while isinstance(func, ast.Attribute):
                parts.append(func.attr)
                func = func.value
            if isinstance(func, ast.Name):
                parts.append(func.id)
            names.add(".".join(reversed(parts)))
    return names


def test_every_studio_request_is_built_by_the_shared_factory() -> None:
    """A module that opens a URL builds its request with studio_request, so none forgets the
    bearer; the only exception is the probe of the daemon's public health route."""
    openers, builders = [], []
    for path in sorted(_SRC.rglob("*.py")):
        calls = _calls(ast.parse(path.read_text(encoding="utf-8")))
        relative = path.relative_to(_SRC).as_posix()
        if calls & {"urllib.request.Request", "Request", "urllib.request.urlopen", "urlopen"}:
            builders.append(relative)
        if "open_url" in calls:
            openers.append(relative)
            if relative != _PUBLIC_HEALTH_PROBE:
                assert "studio_request" in calls, f"{relative} opens a URL it did not build"
    assert builders == ["lionhub/serve/cli/studio_client.py"]
    # Positive control: the walk reached every client this test guards.
    assert {
        "lionhub/serve/cli/schedule.py",
        "lionhub/serve/cli/machine_schedule.py",
        "lionhub/serve/cli/doctor.py",
        _PUBLIC_HEALTH_PROBE,
    } <= set(openers)


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

# SPDX-License-Identifier: Apache-2.0
"""Requests to a daemon on this machine ignore proxy settings.

``urlopen`` sends every request through the proxy named in the environment (or, on
macOS, in the system settings), loopback addresses included. A proxy cannot reach a
daemon on this machine's own loopback interface, so a running Studio daemon would be
reported as absent.

These tests set every proxy variable, run the real urllib opener chain, and replace only
the HTTP connection class. The replacement records the address each request is sent to
and refuses every address except the ones the test names as reachable, so a request
that is sent to the proxy fails the way it does against a real proxy that cannot serve
the daemon. No socket is opened.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.parse
import urllib.request

import pytest

from lionagi.cli.doctor import _check_studio_daemon
from lionagi.cli.machine import MachineError
from lionagi.cli.machine_schedule import _studio
from lionagi.studio.cli import _api, _daemon_health

_PROXY_VARS = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "ALL_PROXY", "all_proxy")
_PROXY_HOST = "127.0.0.1:3128"
_DAEMON_HOST = "127.0.0.1:8765"
_DAEMON_URL = f"http://{_DAEMON_HOST}"

_ROUTES = {
    "/health": (200, "OK", {"status": "ok"}),
    "/api/admin/readiness": (200, "OK", {"status": "healthy", "detail": "store answered"}),
    "/api/schedules/": (200, "OK", {"schedules": []}),
}


class _RawSocket:
    """Hands ``http.client`` the bytes of one response."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def makefile(self, mode: str = "rb", *args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(self._data)


def _http_message(status: int, reason: str, body: dict) -> bytes:
    payload = json.dumps(body).encode()
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + payload


class _Wire:
    """Where each request was addressed, and which addresses answer."""

    def __init__(self, reachable: set[str]) -> None:
        self.reachable = reachable
        self.hosts: list[str] = []
        self.selectors: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wire = self

        class Connection:
            # urllib reads this class attribute when it builds its HTTP handler.
            debuglevel = 0

            def __init__(self, host: str, timeout: object = None, **kwargs: object) -> None:
                self.host = host
                self.sock = None
                self._selector = ""
                wire.hosts.append(host)

            def set_debuglevel(self, level: int) -> None:
                pass

            def close(self) -> None:
                pass

            def request(
                self,
                method: str,
                selector: str,
                body: object = None,
                headers: object = None,
                **kwargs: object,
            ) -> None:
                self._selector = selector
                wire.selectors.append(selector)
                if self.host not in wire.reachable:
                    raise ConnectionRefusedError(61, "Connection refused")

            def getresponse(self) -> http.client.HTTPResponse:
                path = urllib.parse.urlsplit(self._selector).path
                status, reason, body = _ROUTES.get(
                    path, (404, "Not Found", {"detail": "no such route"})
                )
                raw = _RawSocket(_http_message(status, reason, body))
                response = http.client.HTTPResponse(raw)
                response.begin()
                return response

        monkeypatch.setattr(http.client, "HTTPConnection", Connection)


@pytest.fixture
def serve(monkeypatch):
    """Set a proxy in every variable, then answer only at the addresses given."""
    for name in _PROXY_VARS:
        monkeypatch.setenv(name, f"http://{_PROXY_HOST}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    # urlopen keeps the opener it built from the settings of its first call.
    monkeypatch.setattr(urllib.request, "_opener", None)

    def start(*reachable: str) -> _Wire:
        wire = _Wire(set(reachable))
        wire.install(monkeypatch)
        return wire

    return start


def test_daemon_health_sees_a_loopback_daemon_past_a_proxy(serve):
    wire = serve(_DAEMON_HOST)

    assert _daemon_health(8765) == {"status": "ok"}

    assert wire.hosts == [_DAEMON_HOST]


@pytest.mark.parametrize(
    ("host", "authority"),
    [
        ("127.0.0.1", "127.0.0.1:8765"),
        ("localhost", "localhost:8765"),
        ("::1", "[::1]:8765"),
    ],
)
def test_daemon_health_reaches_each_loopback_spelling_past_a_proxy(serve, host, authority):
    wire = serve(authority)

    assert _daemon_health(8765, host=host) == {"status": "ok"}

    assert wire.hosts == [authority]


def test_schedule_api_client_reaches_a_loopback_daemon_past_a_proxy(serve, monkeypatch):
    wire = serve(_DAEMON_HOST)
    monkeypatch.setenv("LIONAGI_STUDIO_URL", _DAEMON_URL)

    assert _api("/") == {"schedules": []}

    assert wire.hosts == [_DAEMON_HOST]


def test_schedule_api_client_reports_an_http_error_from_a_loopback_daemon(
    serve, monkeypatch, capsys
):
    wire = serve(_DAEMON_HOST)
    monkeypatch.setenv("LIONAGI_STUDIO_URL", _DAEMON_URL)

    assert _api("/missing") is None

    assert "Error 404" in capsys.readouterr().err
    assert wire.hosts == [_DAEMON_HOST]


def test_doctor_check_reaches_a_loopback_daemon_past_a_proxy(serve):
    wire = serve(_DAEMON_HOST)

    result = _check_studio_daemon(f"{_DAEMON_URL}/api/admin/readiness")

    assert result["status"] == "ok", result
    assert wire.hosts == [_DAEMON_HOST]


def test_machine_schedule_client_reaches_a_loopback_daemon_past_a_proxy(serve, monkeypatch):
    wire = serve(_DAEMON_HOST)
    monkeypatch.setenv("LIONAGI_STUDIO_URL", _DAEMON_URL)

    assert _studio("/") == {"schedules": []}

    assert wire.hosts == [_DAEMON_HOST]


def test_machine_schedule_client_maps_a_loopback_404_to_not_found(serve, monkeypatch):
    wire = serve(_DAEMON_HOST)
    monkeypatch.setenv("LIONAGI_STUDIO_URL", _DAEMON_URL)

    with pytest.raises(MachineError) as caught:
        _studio("/missing")

    assert caught.value.kind == "not_found"
    assert wire.hosts == [_DAEMON_HOST]


def test_an_unreachable_loopback_daemon_is_reported_not_proxied(serve, monkeypatch):
    wire = serve()
    monkeypatch.setenv("LIONAGI_STUDIO_URL", _DAEMON_URL)

    with pytest.raises(MachineError) as caught:
        _studio("/")

    assert caught.value.kind == "unavailable"
    assert wire.hosts == [_DAEMON_HOST]


def test_a_non_loopback_host_still_goes_through_the_proxy(serve):
    wire = serve(_PROXY_HOST)

    assert _daemon_health(8765, host="studio.example.test") == {"status": "ok"}

    assert wire.hosts == [_PROXY_HOST]
    assert wire.selectors == ["http://studio.example.test:8765/health"]

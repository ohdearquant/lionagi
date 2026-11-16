# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li remote`: the remote's token stays its own, travels only to that remote, and a leg
runs only on a daemon that pins the head."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from lionhub.serve.cli import remote as remote_cli
from lionhub.serve.cli.remote import RemoteError, load_remote

TOKEN = "example-remote-value"  # noqa: S105 — a test value, never a real credential
HEAD = "0123456789abcdef0123456789abcdef01234567"
INV = "0123abcd4567"


@pytest.fixture
def remotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write ~/.lionagi/remotes.yaml for a fake home and return a writer for more entries."""
    home = tmp_path / "home"
    (home / ".lionagi").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LIONAGI_REMOTE_LEGBOX_TOKEN", TOKEN)

    def write(entries: dict) -> None:
        (home / ".lionagi" / "remotes.yaml").write_text(json.dumps(entries))

    write({"legbox": {"url": "http://127.0.0.1:1", "token_env": "LIONAGI_REMOTE_LEGBOX_TOKEN"}})
    return write


def test_a_remote_is_read_with_its_own_token(remotes) -> None:
    remote = load_remote("legbox")
    assert (remote.scheme, remote.host, remote.port, remote.token) == (
        "http",
        "127.0.0.1",
        1,
        TOKEN,
    )


@pytest.mark.parametrize(
    ("entry", "names"),
    [
        ({"url": "http://h:1", "token_env": "LEGBOX_TOKEN"}, "LIONAGI_REMOTE_"),
        ({"url": "http://h:1", "token_env": "LIONAGI_REMOTE_UNSET"}, "is not set"),
        ({"url": "ftp://h:1", "token_env": "LIONAGI_REMOTE_LEGBOX_TOKEN"}, "http"),
        ({"url": "http://u:p@h:1", "token_env": "LIONAGI_REMOTE_LEGBOX_TOKEN"}, "credentials"),
        ({"url": "http://h:1/api", "token_env": "LIONAGI_REMOTE_LEGBOX_TOKEN"}, "no path"),
        (
            {"url": "http://h:1", "token_env": "LIONAGI_REMOTE_LEGBOX_TOKEN", "token": "x"},
            "exactly",
        ),
    ],
)
def test_a_remote_that_could_leak_its_token_is_refused(remotes, entry: dict, names: str) -> None:
    remotes({"legbox": entry})
    with pytest.raises(RemoteError, match=names):
        load_remote("legbox")


def test_a_remote_never_gets_this_machines_studio_token(remotes, monkeypatch) -> None:
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", TOKEN)
    with pytest.raises(RemoteError, match="same token"):
        load_remote("legbox")


@pytest.mark.parametrize(
    ("address", "inside"),
    [
        ("100.100.1.2", True),
        ("fd7a:115c:a1e0::9", True),
        ("127.0.0.1", True),
        ("::ffff:127.0.0.1", True),
        ("192.0.2.1", False),
        ("10.0.0.5", False),
        ("::ffff:192.0.2.1", False),
    ],
)
def test_only_loopback_and_tailnet_addresses_carry_a_plain_http_token(
    address: str, inside: bool
) -> None:
    assert remote_cli._is_loopback_or_tailnet(address) is inside


class _FakeLegHost(BaseHTTPRequestHandler):
    """Answers the leg-host protocol and records each request's path, bearer and Host."""

    seen: list[tuple[str, str, str | None, str | None]] = []
    leg_host = True
    echo_head = True
    redirect = False
    statuses: list[str] = []

    def _send(self, status: int, body: object) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self) -> None:
        self.seen.append(
            (self.command, self.path, self.headers.get("Authorization"), self.headers.get("Host"))
        )

    def do_GET(self) -> None:  # noqa: N802 — stdlib handler API
        self._record()
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/")
            self.end_headers()
        elif self.path == "/api/launches/capabilities":
            self._send(
                200, {"leg_host": self.leg_host, "pinned_head": self.leg_host, "max_concurrent": 2}
            )
        elif self.path == f"/api/invocations/{INV}":
            self._send(
                200, {"id": INV, "status": self.statuses.pop(0) if self.statuses else "completed"}
            )
        elif self.path == f"/api/launches/{INV}/result":
            self._send(
                200,
                {"invocation_id": INV, "status": "completed", "result": "LGTM", "truncated": False},
            )
        else:
            self._send(404, {"detail": "no"})

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler API
        self._record()
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/launches/":
            reply = {"invocation_id": INV, "action_kind": "agent"}
            if self.echo_head:
                reply["action_head"] = body.get("action_head")
            self._send(202, reply)
        else:
            self._send(202, {"invocation_id": INV, "status": "cancelling"})

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_host(remotes):
    _FakeLegHost.seen, _FakeLegHost.statuses = [], ["running"]
    _FakeLegHost.leg_host, _FakeLegHost.echo_head, _FakeLegHost.redirect = True, True, False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLegHost)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    remotes(
        {
            "legbox": {
                "url": f"http://localhost:{server.server_port}",
                "token_env": "LIONAGI_REMOTE_LEGBOX_TOKEN",
            }
        }
    )
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _run_args(**overrides: object) -> argparse.Namespace:
    values = {
        "remote": "legbox",
        "remote_action": "run",
        "agent": "reviewer",
        "repo": "lionagi",
        "head": HEAD,
        "poll": 0.01,
        "prompt": "review",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_a_run_pins_the_head_waits_and_prints_the_result(fake_host, capsys, monkeypatch) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)
    assert remote_cli.run_remote(_run_args()) == 0
    assert capsys.readouterr().out == "LGTM\n"
    paths = [(method, path) for method, path, _auth, _host in _FakeLegHost.seen]
    assert paths[:2] == [("GET", "/api/launches/capabilities"), ("POST", "/api/launches/")]
    assert {auth for _m, _p, auth, _h in _FakeLegHost.seen} == {f"Bearer {TOKEN}"}
    # Connected by name, but the Host sent is the address the socket reached.
    assert {host for _m, _p, _a, host in _FakeLegHost.seen} == {
        f"127.0.0.1:{fake_host.server_port}"
    }
    assert TOKEN not in capsys.readouterr().err


def test_a_daemon_that_is_not_a_leg_host_gets_no_launch(fake_host, capsys, monkeypatch) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)
    _FakeLegHost.leg_host = False
    assert remote_cli.run_remote(_run_args()) == 1
    assert "not a leg host" in capsys.readouterr().err
    assert [p for _m, p, _a, _h in _FakeLegHost.seen] == ["/api/launches/capabilities"]


def test_a_launch_that_does_not_confirm_the_head_is_cancelled(
    fake_host, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)
    _FakeLegHost.echo_head = False
    assert remote_cli.run_remote(_run_args()) == 1
    assert "did not confirm head" in capsys.readouterr().err
    assert _FakeLegHost.seen[-1][:2] == ("POST", f"/api/invocations/{INV}/cancel")


def test_a_redirect_is_not_followed(fake_host, capsys, monkeypatch) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)
    _FakeLegHost.redirect = True
    assert remote_cli.run_remote(_run_args()) == 1
    assert "redirect" in capsys.readouterr().err
    assert len(_FakeLegHost.seen) == 1


def test_an_interrupted_wait_cancels_the_remote_leg(fake_host, capsys, monkeypatch) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)

    def _interrupt(remote, inv_id, poll):
        raise KeyboardInterrupt

    monkeypatch.setattr(remote_cli, "_wait", _interrupt)
    assert remote_cli.run_remote(_run_args()) == 130
    assert _FakeLegHost.seen[-1][:2] == ("POST", f"/api/invocations/{INV}/cancel")


def test_a_peer_off_the_tailnet_is_refused_before_anything_is_sent(
    fake_host, capsys, monkeypatch
) -> None:
    """The check reads the connected socket's peer; a refused peer receives no request at all."""
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)
    monkeypatch.setattr(remote_cli, "_is_loopback_or_tailnet", lambda address: False)
    assert remote_cli.run_remote(_run_args()) == 1
    assert "neither loopback nor a tailnet address" in capsys.readouterr().err
    assert _FakeLegHost.seen == []


def test_a_refused_token_names_its_variable_never_its_value(fake_host, capsys, monkeypatch) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: False)
    monkeypatch.setenv("LIONAGI_REMOTE_LEGBOX_TOKEN", "a-different-value")
    monkeypatch.setattr(
        _FakeLegHost, "do_GET", lambda self: (self._record(), self._send(401, {"detail": "no"}))
    )
    assert remote_cli.run_remote(_run_args()) == 1
    err = capsys.readouterr().err
    assert "LIONAGI_REMOTE_LEGBOX_TOKEN" in err
    assert "a-different-value" not in err


def test_an_agent_cannot_start_a_remote_leg(fake_host, monkeypatch) -> None:
    monkeypatch.setattr(remote_cli, "_refused_inside_an_agent", lambda: True)
    assert remote_cli.run_remote(_run_args()) == 1
    assert _FakeLegHost.seen == []


@pytest.mark.parametrize("head", [HEAD[:12], HEAD.upper(), "main"])
def test_only_a_full_commit_is_a_head(head: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        remote_cli._head(head)


def test_only_li_remote_opens_a_raw_http_connection() -> None:
    """A raw connection bypasses the Studio request factory's origin check; the one module that
    opens one checks the peer itself before writing its token."""
    import ast

    from lionhub.serve.cli import studio_client

    src = Path(studio_client.__file__).resolve().parents[3]
    users = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        if names & {"HTTPConnection", "HTTPSConnection"}:
            users.append(path.relative_to(src).as_posix())
    assert users == ["lionhub/serve/cli/remote.py"]

# SPDX-License-Identifier: Apache-2.0
"""The server refuses connections that an episode's own code opens."""

import asyncio

import pytest

from agent_workbench.server import app as app_module

_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)


def _table(tmp_path, rows):
    """A /proc/net/tcp holding one loopback socket per (local port, remote port, uid)."""
    path = tmp_path / "tcp"
    lines = [
        f"   {i}: 0100007F:{local:04X} 0100007F:{remote:04X} 01 00000000:00000000"
        f" 00:00000000 00000000 {uid:5d}        0 {1000 + i} 1 0000000000000000 20 4 30 10 -1\n"
        for i, (local, remote, uid) in enumerate(rows)
    ]
    path.write_text(_HEADER + "".join(lines))
    return path


def _call(scope):
    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(scope["type"])

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app_module._RefuseEpisodeClients(inner)(scope, receive, send))
    return reached, sent


@pytest.fixture()
def sockets(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module.os, "geteuid", lambda: 0)
    episode_uid = app_module._SANDBOX_UID_BASE + 5
    table = _table(
        tmp_path,
        [
            (40000, 8000, episode_uid),  # an episode's client socket
            (8000, 40000, 0),  # the server's end of that connection
            (40001, 8000, 0),  # a client running as the server's own user
        ],
    )
    monkeypatch.setattr(app_module, "_PROC_NET", (table,))


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_a_connection_from_an_episode_uid_is_refused(sockets, kind):
    scope = {"type": kind, "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8000)}
    reached, sent = _call(scope)
    assert reached == []
    if kind == "http":
        assert sent[0]["status"] == 403
    else:
        assert sent == [{"type": "websocket.close", "code": 1008}]
    # Controls: the server's own user, and a client whose socket is not in this namespace.
    for port in (40001, 50000):
        reached, sent = _call({**scope, "client": ("127.0.0.1", port)})
        assert reached == [kind] and sent == []


def test_without_the_uid_sandbox_nothing_is_refused(sockets, monkeypatch):
    monkeypatch.setattr(app_module.os, "geteuid", lambda: 1000)
    scope = {"type": "http", "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8000)}
    assert _call(scope) == (["http"], [])


def test_an_unreadable_socket_table_refuses_nothing(sockets, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_PROC_NET", (tmp_path / "missing",))
    scope = {"type": "http", "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8000)}
    assert _call(scope) == (["http"], [])


def test_the_app_carries_the_refusal():
    assert any(m.cls is app_module._RefuseEpisodeClients for m in app_module.app.user_middleware)

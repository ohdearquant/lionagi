# SPDX-License-Identifier: Apache-2.0
"""The server refuses connections that an episode's own code opens."""

import asyncio
import ipaddress
import sys

import pytest

from agent_workbench.server import app as app_module

_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)
_EPISODE_UID = app_module._SANDBOX_UID_BASE + 5
# /proc/net/tcp prints each 32-bit word of an address in host byte order; these are little-endian.
_LOOPBACK = "0100007F"
_LOOPBACK_2 = "0200007F"
_TEST_NET = "010200C0"  # 192.0.2.1
_MAPPED_LOOPBACK = "0000000000000000FFFF00000100007F"  # ::ffff:127.0.0.1
little_endian_only = pytest.mark.skipif(
    sys.byteorder != "little", reason="the literals above are little-endian /proc output"
)


def _row(i, local, remote, uid, inode, state="01"):
    return (
        f"   {i}: {local} {remote} {state} 00000000:00000000"
        f" 00:00000000 00000000 {uid:5d}        0 {inode} 1 0000000000000000 20 4 30 10 -1\n"
    )


def _table(tmp_path, rows, name="tcp"):
    """A /proc/net table: rows are (local, remote, uid, inode[, state]), endpoints as HEX:PORT."""
    path = tmp_path / name
    path.write_text(_HEADER + "".join(_row(i, *row) for i, row in enumerate(rows)))
    return path


def _lo(port, host=_LOOPBACK):
    return f"{host}:{port:04X}"


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


def _scope(port, kind="http", client_host="127.0.0.1", server=("127.0.0.1", 8000)):
    return {"type": kind, "client": (client_host, port), "server": server}


def _refused(scope):
    return _call(scope)[0] == []


@pytest.fixture()
def as_root(monkeypatch):
    monkeypatch.setattr(app_module.os, "geteuid", lambda: 0)


@pytest.fixture()
def sockets(tmp_path, monkeypatch, as_root):
    table = _table(
        tmp_path,
        [
            (_lo(40000), _lo(8000), _EPISODE_UID, 1000),  # an episode's client socket
            (_lo(8000), _lo(40000), 0, 1001),  # the server's end of that connection
            (_lo(40001), _lo(8000), 0, 1002),  # a client running as the server's own user
        ],
    )
    monkeypatch.setattr(app_module, "_PROC_NET", (table,))


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_a_connection_from_an_episode_uid_is_refused(sockets, kind):
    reached, sent = _call(_scope(40000, kind))
    assert reached == []
    if kind == "http":
        assert sent[0]["status"] == 403
    else:
        assert sent == [{"type": "websocket.close", "code": 1008}]
    # Controls: the server's own user, and a client whose socket is not in this namespace.
    for port in (40001, 50000):
        reached, sent = _call(_scope(port, kind))
        assert reached == [kind] and sent == []


def test_without_the_uid_sandbox_nothing_is_refused(sockets, monkeypatch):
    monkeypatch.setattr(app_module.os, "geteuid", lambda: 1000)
    assert _call(_scope(40000)) == (["http"], [])


def test_an_unreadable_socket_table_refuses_nothing(sockets, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "_PROC_NET", (tmp_path / "missing",))
    assert _call(_scope(40000)) == (["http"], [])


@little_endian_only
def test_a_closed_socket_on_the_same_connection_does_not_vouch_for_the_live_one(
    tmp_path, monkeypatch, as_root
):
    """A TIME_WAIT row shows uid 0 whoever owned it; it cannot stand in for the live socket's owner,
    whichever row comes first."""
    timewait = (_lo(40000), _lo(8000), 0, 0, "06")
    live = (_lo(40000), _lo(8000), _EPISODE_UID, 1000)
    for rows in ([timewait, live], [live, timewait]):
        monkeypatch.setattr(app_module, "_PROC_NET", (_table(tmp_path, rows),))
        assert _refused(_scope(40000))


@little_endian_only
def test_a_connection_only_a_closed_socket_matches_is_refused(tmp_path, monkeypatch, as_root):
    """Nothing vouches for whose it was: refused. A live socket of the server's own user is let in."""
    for state in ("06", "05"):
        rows = [(_lo(40000), _lo(8000), 0, 0, state)]
        monkeypatch.setattr(app_module, "_PROC_NET", (_table(tmp_path, rows),))
        assert _refused(_scope(40000))
    rows = [(_lo(40000), _lo(8000), 0, 0, "06"), (_lo(40000), _lo(8000), 0, 1000)]
    monkeypatch.setattr(app_module, "_PROC_NET", (_table(tmp_path, rows),))
    assert not _refused(_scope(40000))


@little_endian_only
def test_only_the_connections_own_addresses_match(tmp_path, monkeypatch, as_root):
    """An episode socket on the same ports but other addresses is not the connection: a trainer
    connecting from another host on that port is let in, and the episode's own is refused."""
    rows = [
        (_lo(40000), _lo(8000, _TEST_NET), _EPISODE_UID, 1000),
        (_lo(40000, _LOOPBACK_2), _lo(8000), _EPISODE_UID, 1001),
    ]
    monkeypatch.setattr(app_module, "_PROC_NET", (_table(tmp_path, rows),))
    assert not _refused(_scope(40000, client_host="172.17.0.1", server=("172.17.0.2", 8000)))
    assert not _refused(_scope(40000))
    assert _refused(_scope(40000, client_host="127.0.0.2"))


@little_endian_only
def test_an_ipv4_connection_on_an_ipv6_socket_is_matched(tmp_path, monkeypatch, as_root):
    rows = [(_lo(40000, _MAPPED_LOOPBACK), _lo(8000, _MAPPED_LOOPBACK), _EPISODE_UID, 1000)]
    monkeypatch.setattr(app_module, "_PROC_NET", (_table(tmp_path, rows, "tcp6"),))
    assert _refused(_scope(40000))
    assert _refused(
        _scope(40000, client_host="::ffff:127.0.0.1", server=("::ffff:127.0.0.1", 8000))
    )


@little_endian_only
def test_proc_net_endpoints_are_read_in_host_byte_order():
    assert app_module._endpoint("0100007F:1F40") == (ipaddress.ip_address("127.0.0.1"), 8000)
    assert app_module._endpoint(f"{_MAPPED_LOOPBACK}:0050") == (
        ipaddress.ip_address("127.0.0.1"),
        80,
    )
    assert app_module._endpoint("00000000000000000000000001000000:0050") == (
        ipaddress.ip_address("::1"),
        80,
    )


def test_a_client_address_that_is_no_address_is_refused(sockets):
    assert _refused(_scope(40001, client_host="testclient"))


def test_the_app_carries_the_refusal():
    assert any(m.cls is app_module._RefuseEpisodeClients for m in app_module.app.user_middleware)

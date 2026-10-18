# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Codes and decision capabilities a launch issues, and the reconnect channel.

A code is minted in process, single use, 120 seconds, bound to the origins the
launcher named, and consumed atomically. A redeemed code yields the admission
bearer and a decision capability: a 256-bit secret kept only as its hash, bound
to a random principal id, the daemon's start epoch, the page's origin and the
scope ``operator:decide``, and valid for eight hours. Codes and capabilities
belong to one daemon start; a restart invalidates both.

The reconnect channel is a unix socket that mints one code per connection to a
peer running as the daemon's own user, and nothing else.
"""

from __future__ import annotations

import os
import socket
import stat
import tempfile
import threading
from pathlib import Path

import pytest

import lionagi.studio.launch_access as launch_access
import lionagi.studio.reconnect as reconnect

_HOSTED = "https://lion-studio.khive.ai"


@pytest.fixture()
def short_dir():
    """A directory whose path fits a unix socket's ~100-byte limit."""
    with tempfile.TemporaryDirectory(prefix="studio-sock-", dir="/tmp") as path:
        yield Path(path)


@pytest.fixture()
def fresh(monkeypatch):
    """A process that has issued nothing yet."""
    launch_access.reset_launch_access()
    return launch_access


# Codes


def test_a_code_redeems_once_the_bearer_and_a_decision_capability(fresh):
    code = fresh.mint_pairing_code(origins=[_HOSTED])

    grant = fresh.redeem_pairing_grant(code, origin=_HOSTED)

    assert grant is not None
    assert grant.bearer == fresh.launch_bearer()
    principal = fresh.resolve_decision_capability(grant.capability, origin=_HOSTED)
    assert principal is not None
    assert principal.scope == "operator:decide"
    assert principal.epoch == fresh.daemon_epoch()
    assert principal.method == "pairing"
    assert len(grant.capability) >= 43
    assert fresh.redeem_pairing_grant(code, origin=_HOSTED) is None


def test_an_unknown_code_is_refused(fresh):
    fresh.mint_pairing_code(origins=[_HOSTED])

    assert fresh.redeem_pairing_grant("not-a-code", origin=_HOSTED) is None


def test_an_expired_code_is_refused(fresh, monkeypatch):
    code = fresh.mint_pairing_code(origins=[_HOSTED])
    now = fresh.time.monotonic()
    monkeypatch.setattr(fresh.time, "monotonic", lambda: now + fresh.PAIRING_CODE_TTL_S + 1)

    assert fresh.redeem_pairing_grant(code, origin=_HOSTED) is None


def test_a_wrong_origin_is_refused_and_does_not_consume_the_code(fresh):
    code = fresh.mint_pairing_code(origins=[_HOSTED])

    assert fresh.redeem_pairing_grant(code, origin="https://evil.example") is None
    assert fresh.redeem_pairing_code(code, origin="https://evil.example") is None

    assert fresh.redeem_pairing_grant(code, origin=_HOSTED) is not None


def test_two_concurrent_redemptions_of_one_code_yield_one_capability(fresh):
    code = fresh.mint_pairing_code(origins=[_HOSTED])
    results: list[object] = []
    barrier = threading.Barrier(2)

    def redeem() -> None:
        barrier.wait()
        results.append(fresh.redeem_pairing_grant(code, origin=_HOSTED))

    threads = [threading.Thread(target=redeem) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    granted = [result for result in results if result is not None]
    assert len(granted) == 1


def test_a_code_from_a_previous_daemon_start_is_refused(fresh):
    code = fresh.mint_pairing_code(origins=[_HOSTED])
    fresh.begin_serving()

    assert fresh.redeem_pairing_grant(code, origin=_HOSTED) is None


def test_a_launch_code_belongs_to_the_daemon_it_starts_in_the_same_process(fresh):
    code = fresh.mint_pairing_code(origins=[_HOSTED], for_launch=True)
    other = fresh.mint_pairing_code(origins=[_HOSTED])
    before = fresh.daemon_epoch()

    fresh.begin_serving()

    grant = fresh.redeem_pairing_grant(code, origin=_HOSTED)
    assert grant is not None
    # The code redeems into the serving epoch, with a bearer minted for it.
    assert fresh.daemon_epoch() != before
    assert grant.bearer == fresh.launch_bearer()
    principal = fresh.resolve_decision_capability(grant.capability, origin=_HOSTED)
    assert principal is not None and principal.epoch == fresh.daemon_epoch()
    # Only that code was carried; another code minted before serving is gone.
    assert fresh.redeem_pairing_grant(other, origin=_HOSTED) is None


# Capabilities


def test_the_secret_is_kept_only_as_its_hash(fresh):
    grant = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    assert grant is not None

    stored = [str(key) for key in fresh._capabilities]
    assert grant.capability not in fresh._capabilities
    assert all(grant.capability not in value for value in stored)


def test_an_unknown_capability_resolves_to_nothing(fresh):
    assert fresh.resolve_decision_capability("not-a-capability", origin=_HOSTED) is None


def test_a_capability_expires_eight_hours_after_redemption(fresh, monkeypatch):
    grant = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    assert grant is not None
    now = fresh.time.monotonic()

    monkeypatch.setattr(fresh.time, "monotonic", lambda: now + fresh.DECISION_CAPABILITY_TTL_S - 1)
    assert fresh.resolve_decision_capability(grant.capability, origin=_HOSTED) is not None

    monkeypatch.setattr(fresh.time, "monotonic", lambda: now + fresh.DECISION_CAPABILITY_TTL_S + 1)
    assert fresh.resolve_decision_capability(grant.capability, origin=_HOSTED) is None


def test_a_revoked_capability_resolves_to_nothing(fresh):
    grant = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    assert grant is not None

    assert fresh.revoke_decision_capability(grant.capability) is True

    assert fresh.resolve_decision_capability(grant.capability, origin=_HOSTED) is None
    assert fresh.revoke_decision_capability(grant.capability) is False


def test_a_restart_invalidates_every_capability(fresh):
    grant = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    assert grant is not None

    fresh.begin_serving()

    assert fresh.resolve_decision_capability(grant.capability, origin=_HOSTED) is None


def test_a_capability_sent_to_another_origin_is_refused(fresh):
    grant = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    assert grant is not None

    assert (
        fresh.resolve_decision_capability(grant.capability, origin="https://evil.example") is None
    )


def test_two_redemptions_hold_two_principals(fresh):
    first = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    second = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    assert first is not None and second is not None

    left = fresh.resolve_decision_capability(first.capability, origin=_HOSTED)
    right = fresh.resolve_decision_capability(second.capability, origin=_HOSTED)
    assert left is not None and right is not None
    assert left.id != right.id


# The reconnect channel


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_the_reconnect_socket_is_0600_in_a_0700_directory_and_gone_after_stop(short_dir):
    socket_path = short_dir / "state" / "reconnect.sock"
    channel = reconnect.ReconnectChannel(socket_path, lambda: "code-one")
    channel.start()
    try:
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
    finally:
        channel.stop()
    assert not socket_path.exists()


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_one_connection_gets_one_code_and_the_channels_key(short_dir):
    socket_path = short_dir / "reconnect.sock"
    codes = iter(["code-one", "code-two"])
    channel = reconnect.ReconnectChannel(socket_path, lambda: next(codes))
    channel.start()
    try:
        first = reconnect.read_reconnect_reply(socket_path)
        second = reconnect.read_reconnect_reply(socket_path)
    finally:
        channel.stop()

    assert first == ("code-one", channel.key)
    assert second == ("code-two", channel.key)
    assert len(channel.key) == 32
    assert reconnect.ReconnectChannel(socket_path, lambda: "x").key != channel.key


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_peer_with_another_user_id_gets_no_code(short_dir):
    minted: list[str] = []

    def mint() -> str:
        minted.append("code")
        return "code"

    other_uid = 0 if os.getuid() != 0 else 1
    channel = reconnect.ReconnectChannel(
        short_dir / "reconnect.sock", mint, _uid_reader=lambda _conn: other_uid
    )
    channel.start()
    try:
        assert reconnect.read_reconnect_reply(short_dir / "reconnect.sock", timeout=2) is None
    finally:
        channel.stop()
    assert minted == []


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_stale_socket_file_is_replaced_at_start(short_dir):
    socket_path = short_dir / "reconnect.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    assert socket_path.exists()

    channel = reconnect.ReconnectChannel(socket_path, lambda: "code-after-restart")
    channel.start()
    try:
        assert reconnect.read_reconnect_reply(socket_path) == ("code-after-restart", channel.key)
    finally:
        channel.stop()


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_many_concurrent_clients_each_get_their_own_code(short_dir):
    socket_path = short_dir / "reconnect.sock"
    lock = threading.Lock()
    minted: list[str] = []

    def mint() -> str:
        with lock:
            minted.append(f"code-{len(minted)}")
            return minted[-1]

    clients = 48
    ready = threading.Barrier(clients)
    received: list[str | None] = [None] * clients

    def read(index: int) -> None:
        ready.wait()
        reply = reconnect.read_reconnect_reply(socket_path)
        received[index] = reply[0] if reply is not None else None

    channel = reconnect.ReconnectChannel(socket_path, mint)
    channel.start()
    try:
        threads = [threading.Thread(target=read, args=(i,)) for i in range(clients)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        channel.stop()

    assert None not in received
    assert sorted(received) == sorted(minted)
    assert len(set(received)) == clients


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_refused_connection_is_retried_until_the_daemon_listens(short_dir):
    socket_path = short_dir / "reconnect.sock"
    # Bound but not yet listening, so connections are refused until listen().
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.settimeout(5)

    def listen_then_answer() -> None:
        server.listen(1)
        conn, _address = server.accept()
        with conn:
            conn.sendall(b"code-once-listening " + b"ab" * 32 + b"\n")

    timer = threading.Timer(0.3, listen_then_answer)
    timer.start()
    try:
        assert reconnect.read_reconnect_reply(socket_path) == (
            "code-once-listening",
            bytes.fromhex("ab" * 32),
        )
    finally:
        timer.join(timeout=10)
        server.close()


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_socket_file_nothing_listens_on_answers_none_promptly(short_dir):
    import time

    socket_path = short_dir / "reconnect.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    started = time.monotonic()
    assert reconnect.read_reconnect_reply(socket_path, timeout=5) is None
    assert time.monotonic() - started < 3


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_directory_another_user_owns_is_refused(short_dir, monkeypatch):
    directory = short_dir / "theirs"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(reconnect, "current_uid", lambda: os.getuid() + 1)
    channel = reconnect.ReconnectChannel(directory / "reconnect.sock", lambda: "code")

    with pytest.raises(OSError, match="another user"):
        channel.start()
    assert list(directory.iterdir()) == []


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_the_directory_is_created_private_rather_than_narrowed_after(short_dir, monkeypatch):
    # With chmod unavailable, only the mode the directory was created with keeps
    # it private: there is no moment in which it is open to other users.
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    previous = os.umask(0o022)
    try:
        reconnect._checked_directory(short_dir / "new")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((short_dir / "new").stat().st_mode) == 0o700


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_socket_whose_liveness_cannot_be_read_is_kept(short_dir, monkeypatch):
    socket_path = short_dir / "reconnect.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()

    class _Unreadable:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def settimeout(self, _timeout) -> None:
            pass

        def connect(self, _address) -> None:
            raise PermissionError(13, "Permission denied")

        def close(self) -> None:
            pass

    monkeypatch.setattr(reconnect.socket, "socket", _Unreadable)

    with pytest.raises(OSError, match="cannot tell whether a daemon answers"):
        reconnect._replace_stale_socket(socket_path)
    assert socket_path.exists()


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_socket_file_another_user_owns_is_kept(short_dir, monkeypatch):
    socket_path = short_dir / "reconnect.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    monkeypatch.setattr(reconnect, "current_uid", lambda: os.getuid() + 1)

    with pytest.raises(OSError, match="another user"):
        reconnect._replace_stale_socket(socket_path)
    assert socket_path.exists()


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_the_socket_file_is_the_only_thing_in_the_directory(short_dir):
    directory = short_dir / "private"
    channel = reconnect.ReconnectChannel(directory / "reconnect.sock", lambda: "code")
    channel.start()
    try:
        assert [entry.name for entry in directory.iterdir()] == ["reconnect.sock"]
    finally:
        channel.stop()


def test_a_platform_without_unix_sockets_has_no_reconnect(monkeypatch):
    monkeypatch.setattr(reconnect, "unix_sockets_supported", lambda: False)

    assert reconnect.read_reconnect_reply(Path("/nonexistent/reconnect.sock")) is None


# What the daemon starts and clears


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_the_daemon_starts_the_channel_at_its_state_directory(short_dir, monkeypatch, fresh):
    import lionagi.studio.app as app_mod

    socket_path = short_dir / "reconnect.sock"
    monkeypatch.setattr(app_mod, "default_socket_path", lambda *args: short_dir / "reconnect.sock")
    channel = app_mod._start_reconnect_channel()
    assert channel is not None
    try:
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(short_dir.stat().st_mode) == 0o700
        code, key = reconnect.read_reconnect_reply(socket_path)
        assert key == channel.key
        assert launch_access.redeem_pairing_grant(code, origin=_HOSTED) is not None
    finally:
        channel.stop()
    assert not socket_path.exists()


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_the_serving_daemon_signs_with_the_key_its_reconnect_socket_hands_out(
    short_dir, monkeypatch, fresh
):
    from fastapi.testclient import TestClient

    import lionagi.cli._code_identity as code_identity
    import lionagi.studio.app as app_mod
    import lionagi.studio.services.launches as launches_mod
    import lionagi.studio.services.lifecycle as lifecycle_mod
    import lionagi.studio.services.operator as operator_mod
    from lionagi.studio.scheduler.engine import scheduler

    async def _noop(*args, **kwargs) -> None:
        return None

    # Only the lifespan's wiring of the channel is under test; its other startup
    # work is stubbed so nothing reaches a real store, the scheduler or the mirror.
    monkeypatch.setattr(code_identity, "snapshot_git_position", lambda: None)
    monkeypatch.setattr(scheduler, "start", _noop)
    monkeypatch.setattr(scheduler, "stop", _noop)
    monkeypatch.setattr(lifecycle_mod, "run_startup_reconciliation", _noop)
    monkeypatch.setattr(operator_mod, "operator_startup", _noop)
    monkeypatch.setattr(operator_mod, "operator_shutdown", _noop)
    monkeypatch.setattr(launches_mod, "shutdown_launches", _noop)
    monkeypatch.setattr(app_mod, "_start_claude_mirror", lambda: (None, None))
    monkeypatch.setattr(app_mod, "_stop_claude_mirror", _noop)
    monkeypatch.setattr(app_mod, "_startup_warmup", _noop)
    monkeypatch.setattr(app_mod, "_finalize_warmup", _noop)
    socket_path = short_dir / "reconnect.sock"
    monkeypatch.setattr(app_mod, "default_socket_path", lambda *args: socket_path)

    application = app_mod.create_app()
    challenge = "cd" * 16
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        reply = reconnect.read_reconnect_reply(socket_path)
        assert reply is not None
        body = client.get("/health", params={"challenge": challenge}).json()

    assert body == {"status": "ok", "proof": reconnect.port_proof(reply[1], challenge, 8765)}
    assert application.state.reconnect_key is None
    assert not socket_path.exists()


def test_a_platform_without_unix_sockets_starts_no_channel(monkeypatch):
    import lionagi.studio.app as app_mod

    monkeypatch.setattr(app_mod, "unix_sockets_supported", lambda: False)

    assert app_mod._start_reconnect_channel() is None


@pytest.mark.skipif(not reconnect.unix_sockets_supported(), reason="no unix sockets")
def test_a_channel_that_cannot_be_created_leaves_the_daemon_serving(short_dir, monkeypatch):
    import lionagi.studio.app as app_mod

    blocked = short_dir / "not-a-socket"
    blocked.write_text("x")
    monkeypatch.setattr(app_mod, "default_socket_path", lambda *args: blocked)

    assert app_mod._start_reconnect_channel() is None


def test_serving_clears_what_an_earlier_daemon_issued_in_this_process(fresh):
    first = fresh.redeem_pairing_grant(fresh.mint_pairing_code(origins=[_HOSTED]), origin=_HOSTED)
    code = fresh.mint_pairing_code(origins=[_HOSTED])
    assert first is not None

    fresh.begin_serving()

    assert fresh.resolve_decision_capability(first.capability, origin=_HOSTED) is None
    assert fresh.redeem_pairing_grant(code, origin=_HOSTED) is None

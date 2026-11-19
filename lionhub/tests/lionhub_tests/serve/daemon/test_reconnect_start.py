# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A reconnect channel that cannot start its accept thread leaves no socket behind."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from lionhub.serve.daemon import reconnect


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
def test_a_thread_that_cannot_start_closes_the_listening_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name == "studio-reconnect":
            raise RuntimeError("can't start new thread")
        real_start(self)

    # A short path under /tmp: a unix socket path has a length limit the test tree can exceed.
    with tempfile.TemporaryDirectory(prefix="studio-r", dir="/tmp") as directory:
        path = Path(directory) / "r.sock"
        channel = reconnect.ReconnectChannel(
            path, lambda: "code", _uid_reader=lambda _c: os.getuid()
        )

        with monkeypatch.context() as patched:
            patched.setattr(threading.Thread, "start", start)
            with pytest.raises(RuntimeError, match="can't start new thread"):
                channel.start()

        assert not path.exists()
        # The next channel on the same path starts: nothing answers there any more.
        following = reconnect.ReconnectChannel(
            path, lambda: "code", _uid_reader=lambda _c: os.getuid()
        )
        following.start()
        try:
            assert path.is_socket()
        finally:
            following.stop()
        assert not path.exists()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
@pytest.mark.parametrize("step", ["chmod", "listen"])
def test_a_failure_after_bind_removes_the_socket_file(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    with tempfile.TemporaryDirectory(prefix="studio-r", dir="/tmp") as directory:
        path = Path(directory) / "r.sock"
        channel = reconnect.ReconnectChannel(
            path, lambda: "code", _uid_reader=lambda _c: os.getuid()
        )

        def refuse(*_args: object, **_kwargs: object) -> None:
            assert path.is_socket()  # bind ran: the file this test expects removed exists
            raise OSError(f"{step} refused")

        with monkeypatch.context() as patched:
            if step == "chmod":
                patched.setattr(reconnect.os, "chmod", refuse)
            else:
                patched.setattr(socket.socket, "listen", refuse)
            with pytest.raises(OSError, match=f"{step} refused"):
                channel.start()

        assert not path.exists()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="no unix sockets")
def test_a_failed_bind_leaves_a_file_it_did_not_create(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory(prefix="studio-r", dir="/tmp") as directory:
        path = Path(directory) / "r.sock"
        channel = reconnect.ReconnectChannel(
            path, lambda: "code", _uid_reader=lambda _c: os.getuid()
        )
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            other.bind(str(path))

            def bind_refused(self: socket.socket, _address: object) -> None:
                raise OSError("address in use")

            with monkeypatch.context() as patched:
                patched.setattr(reconnect, "_replace_stale_socket", lambda _p: None)
                patched.setattr(socket.socket, "bind", bind_refused)
                with pytest.raises(OSError, match="address in use"):
                    channel.start()

            assert path.is_socket()
        finally:
            other.close()

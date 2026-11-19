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

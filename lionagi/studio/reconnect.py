# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The daemon's same-user reconnect channel for a later ``li studio``.

An HTTP route cannot issue a pairing code: any process that can reach the
listener can reach it too. A daemon that is already running therefore mints a
code for a new page over a unix socket in its state directory instead. Every
connection is answered only when the peer runs as the daemon's own OS user,
read from the socket itself rather than claimed by the peer, and the answer is
one code and this start's port-proof key, nothing else.

The socket belongs to whichever daemon uses the state directory, which need not
be the daemon on the port ``li studio`` was given. The key settles that: the
daemon signs a fresh challenge together with the TCP port the request arrived
on (``port_proof``, served by ``/health``), so only the daemon holding the key,
reached on that port, can answer it.

The socket file is created with mode ``0600`` inside a ``0700`` directory before
the daemon serves, and removed when the process exits. A socket file left behind
by a daemon that no longer runs is replaced at start.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import socket
import stat
import struct
import sys
import threading
import time
from pathlib import Path

__all__ = (
    "ReconnectChannel",
    "current_uid",
    "default_socket_path",
    "peer_uid",
    "port_proof",
    "read_reconnect_reply",
    "state_dir",
    "unix_sockets_supported",
)

# A private directory inside the state directory, so the socket's 0700 parent
# never widens or narrows the permissions of a directory the store shares.
_SOCKET_DIR = "studio"
_SOCKET_NAME = "studio-reconnect.sock"
# A code is at most 32 characters and the key is 64 hex digits; the reply is the
# two, a space between them and a newline.
_MAX_REPLY_BYTES = 256
_ACCEPT_TIMEOUT_S = 0.2
# Connections waiting to be answered. Each answer is one code and no read, so the
# queue drains quickly; a client that finds it full retries for a short while.
_BACKLOG = 128
_CONNECT_RETRY_S = 1.0


def unix_sockets_supported() -> bool:
    """Whether this platform can carry the reconnect channel."""
    return hasattr(socket, "AF_UNIX") and sys.platform != "win32"


def state_dir() -> Path:
    """The daemon's state directory: the one its StateDB file lives in.

    A store with no file of its own (a server-backed URL) leaves nothing to
    place a private socket beside, so the LionAGI home directory is used then.
    """
    from lionagi._paths import LIONAGI_HOME
    from lionagi.state import db as state_db_mod

    path = state_db_mod.state_db_file()
    return path.parent if path is not None else LIONAGI_HOME


def default_socket_path(directory: str | Path | None = None) -> Path:
    """The reconnect socket in a private directory beside the daemon's store.

    *directory* names the state directory directly; without it the daemon's
    own state directory is used. The socket lives one level down, in a
    directory this channel creates with mode 0700.
    """
    target = state_dir() if directory is None else Path(directory)
    return target / _SOCKET_DIR / _SOCKET_NAME


def port_proof(key: bytes, challenge: str, port: int) -> str:
    """The daemon's answer to *challenge* received on TCP *port*, under this start's *key*."""
    return hmac.new(key, f"{challenge}:{port}".encode("ascii"), hashlib.sha256).hexdigest()


def current_uid() -> int:
    """The daemon's own user id; a platform without one refuses every peer."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else -1


def peer_uid(sock: socket.socket) -> int | None:
    """The user id of the process at the other end of *sock*, from the kernel.

    ``SO_PEERCRED`` on Linux, ``LOCAL_PEERCRED`` on macOS; both are read with
    ``getsockopt`` from the standard library. None means this platform's kernel
    will not say, and the caller refuses the peer.
    """
    if hasattr(socket, "SO_PEERCRED"):
        # struct ucred { pid_t pid; uid_t uid; gid_t gid; } -- four-byte fields.
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        if len(raw) < struct.calcsize("3i"):
            return None
        _pid, uid, _gid = struct.unpack("3i", raw[: struct.calcsize("3i")])
        return uid
    if hasattr(socket, "LOCAL_PEERCRED"):
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, struct.calcsize("4i"))
        fields = struct.calcsize("4i")
        if len(raw) < fields:
            return None
        _version, uid, _gid, _pid = struct.unpack("4i", raw[:fields])
        return uid
    return None


def _checked_directory(path: Path) -> None:
    """Create *path* unless it exists, and keep it private to this user.

    A directory another user owns is refused rather than adopted: its owner
    could replace the socket inside it.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"reconnect directory {path} is not a directory")
    if info.st_uid != current_uid():
        raise OSError(f"reconnect directory {path} belongs to another user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


def _replace_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise OSError(f"reconnect path {path} exists and is not a socket")
    if info.st_uid != current_uid():
        raise OSError(f"reconnect socket {path} belongs to another user")
    # A socket file has no liveness of its own. A refused connection means no
    # process listens on it, so it is a file a dead daemon left behind; a server
    # that answers is a daemon. Any other failure (a full queue, a timeout, a
    # permission error) says nothing about liveness, so the file is kept.
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"cannot tell whether a daemon answers on {path}: {exc}") from exc
    else:
        raise OSError(f"another daemon already answers on {path}")
    finally:
        probe.close()


class ReconnectChannel:
    """A listening unix socket that mints one pairing code per same-user connection.

    ``key`` is drawn fresh for each channel and goes out only on this socket;
    the daemon proves it holds it through ``port_proof``.
    """

    def __init__(self, path: str | Path, mint_code, _uid_reader=peer_uid) -> None:
        self.path = Path(path)
        self.key = secrets.token_bytes(32)
        self._mint_code = mint_code
        self._uid_reader = _uid_reader
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        """Create the private directory and socket, then accept in a thread."""
        _checked_directory(self.path.parent)
        _replace_stale_socket(self.path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(_BACKLOG)
            server.settimeout(_ACCEPT_TIMEOUT_S)
        except BaseException:
            server.close()
            raise
        self._server = server
        thread = threading.Thread(target=self._serve, name="studio-reconnect", daemon=True)
        self._thread = thread
        thread.start()

    def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        while not self._stopping.is_set():
            try:
                conn, _address = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        try:
            uid = self._uid_reader(conn)
        except OSError:
            return
        if uid is None or uid != current_uid():
            return
        # Nothing is read: the reply is one code and the key, so a peer that
        # sends nothing still gets them, and no other content is accepted.
        try:
            code = self._mint_code()
            conn.sendall(f"{code} {self.key.hex()}\n".encode("ascii"))
        except OSError:
            return

    def stop(self) -> None:
        """Stop accepting and remove the socket file."""
        self._stopping.set()
        server, self._server = self._server, None
        if server is not None:
            server.close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self.path.exists() and self.path.is_socket():
            self.path.unlink(missing_ok=True)


def _connect(path: str, timeout: float) -> socket.socket | None:
    """A connected client, or None when nothing listens at *path*.

    A listener whose queue is full can refuse the connection or report that it
    would block, and a socket file with no listener refuses it too, so either
    answer is retried for a short while before it is taken as no daemon.
    """
    deadline = time.monotonic() + min(timeout, _CONNECT_RETRY_S)
    while True:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(path)
        except (ConnectionRefusedError, BlockingIOError):
            client.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)
        except OSError:
            client.close()
            return None
        else:
            return client


def read_reconnect_reply(
    path: str | Path, *, timeout: float = 5.0, _uid_reader=None
) -> tuple[str, bytes] | None:
    """Ask the daemon at *path* for one pairing code and its port-proof key.

    None when nothing answers, or the answer is not a code and a key. PermissionError when the
    process serving *path* is not this user's, or the kernel will not say whose it is: the path
    vouches for nothing, since another user can place a socket there or a link to one.
    """
    if not unix_sockets_supported():
        return None
    client = _connect(str(path), timeout)
    if client is None:
        return None
    try:
        # Read from the connected socket, so nothing done to the path after connect() counts.
        try:
            uid = (peer_uid if _uid_reader is None else _uid_reader)(client)
        except OSError:
            uid = None
        if uid is None or uid != current_uid():
            owner = "a user the kernel does not report" if uid is None else f"user {uid}"
            raise PermissionError(
                f"the process serving {path} runs as {owner}, not as this user ({current_uid()})"
            )
        chunks: list[bytes] = []
        total = 0
        while total < _MAX_REPLY_BYTES:
            try:
                chunk = client.recv(_MAX_REPLY_BYTES - total)
            except OSError:
                return None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\n" in chunk:
                break
    finally:
        client.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        code, key_hex = raw.decode("ascii").split()
        key = bytes.fromhex(key_hex)
    except (UnicodeDecodeError, ValueError):
        return None
    if not code or len(key) != 32:
        return None
    return code, key

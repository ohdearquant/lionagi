# SPDX-License-Identifier: BSD-3-Clause

"""
FastAPI application for the Agent Workbench Environment.

This module creates an HTTP server that exposes the AgentWorkbenchEnvironment
over HTTP and WebSocket endpoints, compatible with EnvClient.

Endpoints:
    - POST /reset: Reset the environment
    - POST /step: Execute an action
    - GET /state: Get current environment state
    - GET /schema: Get action/observation schemas
    - WS /ws: WebSocket endpoint for persistent sessions

Usage:
    # Development (with auto-reload):
    uvicorn server.app:app --reload --host 0.0.0.0 --port 8000

    # Production (the image's command):
    uvicorn server.app:app --host 0.0.0.0 --port 8000 --no-proxy-headers

    # Or run directly:
    python -m server.app

Episode code runs inside this server's container and can reach its port, so a connection opened by
an episode's sandbox user is refused (see _RefuseEpisodeClients). The check reads the connecting
socket's owner, so the server must see the real peer address: run uvicorn without proxy headers,
which would let a loopback client name any address it likes.
"""

import ipaddress
import os
import sys
from pathlib import Path

try:
    from openenv.core.env_server.http_server import create_app
except Exception as e:  # pragma: no cover
    raise ImportError(
        "openenv is required for the web interface. Install dependencies with '\n    uv sync\n'"
    ) from e

try:
    from ..models import WorkbenchAction, WorkbenchObservation
    from .agent_workbench_environment import (
        _SANDBOX_UID_BASE,
        _SANDBOX_UID_SLOTS,
        AgentWorkbenchEnvironment,
    )
except ImportError:
    from models import WorkbenchAction, WorkbenchObservation
    from server.agent_workbench_environment import (
        _SANDBOX_UID_BASE,
        _SANDBOX_UID_SLOTS,
        AgentWorkbenchEnvironment,
    )

_PROC_NET = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))


def _address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """*ip*, with an IPv4-mapped IPv6 address read as the IPv4 address it carries."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _endpoint(field: str) -> tuple:
    """An address:port field of /proc/net/tcp{,6}, which prints each 32-bit word of the address as
    a hex number in host byte order."""
    host, port = field.rsplit(":", 1)
    words = (int(host[i : i + 8], 16) for i in range(0, len(host), 8))
    packed = b"".join(word.to_bytes(4, sys.byteorder) for word in words)
    return _address(ipaddress.ip_address(packed)), int(port, 16)


def _client_sockets(client: tuple, server: tuple) -> list[tuple[int, int]]:
    """(uid, inode) of every local socket from *client* to *server*, both (address, port).

    Only a client inside this network namespace has such a socket; a remote client's end is not
    here. A socket no process holds any longer (one in TIME_WAIT, or closed by its owner) shows
    inode 0 and, in TIME_WAIT, uid 0 whoever owned it.
    """
    found = []
    for table in _PROC_NET:
        try:
            rows = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10:
                continue
            try:
                local, remote = _endpoint(fields[1]), _endpoint(fields[2])
                uid, inode = int(fields[7]), int(fields[9])
            except ValueError:
                continue
            if local == client and remote == server:
                found.append((uid, inode))
    return found


class _RefuseEpisodeClients:
    """Refuses HTTP requests and WebSocket sessions opened by an episode's own code.

    Episode code could otherwise open fresh episodes of its own task and read each answer's grade
    back, which turns submit and check into an answer oracle. Only a server running the uid
    sandbox (as root) has sandbox users to refuse.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self._from_episode(scope):
            if scope["type"] == "http":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    }
                )
                await send(
                    {"type": "http.response.body", "body": b"episode code cannot use this server\n"}
                )
            else:
                await receive()
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _from_episode(scope) -> bool:
        """True when the connection's own socket is here and a sandbox user holds it, or when it
        is here and no process holds it any longer, so nothing vouches for whose it was."""
        if os.geteuid() != 0:
            return False
        client, server = scope.get("client"), scope.get("server")
        if not client or not server or server[1] is None:
            return False
        try:
            client = (_address(ipaddress.ip_address(client[0])), client[1])
            server = (_address(ipaddress.ip_address(server[0])), server[1])
        except ValueError:
            return True
        sockets = _client_sockets(client, server)
        if any(
            _SANDBOX_UID_BASE <= uid < _SANDBOX_UID_BASE + _SANDBOX_UID_SLOTS for uid, _ in sockets
        ):
            return True
        return bool(sockets) and all(inode == 0 for _, inode in sockets)


# Create the app with web interface and README integration
app = create_app(
    AgentWorkbenchEnvironment,
    WorkbenchAction,
    WorkbenchObservation,
    env_name="agent_workbench",
    max_concurrent_envs=8,
)
app.add_middleware(_RefuseEpisodeClients)


def main(host: str = "0.0.0.0", port: int = 8000):  # noqa: S104 - the server runs in a container
    """
    Entry point for direct execution via uv run or python -m.

    This function enables running the server without Docker:
        uv run --project . server
        uv run --project . server --port 8001
        python -m agent_workbench.server.app

    Args:
        host: Host address to bind to (default: "0.0.0.0")
        port: Port number to listen on (default: 8000)

    Proxy headers stay off so the episode-client check sees the real peer (see the module docstring).
    """
    import uvicorn

    uvicorn.run(app, host=host, port=port, proxy_headers=False)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    main(port=args.port)

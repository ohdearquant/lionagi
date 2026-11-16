# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Implement `li remote`: run an agent leg on a leg-host Studio daemon on another machine.

A remote is named in ``~/.lionagi/remotes.yaml`` by its URL and the name of the
environment variable that holds its token. The token is never written to a file by this
command, never printed, and never sent anywhere but that remote. See
docs/guides/remote-legs.md.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lionagi._auto import CliDeclaration, auto_register
from lionhub.orchestration.console import _refused_inside_an_agent
from lionhub.serve.cli.studio_client import STUDIO_TOKEN_ENV

__all__ = (
    "RemoteError",
    "add_remote_subparser",
    "load_remote",
    "run_remote",
)

_REMOTE_NAME = re.compile(r"[a-z0-9_-]+")
_TOKEN_ENV_NAME = re.compile(r"LIONAGI_REMOTE_[A-Z0-9_]+")
_HEAD = re.compile(r"[0-9a-f]{40}")
_INVOCATION_ID = re.compile(r"[0-9a-f]{8,32}")
_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out"})
_TAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
_TIMEOUT_SECONDS = 30.0


class RemoteError(Exception):
    """A remote leg could not be configured, reached, or run; the message says which."""


@dataclass(frozen=True)
class Remote:
    """A configured remote: where it is and the variable its token is read from."""

    name: str
    scheme: str
    host: str
    port: int
    token_env: str
    token: str


def _remotes_file() -> Path:
    return Path.home() / ".lionagi" / "remotes.yaml"


def load_remote(name: str, path: Path | None = None) -> Remote:
    """Read remote *name* and its token, refusing a configuration that could leak the token."""
    path = path or _remotes_file()
    if not _REMOTE_NAME.fullmatch(name):
        raise RemoteError(f"remote name {name!r} must match [a-z0-9_-]+")
    if not path.is_file():
        raise RemoteError(f"no remotes are configured: {path} does not exist")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entry = raw.get(name) if isinstance(raw, dict) else None
    if not isinstance(entry, dict) or set(entry) != {"url", "token_env"}:
        raise RemoteError(f"{path} has no remote {name!r} with exactly 'url' and 'token_env'")
    url = urllib.parse.urlsplit(str(entry["url"]))
    try:
        port = url.port
    except ValueError as exc:
        raise RemoteError(f"remote {name!r} has an unreadable URL port") from exc
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise RemoteError(
            f"remote {name!r} URL must be http(s)://host[:port] with no path, credentials or query"
        )
    token_env = str(entry["token_env"])
    if not _TOKEN_ENV_NAME.fullmatch(token_env):
        raise RemoteError(
            f"remote {name!r} token_env {token_env!r} must be named LIONAGI_REMOTE_<NAME>, "
            "the prefix lionagi keeps out of every agent's environment"
        )
    token = os.environ.get(token_env) or ""
    if not token:
        raise RemoteError(f"{token_env} is not set: the token for remote {name!r} is read from it")
    if token == os.environ.get(STUDIO_TOKEN_ENV):
        raise RemoteError(
            f"{token_env} holds the same token as {STUDIO_TOKEN_ENV}: a remote gets its own "
            "token, never this machine's Studio credential"
        )
    default_port = 443 if url.scheme == "https" else 80
    return Remote(name, url.scheme, url.hostname, port or default_port, token_env, token)


def _is_loopback_or_tailnet(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in net for net in _TAILNET_NETS)


def _call(remote: Remote, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """One request to *remote*, sent straight to it: no proxy, no redirect followed.

    Over plain http the address the socket actually reached must be loopback or a tailnet
    address before the token is written to it, and that address is the Host sent, the one
    name a leg host bound to it admits.
    """
    conn: http.client.HTTPConnection
    if remote.scheme == "https":
        conn = http.client.HTTPSConnection(
            remote.host, remote.port, timeout=_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(remote.host, remote.port, timeout=_TIMEOUT_SECONDS)
    try:
        try:
            conn.connect()
        except OSError as exc:
            raise RemoteError(f"cannot reach remote {remote.name!r}: {exc}") from exc
        headers = {"Authorization": f"Bearer {remote.token}", "Accept": "application/json"}
        if remote.scheme == "http":
            peer = conn.sock.getpeername()[0]
            if not _is_loopback_or_tailnet(peer):
                raise RemoteError(
                    f"remote {remote.name!r} answered from {peer}, which is neither loopback nor a "
                    "tailnet address; over plain http the token is sent only inside those"
                )
            authority = f"[{peer}]" if ":" in peer else peer
            headers["Host"] = f"{authority}:{remote.port}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        status, raw = response.status, response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise RemoteError(f"request to remote {remote.name!r} failed: {exc}") from exc
    finally:
        conn.close()
    if 300 <= status < 400:
        raise RemoteError(f"remote {remote.name!r} answered a redirect ({status}); not followed")
    if status == 401:
        raise RemoteError(f"remote {remote.name!r} refused the token in {remote.token_env}")
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if status >= 400:
        detail = data.get("detail") if isinstance(data, dict) else raw[:500].decode(errors="replace")
        raise RemoteError(f"remote {remote.name!r} answered HTTP {status}: {detail}")
    return data


def _invocation_id(value: str) -> str:
    if not _INVOCATION_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an invocation id")
    return value


def _head(value: str) -> str:
    if not _HEAD.fullmatch(value):
        raise argparse.ArgumentTypeError("--head must be a full 40-character lowercase hex commit")
    return value


def _launch(remote: Remote, args: argparse.Namespace) -> str:
    caps = _call(remote, "GET", "/api/launches/capabilities")
    if not (isinstance(caps, dict) and caps.get("leg_host") and caps.get("pinned_head")):
        raise RemoteError(f"remote {remote.name!r} is not a leg host; it would not pin the head")
    started = _call(
        remote,
        "POST",
        "/api/launches/",
        {
            "action_kind": "agent",
            "action_agent": args.agent,
            "action_prompt": args.prompt,
            "action_repo": args.repo,
            "action_head": args.head,
        },
    )
    inv_id = started.get("invocation_id") if isinstance(started, dict) else None
    if not isinstance(inv_id, str) or not _INVOCATION_ID.fullmatch(inv_id):
        raise RemoteError(f"remote {remote.name!r} returned no invocation id")
    if started.get("action_head") != args.head:
        _call(remote, "POST", f"/api/invocations/{inv_id}/cancel")
        raise RemoteError(
            f"remote {remote.name!r} did not confirm head {args.head}; launch {inv_id} cancelled"
        )
    return inv_id


def _wait(remote: Remote, inv_id: str, poll: float) -> str:
    while True:
        row = _call(remote, "GET", f"/api/invocations/{inv_id}")
        status = row.get("status") if isinstance(row, dict) else None
        if status in _TERMINAL:
            return status
        time.sleep(poll)


def _print_result(remote: Remote, inv_id: str) -> int:
    stored = _call(remote, "GET", f"/api/launches/{inv_id}/result")
    if stored.get("result") is not None:
        sys.stdout.write(stored["result"])
        if not stored["result"].endswith("\n"):
            sys.stdout.write("\n")
    if stored.get("truncated"):
        print(f"note: the result of {inv_id} was cut to its last part", file=sys.stderr)
    status = stored.get("status")
    if status != "completed":
        print(f"remote leg {inv_id} ended {status}", file=sys.stderr)
        return 1
    return 0


def _cmd_run(remote: Remote, args: argparse.Namespace) -> int:
    if _refused_inside_an_agent():
        return 1
    inv_id = _launch(remote, args)
    print(f"remote leg {inv_id} started on {remote.name} at {args.head}", file=sys.stderr)
    try:
        _wait(remote, inv_id, args.poll)
    except KeyboardInterrupt:
        _call(remote, "POST", f"/api/invocations/{inv_id}/cancel")
        print(f"cancelled remote leg {inv_id}", file=sys.stderr)
        return 130
    return _print_result(remote, inv_id)


def _cmd_status(remote: Remote, args: argparse.Namespace) -> int:
    row = _call(remote, "GET", f"/api/invocations/{args.invocation_id}")
    print(json.dumps({k: row.get(k) for k in ("id", "status", "status_reason_summary")}))
    return 0


def _cmd_output(remote: Remote, args: argparse.Namespace) -> int:
    return _print_result(remote, args.invocation_id)


def _cmd_cancel(remote: Remote, args: argparse.Namespace) -> int:
    _call(remote, "POST", f"/api/invocations/{args.invocation_id}/cancel")
    print(f"cancelling remote leg {args.invocation_id}", file=sys.stderr)
    return 0


_ACTIONS = {"run": _cmd_run, "status": _cmd_status, "output": _cmd_output, "cancel": _cmd_cancel}


def add_remote_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register `li remote`. Returns the `remote` parser."""
    remote = subparsers.add_parser(
        "remote",
        help="Run an agent leg on a leg-host Studio daemon on another machine.",
        description=(
            "Run agent legs on a remote leg host, named in ~/.lionagi/remotes.yaml. "
            "See docs/guides/remote-legs.md."
        ),
    )
    sub = remote.add_subparsers(dest="remote_action")
    sub.required = True
    run = sub.add_parser("run", help="Start a leg at a pinned head, wait, print its result.")
    run.add_argument("remote", help="Remote name from ~/.lionagi/remotes.yaml.")
    run.add_argument("-a", "--agent", required=True, help="Profile name on the leg host.")
    run.add_argument("--repo", required=True, help="Repository name the leg host serves.")
    run.add_argument("--head", required=True, type=_head, help="Full 40-hex commit to run at.")
    run.add_argument("--poll", type=float, default=5.0, help="Seconds between status reads.")
    run.add_argument("prompt", help="The leg's prompt.")
    for action, text in (
        ("status", "Show a remote leg's status."),
        ("output", "Print a remote leg's result."),
        ("cancel", "Cancel a remote leg."),
    ):
        p = sub.add_parser(action, help=text)
        p.add_argument("remote", help="Remote name from ~/.lionagi/remotes.yaml.")
        p.add_argument("invocation_id", type=_invocation_id, help="The remote invocation id.")
    return remote


@auto_register(area="remote", cli=CliDeclaration(seed="remote", parser_factory=add_remote_subparser))
def run_remote(args: argparse.Namespace) -> int:
    try:
        remote = load_remote(args.remote)
        return _ACTIONS[args.remote_action](remote, args)
    except RemoteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

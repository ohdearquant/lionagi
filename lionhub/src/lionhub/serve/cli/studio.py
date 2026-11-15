# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Implement the `li studio` command."""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from lionagi._auto import CliDeclaration, auto_register
from lionagi._log_channels import log_error, warn
from lionagi._paths import ensure_lionagi_dir
from lionagi.studio._host import admitted_host_names, is_unspecified_host, normalize_host
from lionhub.orchestration.console import _refused_inside_an_agent
from lionhub.orchestration.schedule.parsing import _url_authority

_STUDIO_IMAGE = "ghcr.io/ohdearquant/lion-studio:latest"
_HOSTED_URL = "https://lion-studio.khive.ai"


def _mount_allowed_roots() -> list[Path]:
    """Host path prefixes allowed for Docker bind-mounts (home + XDG_CONFIG_HOME)."""
    roots: list[Path] = [Path.home().resolve()]
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        xdg_path = Path(xdg_config).resolve()
        if xdg_path not in roots:
            roots.append(xdg_path)
    return roots


def _is_mount_allowed(resolved_path: Path, allowed_roots: list[Path]) -> bool:
    for root in allowed_roots:
        try:
            if resolved_path.is_relative_to(root):
                return True
        except (TypeError, ValueError):
            continue
    return False


def _add_studio_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    # The `start` subparser must SUPPRESS its defaults, or its unset defaults
    # overwrite values parsed at the parent `studio` parser level.
    def _default(value):
        return argparse.SUPPRESS if suppress_defaults else value

    parser.add_argument(
        "--port",
        type=int,
        default=_default(None),
        help="Backend API port (default: LIONAGI_STUDIO_PORT env or 8765)",
    )
    parser.add_argument(
        "--host",
        default=_default("127.0.0.1"),
        help=(
            "Host to bind (default: 127.0.0.1). --docker also publishes the "
            "container port on this host, so anything else there exposes the "
            "daemon to that interface. This is an IP address, not an address "
            "with a port: the port is --port, and a value carrying a port is "
            "refused."
        ),
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=_default(3000),
        dest="frontend_port",
        help="Frontend port (default: 3000)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        default=_default(False),
        dest="no_open",
        help="Don't open the hosted UI in a browser (--web only)",
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        default=_default(False),
        dest="no_docker",
        help=argparse.SUPPRESS,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--web",
        action="store_true",
        default=_default(False),
        help="Start the backend only; frontend is the hosted UI (default)",
    )
    mode.add_argument(
        "--docker",
        action="store_true",
        default=_default(False),
        help=(
            "Run the bundled frontend + backend via Docker. The port is "
            "published on --host (loopback by default)"
        ),
    )
    mode.add_argument(
        "--no-frontend",
        action="store_true",
        default=_default(False),
        dest="no_frontend",
        help="Only start the backend API server",
    )
    mode.add_argument(
        "--dev",
        action="store_true",
        default=_default(False),
        help="Run the in-repo frontend in dev mode (hot-reload, no build step)",
    )


def add_studio_subparser(subparsers: argparse._SubParsersAction) -> None:
    studio_parser = subparsers.add_parser("studio", help="Lion Studio server")
    _add_studio_flags(studio_parser)

    studio_sub = studio_parser.add_subparsers(dest="studio_action")
    studio_sub.required = False

    start_parser = studio_sub.add_parser("start", help="Start Lion Studio")
    _add_studio_flags(start_parser, suppress_defaults=True)


def _validate_mode_flags(args: argparse.Namespace) -> None:
    # Mutual exclusion can be split across the parent and `start` subparser,
    # which argparse's per-parser groups can't see — validate the combined namespace.
    selected = [
        flag
        for flag, attr in (
            ("--web", "web"),
            ("--docker", "docker"),
            ("--no-frontend", "no_frontend"),
            ("--dev", "dev"),
        )
        if getattr(args, attr, False)
    ]
    if len(selected) > 1:
        print(
            f"li studio: mode flags are mutually exclusive: {' '.join(selected)}",
            file=sys.stderr,
        )
        raise SystemExit(2)


@auto_register(
    area="studio", cli=CliDeclaration(seed="studio", parser_factory=add_studio_subparser)
)
def run_studio(args: argparse.Namespace) -> int:
    if not getattr(args, "studio_action", None):
        args.studio_action = "start"
    _validate_mode_flags(args)
    # A daemon started here would start its runs outside this process's tree.
    if _refused_inside_an_agent():
        return 1
    return _studio_start(args)


def _find_repo_root() -> Path | None:
    import lionagi

    repo_root = Path(lionagi.__file__).resolve().parent.parent
    if (repo_root / "apps" / "studio").is_dir():
        return repo_root
    return None


def _find_frontend_dir() -> Path | None:
    repo_root = _find_repo_root()
    if repo_root is None:
        return None
    candidate = repo_root / "apps" / "studio" / "frontend"
    if (candidate / "package.json").exists():
        return candidate
    return None


def _ensure_apps_importable() -> bool:
    repo_root = _find_repo_root()
    if repo_root is None:
        return False
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return True


def _has_docker() -> bool:
    return shutil.which("docker") is not None


def _studio_start(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print(
            "uvicorn is required. Install with: pip install 'lionagi[studio]'",
            file=sys.stderr,
        )
        return 1

    port_from_env = os.environ.get("LIONAGI_STUDIO_PORT")
    port: int = (
        getattr(args, "port", None) or (int(port_from_env) if port_from_env else None) or 8765
    )
    host: str = getattr(args, "host", "127.0.0.1")
    no_frontend: bool = getattr(args, "no_frontend", False)
    use_docker: bool = getattr(args, "docker", False)
    dev_mode: bool = getattr(args, "dev", False)
    no_open: bool = getattr(args, "no_open", False)
    frontend_port: int = getattr(args, "frontend_port", 3000)
    no_docker: bool = getattr(args, "no_docker", False)

    if no_docker:
        warn(
            "--no-docker is deprecated and ignored; Docker is now opt-in with "
            "--docker. Use bare `li studio` or `li studio --web` for the hosted UI."
        )

    if no_frontend:
        return _start_backend_only(host, port)

    if dev_mode:
        frontend_dir = _find_frontend_dir()
        return _start_local(host, port, frontend_port, frontend_dir, dev_mode=True)

    if use_docker:
        if not _has_docker():
            print("Error: Docker not found. Install it from https://docker.com/", file=sys.stderr)
            return 1
        return _start_docker(host, port, frontend_port)

    # Default (bare `li studio` / `--web`): hosted frontend, local daemon only.
    return _start_hosted(host, port, no_open)


class _PairingRefusedError(Exception):
    """A daemon answers on the port but did not issue a pairing code."""


def _daemon_health(
    port: int, challenge: str | None = None, *, host: str = "127.0.0.1", deadline_s: float = 3.0
) -> dict[str, Any] | None:
    """What a lionagi daemon on ``host`` and ``port`` answers at ``/health``, or None.

    The socket timeout bounds each read, not the exchange: a peer that sends a
    byte at a time holds ``urlopen`` and ``read`` for as long as it keeps going.
    So the exchange runs on a worker and an answer later than *deadline_s* is
    None, the same as no answer. The abandoned worker is a daemon thread and
    ends when that peer stops or closes.
    """
    import http.client
    import urllib.parse

    from lionagi.ln._ssrf import open_url

    url = f"http://{_url_authority(host)}:{port}/health"
    if challenge is not None:
        url += "?" + urllib.parse.urlencode({"challenge": challenge})
    outcome: list[object] = []

    def _probe() -> None:
        try:
            with open_url(url, timeout=2) as resp:
                outcome.append(json.loads(resp.read()))
        except (OSError, ValueError, http.client.HTTPException):
            outcome.append(None)
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=_probe, name="studio-health-probe", daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive() or not outcome:
        return None
    health = outcome[0]
    if isinstance(health, Exception):
        raise health
    if isinstance(health, dict) and health.get("status") == "ok":
        return health
    return None


def _code_from_running_daemon(port: int) -> str | None:
    """A pairing code from the lionagi daemon already listening on ``port``, or None.

    None means nothing that answers like a lionagi daemon is there, so this launch
    starts one. The code comes over the same-user reconnect socket in this state
    directory, never over HTTP, and is returned only when the daemon on ``port``
    proves it is the one that socket belongs to. Anything else raises, since
    starting a second daemon on the same port would only fail to bind.
    """
    import hmac
    import secrets

    from lionagi.studio.reconnect import (
        default_socket_path,
        port_proof,
        read_reconnect_reply,
        unix_sockets_supported,
    )

    if _daemon_health(port) is None:
        return None
    if not unix_sockets_supported():
        raise _PairingRefusedError(
            "this platform has no reconnect channel; restart the daemon to pair a new page"
        )
    socket_path = default_socket_path()
    try:
        reply = read_reconnect_reply(socket_path)
    except PermissionError as exc:
        raise _PairingRefusedError(
            f"{exc}; stop that process, or start this daemon with its own state directory"
        ) from exc
    if reply is None:
        raise _PairingRefusedError(
            "it did not answer on its reconnect socket; restart the daemon to pair a new page"
        )
    code, key = reply
    # The socket answers for whichever daemon shares this state directory. Only that
    # daemon can sign a fresh challenge with the port the request reached, so a second
    # daemon on this port, or a listener relaying to it from another port, fails here,
    # and the code just minted is never shown; it expires unused.
    challenge = secrets.token_hex(16)
    health = _daemon_health(port, challenge)
    proof = health.get("proof") if health is not None else None
    # compare_digest takes ASCII strings only; any other proof is simply not the one expected.
    if (
        not isinstance(proof, str)
        or not proof.isascii()
        or not hmac.compare_digest(proof, port_proof(key, challenge, port))
    ):
        raise _PairingRefusedError(
            f"the reconnect socket at {socket_path} belongs to another daemon, not the one on "
            f"port {port}, or port {port} is not the port that daemon binds (a forwarded or "
            f"mapped port cannot pair); stop that daemon, or start this one with its own state "
            f"directory"
        )
    return code


def _hosted_launch_url(port: int, code: str) -> str:
    """The hosted page's URL for this daemon, carrying a one-time pairing code.

    The code rides the fragment, which a browser never sends to a server; the page
    reads it once, removes it from the address bar and exchanges it for a bearer.
    """
    query = "" if port == 8765 else f"?api=http://127.0.0.1:{port}"
    return f"{_HOSTED_URL}/{query}#pair={code}"


def _dev_pairing_line(frontend_url: str) -> str:
    """The link that pairs the dev page at *frontend_url* with the daemon this process starts.

    The code is bound to that page's exact origin, the address Vite reported, so
    only a page opened at that address can spend it. The link is that origin as a
    browser sends it, so the page it opens is the page the code is bound to.
    """
    from lionagi.studio.launch_access import (
        PAIRING_CODE_TTL_S,
        _canonical_origin,
        mint_pairing_code,
    )

    origin = _canonical_origin(frontend_url)
    code = mint_pairing_code(origins=[origin], for_launch=True)
    minutes = int(PAIRING_CODE_TTL_S // 60)
    return (
        f"to decide Operator proposals, open this link within {minutes} minutes; "
        f"it works once: {origin}/#pair={code}"
    )


def _dev_pairing_origin(frontend_url: str, bind_host: str) -> str | None:
    """The origin a dev page pairs from, or None when no page this daemon serves can.

    Vite forwards the page's own Host to the daemon, and the daemon refuses every
    request whose Host it does not admit, the pairing request included. The page
    Vite reported is used when its host is admitted. A wildcard bind admits only
    loopback names and Vite serves the same port on loopback, so the link moves to
    ``localhost``, the name Vite prints as that bind's Local address. Any other
    refused host gets no link.
    """
    from urllib.parse import urlsplit

    from lionagi.studio.launch_access import _canonical_origin

    origin = _canonical_origin(frontend_url)
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if normalize_host(parts.hostname) in admitted_host_names(bind_host):
        return origin
    if is_unspecified_host(bind_host):
        return f"{parts.scheme}://localhost" + (f":{port}" if port else "")
    return None


def _ignore_sigpipe() -> None:
    """Let a write to a peer that already closed raise BrokenPipeError in the daemon.

    `li` sets SIGPIPE to its default action so `li ... | head` stops quietly; a server running in
    that process would then be killed by the first client that disconnects mid-write.
    """
    import signal

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def _bind_refusal(host: str, port: int) -> OSError | None:
    """Why the daemon could not listen on ``host`` and ``port`` now, or None when it can.

    Opens a listener with ``asyncio``'s ``create_server`` for that host and port and
    closes it before returning. The daemon's uvicorn start binds with
    ``loop.create_server(host=..., port=...)`` plus its own protocol factory, ``ssl``
    (None here) and ``backlog``; neither call passes reuse or family options, so
    whether the bind succeeds depends on the same host and port. This is a probe at
    launch, not a reservation: another program can still take the port before the
    daemon binds.
    """

    async def _listen_once() -> None:
        server = await asyncio.get_running_loop().create_server(
            asyncio.Protocol, host=host, port=port
        )
        server.close()
        await server.wait_closed()

    try:
        asyncio.run(_listen_once())
    except OSError as exc:
        return exc
    return None


def _start_hosted(host: str, port: int, no_open: bool) -> int:
    import errno
    import socket

    from lionagi.studio.launch_access import PAIRING_CODE_TTL_S, mint_pairing_code

    daemon_url = f"http://127.0.0.1:{port}"
    try:
        code = _code_from_running_daemon(port)
    except _PairingRefusedError as exc:
        log_error(
            f"A Studio daemon is already running at {daemon_url}, but it would not pair "
            f"a page ({exc})."
        )
        return 1
    already_running = code is not None
    if code is None:
        # Nothing answered as a daemon, so this process serves the page. A port it
        # cannot bind would fail only after the link is out, leaving a page that
        # holds a code no daemon will redeem.
        refusal = _bind_refusal(host, port)
        if refusal is not None:
            # The advice has to fit the cause. A host that does not resolve raises
            # ``gaierror``, whose errno is an EAI_* code that can equal a real errno, so
            # it is told apart by type before any errno comparison.
            if isinstance(refusal, socket.gaierror) or refusal.errno == errno.EADDRNOTAVAIL:
                reason = f"{host} is not an address this machine can listen on ({refusal})"
                advice = "Check `--host`."
            elif refusal.errno == errno.EADDRINUSE:
                reason = (
                    f"port {port} is in use, and nothing answered {daemon_url}/health as a "
                    f"lionagi daemon in time"
                )
                advice = "Stop that program, or choose another port with `li studio --port <port>`."
            elif refusal.errno == errno.EACCES:
                reason = f"port {port} needs privileges ({refusal})"
                advice = "Choose another port with `li studio --port <port>`."
            else:
                reason = f"this process cannot listen on {host}:{port} ({refusal})"
                advice = "Choose another port with `li studio --port <port>`."
            log_error(f"Lion Studio did not start: {reason}. {advice}")
            return 1
        code = mint_pairing_code(for_launch=True)
    url = _hosted_launch_url(port, code)
    print(f"Lion Studio: {_HOSTED_URL}")
    print(f"  connects to your local daemon at {daemon_url}")
    opened = False
    if not no_open and sys.stdin.isatty() and sys.stdout.isatty():
        import webbrowser

        with contextlib.suppress(Exception):
            opened = bool(webbrowser.open(url))
    if not opened:
        minutes = int(PAIRING_CODE_TTL_S // 60)
        print(f"  open this link within {minutes} minutes; it works once: {url}")
    print()
    if already_running:
        print("A daemon was already running on this port; this page is paired with it.")
        return 0
    return _start_backend_only(host, port)


def _start_backend_only(host: str, port: int) -> int:
    import uvicorn

    # The backend app lives inside the installed package
    # (lionagi.studio.app); it does not import anything from the repo's
    # apps/ tree, so a pip install must not be gated on finding one. The
    # repo root is still added to sys.path when present, purely as a
    # convenience for repo checkouts.
    _ensure_apps_importable()

    print(f"Lion Studio API: http://{host}:{port}")
    # Set before uvicorn.run: the app is loaded via import string and reads this from env.
    os.environ["LIONAGI_STUDIO_HOST"] = host
    _ignore_sigpipe()
    # One process: a pairing code lives in the process that minted it, whether a
    # hosted launch minted it before calling here or a later `li studio` asks
    # the reconnect socket for one, and every worker starts its own reconnect
    # channel and mints into its own memory. A worker count from WEB_CONCURRENCY
    # would serve the pairing route from a process that never issued the code,
    # so a --no-frontend start, which mints nothing itself, runs one worker too.
    uvicorn.run("lionagi.studio.app:app", host=host, port=port, workers=1)
    return 0


_STUDIO_CONTAINER_PORT = 8765


def _published_host(host: str) -> str:
    """The address ``--docker`` publishes the container's port on, named by *host*.

    Brackets are stripped first, so a bracketed blank is as blank as a bare one:
    a blank host is the loopback default rather than a request to publish
    everywhere, since an empty host half means every interface to docker, the
    opposite of what an unset flag asks for. What is left has to be an IP
    address literal. Docker does not resolve a name in the host half of a
    mapping, and it cannot publish on an address that carries a port or an IPv6
    zone, so each of those is refused here with a message instead of failing
    inside docker.

    An IPv4-mapped IPv6 address (``::ffff:192.168.1.5``) is returned as the
    IPv4 address it maps, since that is what a client reaches it as and what the
    daemon's Host grammar accepts -- `normalize_host` does that unwrapping, so the
    spelling in brackets and the compressed spelling reach the same address.

    Raises:
        ValueError: *host* is not an address the port can be published on.
    """
    normalized = normalize_host(host)
    if not normalized:
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        raise ValueError(
            f"--host {host!r} is not an IP address; --host takes an address, not an "
            "address with a port (the port is --port). --docker publishes the "
            "container port on an address such as 127.0.0.1, 0.0.0.0, 192.168.1.5 or ::1"
        ) from None
    if getattr(address, "scope_id", None) is not None:
        raise ValueError(
            f"--host {host!r} carries an IPv6 zone, which names a local interface: "
            "--docker cannot publish a port on it"
        ) from None
    # An IPv4-mapped IPv6 address is the IPv4 address in another spelling; a
    # browser sends the IPv4 form, which is also the form the daemon admits.
    return str(address)


def _bracketed(address: str) -> str:
    """*address* as a URL authority or a port mapping needs it: an IPv6 literal
    in brackets, since its own colons would otherwise read as a separator."""
    return f"[{address}]" if ":" in address else address


def _printed_host(published: str) -> str:
    """The host to print in the Studio URL for the address *published*.

    A wildcard (``0.0.0.0`` or ``::``) names no address a browser can open, and
    the daemon refuses it as a Host, so the URL says ``localhost``, which it
    admits. Any other address is printed as published, an IPv6 literal in
    brackets.
    """
    if ipaddress.ip_address(published).is_unspecified:
        return "localhost"
    return _bracketed(published)


def _docker_publish_spec(host: str, api_port: int) -> str:
    """The ``-p`` value publishing the container's port on *host* at *api_port*.

    A host half is always written: a bare ``PORT:PORT`` mapping publishes on
    every host interface, which puts an unauthenticated daemon on the local
    network.

    Raises:
        ValueError: *host* is not an address the port can be published on.
    """
    return f"{_bracketed(_published_host(host))}:{api_port}:{_STUDIO_CONTAINER_PORT}"


def _start_docker(host: str, api_port: int, frontend_port: int) -> int:
    try:
        publish_spec = _docker_publish_spec(host, api_port)
        published = _published_host(host)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    lionagi_home = Path.home() / ".lionagi"
    ensure_lionagi_dir(lionagi_home)

    print(f"Pulling {_STUDIO_IMAGE}...")
    try:
        pull = subprocess.run(  # noqa: S603
            ["docker", "pull", _STUDIO_IMAGE],  # noqa: S607
            capture_output=True,
        )
    except KeyboardInterrupt:
        # Ctrl-C during the pull stops the launch before anything runs, exactly
        # as it does during the run; the interrupt is not a failed launch.
        print("\nStopping Lion Studio...")
        return 0
    if pull.returncode != 0:
        stderr = pull.stderr.decode(errors="replace").strip()
        print(f"Warning: docker pull failed: {stderr}", file=sys.stderr)
        print("Trying to use cached image...", file=sys.stderr)

    print()
    print(f"Lion Studio: http://{_printed_host(published)}:{api_port}")
    print("Press Ctrl+C to stop")
    print()

    env_args: list[str] = []
    if os.environ.get("LIONAGI_STUDIO_AUTH_TOKEN"):
        # The name alone: docker reads the value from this process's environment,
        # so the token reaches the container without entering the argument list.
        env_args += ["-e", "LIONAGI_STUDIO_AUTH_TOKEN"]
    # The published address is passed to the daemon: it admits a request whose
    # Host is the address it is reached by. A wildcard is passed too, so the
    # container's startup warning names the same address the publish does. It
    # adds no admitted Host: an unspecified address binds every interface and is
    # never a name a page is opened at, so the daemon refuses it as a Host.
    env_args += ["-e", f"LIONAGI_STUDIO_HOST={published}"]

    claude_plugins = Path.home() / ".claude" / "plugins"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-p",
        # The image's uvicorn binds 0.0.0.0 inside the container, which is what
        # makes the published port reachable at all; the host half of this
        # mapping, not that bind, decides who can reach it.
        publish_spec,
        *env_args,
        "-v",
        f"{lionagi_home}:/root/.lionagi",
    ]
    if claude_plugins.is_dir():
        docker_cmd.extend(["-v", f"{claude_plugins}:/root/.claude/plugins:ro"])

    allowed_roots = _mount_allowed_roots()
    symlink_mounts: set[Path] = set()
    for subdir_name in ("agents", "skills", "playbooks", "teams"):
        subdir = lionagi_home / subdir_name
        if not subdir.is_dir():
            continue
        for entry in subdir.iterdir():
            if not entry.is_symlink():
                continue
            try:
                target = entry.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            mount_src = target if target.is_dir() else target.parent
            if not _is_mount_allowed(mount_src, allowed_roots):
                warn(
                    f"symlink target {mount_src} is outside the allowed mount "
                    "roots and will not be mounted."
                )
                continue
            symlink_mounts.add(mount_src)

    for mount_src in sorted(symlink_mounts):
        docker_cmd.extend(["-v", f"{mount_src}:{mount_src}:ro"])

    if symlink_mounts:
        print(f"Mounted {len(symlink_mounts)} symlink target(s) for Library access:")
        for m in sorted(symlink_mounts):
            print(f"  {m} (ro)")
        print()

    docker_cmd.extend(["--name", "lion-studio", _STUDIO_IMAGE])

    try:
        run = subprocess.run(docker_cmd, check=False)  # noqa: S603
    except KeyboardInterrupt:
        print("\nStopping Lion Studio...")
        subprocess.run(  # noqa: S603
            ["docker", "stop", "lion-studio"],  # noqa: S607
            capture_output=True,
        )
        return 0
    # docker's own exit status, so a refused publish (a non-zero run) is not
    # reported as a successful launch.
    return run.returncode


def _start_local(
    host: str,
    port: int,
    frontend_port: int,
    frontend_dir: Path | None,
    dev_mode: bool,
) -> int:
    import uvicorn

    if frontend_dir is None:
        print("Error: --dev requires the lionagi repo. Clone it first.", file=sys.stderr)
        return 1

    if not shutil.which("node"):
        print(
            "Error: Node.js required for local frontend. Install from https://nodejs.org/",
            file=sys.stderr,
        )
        return 1

    if not _ensure_apps_importable():
        print(
            "Error: studio backend not found. Run from the lionagi repo root or install "
            "the full studio package.",
            file=sys.stderr,
        )
        return 1

    frontend_proc: subprocess.Popen | None = None

    if dev_mode:
        # Dev mode: hot-reload Vite dev server + uvicorn side-by-side.
        # Vite proxies /api → uvicorn (configured in vite.config.mts).
        # The page pairs with the daemon this process serves; no other daemon
        # knows the code minted here.
        if _daemon_health(port, host=host) is not None:
            log_error(
                f"A Studio daemon is already running at http://{_url_authority(host)}:{port}; "
                f"--dev serves its own. Stop that daemon or choose another --port."
            )
            return 1
        api_url = _studio_api_url(host, port)
        proxied_to = os.environ.get("STUDIO_API_URL", api_url)
        launched = _launch_vite_dev(
            frontend_dir,
            frontend_port,
            host=host,
            api_host=host,
            api_port=port,
        )
        if launched:
            frontend_proc, frontend_url = launched
            if frontend_url:
                print(f"Lion Studio UI (dev):  {frontend_url}")
                pairing_origin = _dev_pairing_origin(frontend_url, host)
                if not _proxy_reaches_daemon(proxied_to, host, port):
                    warn(
                        f"STUDIO_API_URL sends the dev page's API calls to {proxied_to}, "
                        f"which cannot redeem a code this launch mints; no pairing link."
                    )
                elif pairing_origin is None:
                    warn(
                        f"The daemon refuses the Host a page at {frontend_url} sends, so "
                        f"that page cannot pair; no pairing link. Bind --host to the "
                        f"address you open the page at."
                    )
                else:
                    print(f"  {_dev_pairing_line(pairing_origin)}")
            # else: _launch_vite_dev already printed a specific, actionable
            # warning (early exit / stream end / timeout) with captured output.
        print(f"Lion Studio API:       http://{host}:{port}")
    else:
        # Production mode: build dist/ once, then uvicorn serves both UI and API
        # from the same origin — no second process needed.
        if _ensure_frontend_built(frontend_dir):
            # Must be set before uvicorn.run: app.py reads it at import time to
            # activate the SPA fallback.
            dist_path = frontend_dir / "dist"
            os.environ["LIONAGI_STUDIO_FRONTEND_DIST"] = str(dist_path)
            print(f"Lion Studio: http://{host}:{port}")
        else:
            print("Warning: frontend build failed; starting API-only mode.", file=sys.stderr)
            print(f"Lion Studio API: http://{host}:{port}")

    print("Press Ctrl+C to stop")

    # Set before uvicorn.run: the app is loaded via import string and reads this from env.
    os.environ["LIONAGI_STUDIO_HOST"] = host
    _ignore_sigpipe()
    try:
        # One process, as in _start_backend_only: the dev page's code lives here.
        uvicorn.run("lionagi.studio.app:app", host=host, port=port, workers=1)
    except KeyboardInterrupt:
        print("\nStopping Lion Studio...")
    finally:
        if frontend_proc:
            frontend_proc.terminate()
            try:
                frontend_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                frontend_proc.kill()
                frontend_proc.wait()
    return 0


def _is_build_stale(frontend_dir: Path) -> bool:
    """True when dist/index.html is absent or older than source files."""
    build_marker = frontend_dir / "dist" / "index.html"
    if not build_marker.exists():
        return True

    try:
        marker_mtime = build_marker.stat().st_mtime
    except OSError:
        return True

    source_roots = [
        frontend_dir / "src",
    ]
    # Only include config files that actually exist in the frontend dir.
    _candidate_source_files = [
        frontend_dir / "index.html",
        frontend_dir / "vite.config.mts",
        frontend_dir / "package.json",
        frontend_dir / "package-lock.json",
        frontend_dir / "tsconfig.json",
        frontend_dir / "tailwind.config.ts",
        frontend_dir / "postcss.config.cjs",
        frontend_dir / "postcss.config.js",
    ]
    source_files = [f for f in _candidate_source_files if f.exists()]

    for f in source_files:
        try:
            if f.stat().st_mtime > marker_mtime:
                return True
        except OSError:
            return True

    for root in source_roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime > marker_mtime:
                    return True
            except OSError:
                return True

    return False


def _needs_npm_install(frontend_dir: Path) -> bool:
    """True when node_modules/ is missing, Vite is not installed, or package.json is newer than the install marker."""
    node_modules = frontend_dir / "node_modules"
    if not node_modules.exists():
        return True
    if not (node_modules / ".bin" / "vite").exists():
        return True

    # Use node_modules/.package-lock.json as the install marker (npm touches it
    # on every install).  Fall back to node_modules/ dir mtime if absent.
    install_marker = node_modules / ".package-lock.json"
    if not install_marker.exists():
        install_marker = node_modules

    try:
        installed_mtime = install_marker.stat().st_mtime
    except OSError:
        return True

    for dep_file in (frontend_dir / "package.json", frontend_dir / "package-lock.json"):
        try:
            if dep_file.exists() and dep_file.stat().st_mtime > installed_mtime:
                return True
        except OSError:
            return True

    return False


def _ensure_frontend_built(frontend_dir: Path) -> bool:
    """Install deps if needed, then build with Vite. Returns True on success."""
    if _needs_npm_install(frontend_dir):
        print("Installing frontend dependencies...")
        try:
            subprocess.run(  # noqa: S603
                ["npm", "install"],  # noqa: S607
                cwd=str(frontend_dir),
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Warning: npm install failed: {e}", file=sys.stderr)
            return False

    if _is_build_stale(frontend_dir):
        print("Building frontend...")
        try:
            subprocess.run(  # noqa: S603
                ["npx", "vite", "build"],  # noqa: S607
                cwd=str(frontend_dir),
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"Warning: frontend build failed: {e}", file=sys.stderr)
            return False

    return True


def _vite_dev_argv(frontend_port: int, host: str) -> list[str]:
    """Build the `npx vite` dev-server argv from a single host/port source of truth."""
    return ["npx", "vite", "--port", str(frontend_port), "--host", host]


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_VITE_LOCAL_URL_RE = re.compile(r"Local:\s+(https?://[^\s/]+)")
_VITE_NETWORK_URL_RE = re.compile(r"Network:\s+(https?://[^\s/]+)")
_VITE_SETTLE_SECONDS = 0.15

# Startup-output lines kept for diagnostics when Vite fails to report a
# bound address — bounded so a runaway process can't grow this unbounded.
_STARTUP_TAIL_LINES = 40


def _host_is_loopback(host: str) -> bool:
    """True when `host` is a loopback address — the address whose `Local:`
    line is the one a caller can actually reach. Anything else — a LAN IP,
    a real hostname, or the 0.0.0.0/:: wildcard — binds extra interfaces
    that only show up on Vite's `Network:` line; `Local:` stays a loopback
    URL even when told to listen everywhere, so using it there would print
    an address a LAN client cannot reach.

    Numeric hosts (127.0.0.1, 127.0.0.2, ::1, ...) are classified via
    `ipaddress` rather than an exact-string allowlist, since the entire
    127.0.0.0/8 block is loopback, not just 127.0.0.1. `localhost` and
    `*.localhost` get explicit handling since they aren't IP literals.
    The 0.0.0.0/:: wildcards are "unspecified", not loopback, and stay
    LAN-seeking. Hostnames compare case-insensitively, as DNS does.
    """
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_wants_lan_url(host: str) -> bool:
    return not _host_is_loopback(host)


def _parse_vite_local_url(line: str) -> str | None:
    """Extract the bound URL from a Vite startup line like `➜  Local:   http://127.0.0.1:5174/`.

    Vite colors this line with ANSI escapes whenever it thinks it has a color
    terminal (npm/npx can force this even with a piped stdout), so the escapes
    must be stripped before matching or the parse silently misses.
    """
    plain = _ANSI_ESCAPE_RE.sub("", line)
    match = _VITE_LOCAL_URL_RE.search(plain)
    return match.group(1) if match else None


def _parse_vite_network_url(line: str) -> str | None:
    """Extract the LAN address from a Vite startup line like `➜  Network:   http://192.168.1.5:5174/`."""
    plain = _ANSI_ESCAPE_RE.sub("", line)
    match = _VITE_NETWORK_URL_RE.search(plain)
    return match.group(1) if match else None


# Sentinel pushed onto the hits queue by the reader thread when Vite's
# stdout hits EOF (process exited, or otherwise closed the stream) — lets
# the caller learn about that promptly instead of blocking for the full
# readiness timeout.
_STREAM_EOF = object()


def _await_vite_ready_url(
    proc: subprocess.Popen,
    *,
    host: str,
    timeout: float = 10.0,
) -> str | None:
    """Read Vite's startup banner for the address it actually bound.

    Loopback hosts read the `Local:` line; non-loopback hosts (LAN IP, or the
    `0.0.0.0`/`::` wildcard) read `Network:` instead, since that's what's
    reachable from outside this machine. Returns None on failure, printing
    one of three distinct warnings (early exit, stream EOF while still
    running, or a genuine timeout) each with the last `_STARTUP_TAIL_LINES`
    of captured output. Callers must not construct a guessed URL on the
    failure path. See docs/internals/studio.md for the background drain
    thread's lifecycle.
    """
    parse = _parse_vite_network_url if _host_wants_lan_url(host) else _parse_vite_local_url
    hits: queue.Queue[object] = queue.Queue()
    done = threading.Event()
    tail_lock = threading.Lock()
    tail: collections.deque[str] = collections.deque(maxlen=_STARTUP_TAIL_LINES)

    def _pump() -> None:
        if proc.stdout is None:
            hits.put(_STREAM_EOF)
            return
        try:
            for line in proc.stdout:
                with tail_lock:
                    tail.append(line.rstrip("\n"))
                if done.is_set():
                    continue
                found = parse(line)
                if found:
                    hits.put(found)
        finally:
            hits.put(_STREAM_EOF)

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()

    def _diagnostics() -> str:
        with tail_lock:
            lines = list(tail)
        if not lines:
            return " No output was captured."
        return "\nCaptured Vite output (most recent lines):\n  " + "\n  ".join(lines)

    try:
        first = hits.get(timeout=timeout)
    except queue.Empty:
        done.set()
        warn(
            f"Timed out after {timeout:.0f}s waiting for Vite to report a bound "
            f"address.{_diagnostics()}"
        )
        return None

    if first is _STREAM_EOF:
        done.set()
        exit_code = proc.poll()
        if exit_code is None:
            # Stdout closed before the process was reaped — give it a brief
            # grace window rather than reporting "still running" spuriously.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.0)
            exit_code = proc.poll()
        if exit_code is not None:
            warn(
                f"Vite exited early (exit code {exit_code}) before reporting a "
                f"bound address.{_diagnostics()}"
            )
        else:
            warn(
                "Vite's output stream ended before reporting a bound address, "
                f"and the process is still running.{_diagnostics()}"
            )
        return None

    matches = [first]
    deadline = time.monotonic() + _VITE_SETTLE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            item = hits.get(timeout=remaining)
        except queue.Empty:
            break
        if item is _STREAM_EOF:
            continue
        matches.append(item)
    done.set()

    if len(matches) > 1:
        kind = "Network" if _host_wants_lan_url(host) else "Local"
        warn(f"Vite reported {len(matches)} {kind}: addresses; using the first ({matches[0]}).")
    return matches[0]


def _studio_api_url(api_host: str, api_port: int) -> str:
    """The address the dev server's /api proxy reaches this launch's daemon at."""
    url_host = f"[{api_host}]" if ":" in api_host and not api_host.startswith("[") else api_host
    return f"http://{url_host}:{api_port}"


def _proxy_reaches_daemon(target: str, host: str, port: int) -> bool:
    """Whether a dev proxy aimed at *target* reaches the daemon bound to *host* and *port*.

    Both sides are read as the address a client connects to, so an unspecified
    bind address matches the loopback address of its family and an IPv6 address
    matches however it is spelled. A name is compared as written: ``localhost``
    can resolve to either family, so it is not taken to reach a daemon bound to
    one of them. A target with a path, query or fragment sends the page's API
    calls under that path, where this daemon serves nothing.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(target)
        target_port = parts.port
    except ValueError:
        return False
    if (
        parts.scheme != "http"
        or not parts.hostname
        or "@" in parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return False
    return (_url_authority(parts.hostname), target_port or 80) == (_url_authority(host), port)


def _launch_vite_dev(
    frontend_dir: Path,
    frontend_port: int,
    *,
    host: str = "127.0.0.1",
    api_host: str | None = None,
    api_port: int | None = None,
) -> tuple[subprocess.Popen, str | None] | None:
    """Spawn the Vite dev server and resolve the URL it actually bound to.

    Returns the process paired with the real bound URL — or paired with None
    when Vite's bound address could not be determined, so the caller never
    prints/opens a guessed address nothing may be listening on. Returns None
    only when the process itself failed to spawn.
    """
    env = {**os.environ, "PORT": str(frontend_port)}
    if api_host is not None and api_port is not None:
        # An operator-supplied target is an intentional escape hatch and wins
        # over the host/port selected by this CLI invocation.
        env.setdefault("STUDIO_API_URL", _studio_api_url(api_host, api_port))
    try:
        proc = subprocess.Popen(  # noqa: S603
            _vite_dev_argv(frontend_port, host),  # noqa: S607
            cwd=str(frontend_dir),
            env=env,
            stdout=subprocess.PIPE,
            # Merged into stdout (not discarded) so a crash's diagnostics
            # are part of the same captured stream _await_vite_ready_url
            # already reads and can show on failure.
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("Warning: npx not found.", file=sys.stderr)
        return None

    url = _await_vite_ready_url(proc, host=host)
    return proc, url

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li studio` / `li schedule` — Studio launcher and schedule API client."""

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
from lionagi.cli._schedule_parsing import (
    _is_schedule_request_timeout,
    _normalize_schedule_reason,
    _schedule_request_timeout_message,
    add_schedule_subparser,
    build_create_body,
    suggest_schedule_flag,  # noqa: F401 -- still importable from here for existing callers
)
from lionagi.state.db import SCHEDULE_RUN_TERMINAL_STATUSES
from lionagi.studio._host import admitted_host_names, is_unspecified_host, normalize_host

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
    pkg_root = Path(__file__).resolve().parents[1]
    repo_root = pkg_root.parent
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
    import urllib.request

    url = f"http://{_url_authority(host)}:{port}/health"
    if challenge is not None:
        url += "?" + urllib.parse.urlencode({"challenge": challenge})
    outcome: list[object] = []

    def _probe() -> None:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
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


def _start_hosted(host: str, port: int, no_open: bool) -> int:
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


# --- `li schedule` — manage lionagi Studio schedules from the CLI ---


_warned_api_suffix = False
_SCHEDULE_API_TIMEOUT_SECONDS = 10.0


def _url_authority(host: str) -> str:
    """*host* as the authority of a URL: something a client can connect to.

    An unspecified address (`0.0.0.0`, `::`) names no host a client opens, so a
    URL built on it is one the daemon refuses as a Host: it maps to the loopback
    address of the same family. An IPv6 literal is bracketed, or its own colons
    read as the port separator and the URL fails to parse. Anything else is left
    as the caller wrote it.

    Every value is read through `normalize_host` first, so a URL is built from the
    address the value names rather than from its spelling: a blank value is a URL
    with no host at all (`http://:8765` does not even parse), `[localhost]` is a
    bracketed name, and an IPv4-mapped address is the IPv4 address a client
    connects to. The authority here is also the Host the URL sends, so each of
    these has to be one the daemon's Host guard admits under the same value.
    """
    normalized = normalize_host(host)
    if not normalized:
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return normalized
    if address.is_unspecified:
        return "[::1]" if address.version == 6 else "127.0.0.1"
    return f"[{normalized}]" if address.version == 6 else normalized


def _base_url() -> str:
    if url := os.environ.get("LIONAGI_STUDIO_URL"):
        url = url.rstrip("/")
        # Endpoint paths below add /api themselves; strip a base URL that
        # already carries it to avoid hitting /api/api/... and 404ing.
        if url.endswith("/api"):
            url = url.removesuffix("/api")
            global _warned_api_suffix
            if not _warned_api_suffix:
                _warned_api_suffix = True
                warn(
                    f"LIONAGI_STUDIO_URL ends with /api; using {url} as the Studio "
                    "root because endpoint paths add /api themselves. If your proxy "
                    "prefix intentionally ends in /api, point LIONAGI_STUDIO_URL at "
                    "the Studio root instead."
                )
        return url
    host = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
    port = os.environ.get("LIONAGI_STUDIO_PORT", "8765")
    return f"http://{_url_authority(host)}:{port}"


def _api(path: str, method: str = "GET", body: dict | None = None) -> Any:
    """Minimal HTTP helper — no extra deps beyond stdlib urllib."""
    import urllib.error
    import urllib.request

    url = f"{_base_url()}/api/schedules{path}"
    data = json.dumps(body).encode() if body is not None else None
    declares_json = data is not None or method.upper() not in {"GET", "HEAD", "OPTIONS"}
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if declares_json else {},
    )
    started_at = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=_SCHEDULE_API_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode(errors="replace")
        print(f"Error {exc.code}: {msg}", file=sys.stderr)
        return None
    except OSError as exc:
        if _is_schedule_request_timeout(exc):
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            print(
                _schedule_request_timeout_message(
                    method=method,
                    url=url,
                    elapsed_seconds=elapsed_seconds,
                    limit_seconds=_SCHEDULE_API_TIMEOUT_SECONDS,
                ),
                file=sys.stderr,
            )
            return None
        print(
            f"Cannot reach Studio at {_base_url()} — is `li studio` running? ({exc})",
            file=sys.stderr,
        )
        return None


def _cmd_list(args: argparse.Namespace) -> int:
    result = _api("/")
    if result is None:
        return 1
    schedules = result.get("schedules", [])
    if not schedules:
        print("(no schedules)")
        return 0
    for s in schedules:
        status = "enabled" if s.get("enabled") else "disabled"
        line = f"  {s['id']}  {s['name']:<30} [{status}]  {s.get('trigger_type', '?')}"
        if s.get("max_runs"):
            line += f"  (runs left: {s.get('remaining_runs')}/{s['max_runs']})"
        print(line)
    return 0


def _cmd_get(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}")
    if result is None:
        return 1
    print(json.dumps(result, indent=2))
    return 0


def _cmd_limits(args: argparse.Namespace) -> int:
    result = _api("/limits")
    if result is None:
        return 1
    cap = result.get("max_scheduled_concurrent")
    cap_display = "unlimited" if not cap else str(cap)
    print(f"Max concurrent fires:      {cap_display}")
    print(f"Current in-flight:         {result.get('current_inflight', 0)}")
    holders = result.get("current_holders") or []
    print(f"Current holders:          {len(holders)}")
    for holder in holders:
        print(
            f"  {holder.get('schedule_id', '?')}  {holder.get('path', '?')}"
            f"  acquired: {_fmt_rfc3339(holder.get('acquired_at'))}"
        )
    adhoc_cap = result.get("max_adhoc_concurrent")
    adhoc_cap_display = "unlimited" if not adhoc_cap else str(adhoc_cap)
    print(f"Max concurrent ad-hoc:     {adhoc_cap_display}")
    print(f"Current ad-hoc in-flight:  {result.get('current_adhoc_inflight', 0)}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    body, err = build_create_body(args)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    result = _api("/", method="POST", body=body)
    if result is None:
        return 1
    print(f"Created: {result.get('id')}  {result.get('name')}")
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/enable", method="POST")
    if result is None:
        return 1
    print(f"Enabled: {args.id}")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    body = None
    if args.reason is not None:
        try:
            body = {"reason": _normalize_schedule_reason(args.reason)}
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    result = _api(f"/{args.id}/disable", method="POST", body=body)
    if result is None:
        return 1
    print(f"Disabled: {args.id}")
    return 0


# fire_now() hands the run_id back to the HTTP caller before its occurrence
# row is durably written (the fire runs as a background task) -- a lookup
# immediately after trigger can race that insert. Retry within this bounded
# grace period instead of a single up-front lookup.
_TRIGGER_WAIT_GRACE_SECONDS = 5.0
_TRIGGER_WAIT_GRACE_POLL_SECONDS = 0.2
_TRIGGER_WAIT_POLL_SECONDS = 2.0
_TRIGGER_WAIT_MAX_SECONDS = 600.0


def _fmt_rfc3339(ts: float | None) -> str:
    if ts is None:
        return "-"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _fmt_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    secs = ms // 1000
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h{mins:02d}m"


def _fmt_outcome(outcome: dict[str, Any] | None) -> str:
    if not outcome:
        return "-"
    code, summary = outcome.get("code", "?"), outcome.get("summary")
    return f"{code}: {summary}" if summary and summary != code else code


def _fmt_artifacts(artifacts: list[str] | None) -> str:
    if not artifacts:
        return "-"
    return artifacts[0] if len(artifacts) == 1 else f"{artifacts[0]} (+{len(artifacts) - 1} more)"


def _print_run_table(runs: list[dict[str, Any]]) -> None:
    header = f"{'RUN':<14}{'STATUS':<11}{'FIRED':<26}{'DURATION':<10}{'OUTCOME':<32}{'INVOCATION':<14}ARTIFACTS"
    print(header)
    for r in runs:
        outcome = _fmt_outcome(r.get("outcome"))
        print(
            f"{r.get('id', '?'):<14}{r.get('status', '?'):<11}{_fmt_rfc3339(r.get('fired_at')):<26}"
            f"{_fmt_duration_ms(r.get('duration_ms')):<10}{outcome[:30]:<32}"
            f"{r.get('invocation_id') or '-':<14}{_fmt_artifacts(r.get('artifacts'))}"
        )
        if r.get("reason_code") or r.get("reason_summary"):
            print(f"  reason: {r.get('reason_code') or '-'} — {r.get('reason_summary') or '-'}")


def _cmd_trigger(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/trigger", method="POST")
    if result is None:
        return 1
    print(f"Triggered: {args.id}")
    run_id = result.get("run_id") if isinstance(result, dict) else None
    if not run_id:
        return 0
    print(f"Run: {run_id}")
    if not getattr(args, "wait", False):
        return 0
    return _wait_for_run(run_id)


def _wait_for_run(run_id: str) -> int:
    """Poll `/schedules/runs/{run_id}` for the occurrence, tolerating the
    grace-period race, then for a terminal status."""
    import time as _time

    deadline_grace = _time.monotonic() + _TRIGGER_WAIT_GRACE_SECONDS
    run = _api(f"/runs/{run_id}")
    while run is None and _time.monotonic() < deadline_grace:
        _time.sleep(_TRIGGER_WAIT_GRACE_POLL_SECONDS)
        run = _api(f"/runs/{run_id}")
    if run is None:
        print(f"Error: run {run_id!r} never appeared", file=sys.stderr)
        return 1

    deadline = _time.monotonic() + _TRIGGER_WAIT_MAX_SECONDS
    while run.get("status") not in SCHEDULE_RUN_TERMINAL_STATUSES and _time.monotonic() < deadline:
        _time.sleep(_TRIGGER_WAIT_POLL_SECONDS)
        run = _api(f"/runs/{run_id}")
        if run is None:
            print(f"Error: run {run_id!r} disappeared while waiting", file=sys.stderr)
            return 1

    print(f"status: {run.get('status', '?')}  outcome: {_fmt_outcome(run.get('outcome'))}")
    return 0 if run.get("status") == "completed" else 1


def _cmd_delete(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}", method="DELETE")
    if result is None:
        return 1
    print(f"Deleted: {args.id}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    path = f"/{args.id}/runs?limit={args.limit}"
    for status in getattr(args, "status", None) or ():
        path += f"&status={status}"
    result = _api(path)
    if result is None:
        return 1
    runs = result.get("runs", [])
    if getattr(args, "as_json", False):
        print(json.dumps(result))
        return 0
    if not runs:
        print("(no runs)")
        return 0
    _print_run_table(runs)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    result = _api(f"/runs/{args.id}")
    if result is None:
        return 1
    if getattr(args, "as_json", False):
        print(json.dumps(result))
    else:
        _print_run_table([result])
    return 0


def _status_still_running(result: dict[str, Any] | None) -> bool:
    latest = (result or {}).get("latest_run") or {}
    status = latest.get("status")
    return status is not None and status not in SCHEDULE_RUN_TERMINAL_STATUSES


def _cmd_status(args: argparse.Namespace) -> int:
    import time as _time

    result = _api(f"/{args.id}/status")
    if getattr(args, "wait", False):
        deadline = _time.monotonic() + _TRIGGER_WAIT_MAX_SECONDS
        while result is not None and _status_still_running(result) and _time.monotonic() < deadline:
            _time.sleep(_TRIGGER_WAIT_POLL_SECONDS)
            result = _api(f"/{args.id}/status")
    if result is None:
        return 1

    if getattr(args, "as_json", False):
        print(json.dumps(result))
        return int(result.get("exit_code", 2))

    schedule = result.get("schedule") or {}
    latest = result.get("latest_run")
    trigger = (
        f'cron "{schedule["cron_expr"]}"'
        if schedule.get("cron_expr")
        else (f"every {schedule['interval_sec']}s" if schedule.get("interval_sec") else "-")
    )
    print(
        f"{schedule.get('id', args.id)}  {'enabled' if schedule.get('enabled') else 'disabled'}  {trigger}"
    )
    print(f"next:        {_fmt_rfc3339(schedule.get('next_fire_at'))}")
    if latest is None:
        print("last run:    (none)")
        return int(result.get("exit_code", 2))
    print(f"last run:    {latest.get('id')}")
    exit_code_str = f" (exit {latest['exit_code']})" if latest.get("exit_code") is not None else ""
    print(f"status:      {latest.get('status', '?')}{exit_code_str}")
    print(f"fired:       {_fmt_rfc3339(latest.get('fired_at'))}")
    if latest.get("ended_at") is not None:
        print(
            f"ended:       {_fmt_rfc3339(latest.get('ended_at'))}  ({_fmt_duration_ms(latest.get('duration_ms'))})"
        )
    outcome = latest.get("outcome") or {}
    print(f"outcome:     {outcome.get('code', '-')} — {outcome.get('summary', '-')}")
    if latest.get("invocation_id"):
        print(f"invocation:  {latest['invocation_id']}")
    for sid in latest.get("session_ids") or ():
        print(f"session:     {sid}")
    for path in latest.get("artifacts") or ():
        print(f"artifacts:   {path}")
    if latest.get("invocation_id"):
        print(f"inspect:     li monitor {latest['invocation_id']}")
    return int(result.get("exit_code", 2))


def _load_schedule_set_doc(path_str: str):
    """Read + parse a ScheduleSet file. Prints an error and returns
    ``(None, None)`` on any failure -- callers just check for None."""
    from lionagi.studio.services.schedule_declaration import parse_schedule_set

    path = Path(path_str)
    if not path.is_file():
        print(f"Error: file not found: {path}", file=sys.stderr)
        return None, None
    try:
        doc = parse_schedule_set(path.read_text(), source=str(path))
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the user
        print(f"Error: {exc}", file=sys.stderr)
        return None, None
    return doc, path.resolve().parent


def _cmd_validate_set(args: argparse.Namespace) -> int:
    from lionagi.studio.services.schedule_declaration import ScheduleSetError, resolve_schedule_set

    doc, manifest_dir = _load_schedule_set_doc(args.file)
    if doc is None:
        return 1
    owner_key = f"{doc.metadata.project}/{doc.metadata.name}"
    try:
        resolved = resolve_schedule_set(doc, manifest_dir)
    except ScheduleSetError as exc:
        if args.as_json:
            print(
                json.dumps(
                    {
                        "valid": False,
                        "owner_key": owner_key,
                        "errors": [{"name": n, "message": m} for n, m in exc.errors],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for name, message in exc.errors:
                # Every raise site carries the qualified name, so it prints as it stands.
                warn(f"INVALID  {name}  {message}")
        return 1

    if args.as_json:
        print(
            json.dumps(
                {
                    "valid": True,
                    "owner_key": owner_key,
                    "schedules": {name: r.resolved for name, r in resolved.items()},
                },
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
    else:
        print(f"VALID  {owner_key}  {len(resolved)} schedule(s)")
    return 0


def _fmt_plan_line(entry, set_ref: str) -> str:
    from lionagi.studio.services.schedule_declaration import PlanEntry

    e: PlanEntry = entry
    if e.action == "CREATE":
        target = e.resolved.resolved["target"]
        model = target.get("model")
        cwd = e.resolved.resolved["execution"]["cwd"]
        extra = f"  model={model}" if model else ""
        return f"CREATE     {e.qualified_name}{extra}  cwd={cwd}"
    if e.action == "UPDATE":
        if e.model_change is not None:
            stored, declared = e.model_change
            return f"UPDATE     {e.qualified_name}  model={stored} -> {declared}"
        return f"UPDATE     {e.qualified_name}"
    if e.action == "ADOPT":
        origin = (
            f"  adopts existing row {e.adopted_from}" if e.adopted_from else "  adopts existing row"
        )
        return f"ADOPT      {e.qualified_name}{origin}"
    if e.action == "UNCHANGED":
        return f"UNCHANGED  {e.qualified_name}"
    if e.action == "DISABLE":
        return f"DISABLE    {e.qualified_name}     omitted from set {set_ref}"
    return f"ERROR      {e.qualified_name}  {e.detail}"


def _cmd_apply_set(args: argparse.Namespace) -> int:
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedule_declaration import (
        ScheduleSetError,
        apply_schedule_set,
        build_plan,
    )

    doc, manifest_dir = _load_schedule_set_doc(args.file)
    if doc is None:
        return 1
    set_ref = f"{doc.metadata.project}/{doc.metadata.name}"

    async def _run():
        async with StateDB() as db:
            if args.dry_run:
                plan, _resolved = await build_plan(db, doc, manifest_dir, adopt=args.adopt)
                return plan
            result = await apply_schedule_set(db, doc, manifest_dir, adopt=args.adopt)
            return result.plan

    try:
        plan = asyncio.run(_run())
    except ScheduleSetError as exc:
        if args.as_json:
            print(
                json.dumps(
                    {"valid": False, "errors": [{"name": n, "message": m} for n, m in exc.errors]},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for name, message in exc.errors:
                # Every raise site carries the qualified name, so it prints as it stands.
                warn(f"INVALID  {name}  {message}")
        return 1

    has_errors = any(e.action == "ERROR" for e in plan)
    if args.as_json:
        print(
            json.dumps(
                {
                    "dry_run": bool(args.dry_run),
                    "set": set_ref,
                    "plan": [
                        {
                            "name": e.qualified_name,
                            "action": e.action,
                            "detail": e.detail,
                            **({"adopted_from": e.adopted_from} if e.adopted_from else {}),
                            **(
                                {
                                    "model_change": {
                                        "from": e.model_change[0],
                                        "to": e.model_change[1],
                                    }
                                }
                                if e.model_change is not None
                                else {}
                            ),
                        }
                        for e in plan
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for entry in plan:
            # A refused entry goes through the CLI's warning channel, as every
            # other refusal here does; the rest of the plan is ordinary output.
            if entry.action == "ERROR":
                warn(_fmt_plan_line(entry, set_ref))
            else:
                print(_fmt_plan_line(entry, set_ref))
        created = sum(1 for e in plan if e.action == "CREATE")
        adopted = sum(1 for e in plan if e.action == "ADOPT")
        updated = sum(1 for e in plan if e.action == "UPDATE")
        unchanged = sum(1 for e in plan if e.action == "UNCHANGED")
        disabled = sum(1 for e in plan if e.action == "DISABLE")
        # The adopt count appears only when there is one, so output without
        # --adopt reads as it did before adoption existed.
        if args.dry_run:
            adopt_part = f"{adopted} adopt, " if adopted else ""
            print(
                f"Plan: {created} create, {adopt_part}{updated} update, "
                f"{unchanged} unchanged, {disabled} disable"
            )
        elif not has_errors:
            adopt_part = f"{adopted} adopted, " if adopted else ""
            print(
                f"Applied atomically: {created} created, {adopt_part}{updated} updated, "
                f"{unchanged} unchanged, {disabled} disabled"
            )
    return 1 if has_errors else 0


# Distinct from 0 (clean success) and 1 (hard failure, e.g. an unreadable
# input file elsewhere in this CLI); mirrors the EXIT_UNKNOWN=2 "ambiguous,
# needs attention" convention used by `li status`/`li monitor`/`li wait` --
# the document and report are still emitted exactly as on a clean export.
EXIT_EXPORT_PARTIAL = 2


def _cmd_export(args: argparse.Namespace) -> int:
    """`li schedule export` — convert rows into ScheduleSet document(s), one
    per distinct project when the export spans more than one. Read-only:
    never opens a write transaction against the database. Exit code 2 (not
    0/1) when any row was BLOCKED -- see EXIT_EXPORT_PARTIAL."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedule_export import (
        build_managed_export_document,
        convert_legacy_rows,
        dump_schedule_set_yaml,
        format_report,
        is_legacy_row,
        is_managed_row,
    )

    output_path = Path(args.output).resolve() if args.output else None
    manifest_dir = output_path.parent if output_path else Path.cwd()
    flows_dir = (
        manifest_dir / f"{output_path.stem}.flows"
        if output_path
        else manifest_dir / "exported-flows"
    )

    async def _run():
        async with StateDB() as db:
            rows = await db.list_schedules(limit=1_000_000)
            if args.legacy:
                return convert_legacy_rows(
                    [r for r in rows if is_legacy_row(r)],
                    flows_dir=flows_dir,
                    manifest_dir=manifest_dir,
                )
            return build_managed_export_document([r for r in rows if is_managed_row(r)])

    docs, lines = asyncio.run(_run())

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if len(docs) == 1:
            output_path.write_text(dump_schedule_set_yaml(docs[0]))
        else:
            # Mixed-project export: one file per project, suffixed with its
            # project so none silently overwrite `--output`. The sanitizer is
            # not injective (foo/bar and foo:bar both become foo_bar), so
            # collisions are rejected before ANY sibling is written rather
            # than letting the last document win.
            tokens: dict[str, str] = {}
            for doc in docs:
                token = re.sub(r"[^a-zA-Z0-9_.-]", "_", doc.metadata.project)
                if token in tokens:
                    log_error(
                        f"cannot export: projects {tokens[token]!r} and "
                        f"{doc.metadata.project!r} both sanitize to sibling file "
                        f"token {token!r}; export to stdout or rename a project"
                    )
                    return 1
                tokens[token] = doc.metadata.project
            for doc, token in zip(docs, tokens, strict=True):
                sibling = output_path.with_name(f"{output_path.stem}.{token}{output_path.suffix}")
                sibling.write_text(dump_schedule_set_yaml(doc))
    else:
        print("\n---\n".join(dump_schedule_set_yaml(doc) for doc in docs), end="")

    report_text = format_report(lines)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text)
    else:
        print(report_text, file=sys.stderr, end="")

    has_blocked = any(line.status == "BLOCKED" for line in lines)
    return EXIT_EXPORT_PARTIAL if has_blocked else 0


# Typed quick-create — `li schedule create <kind> <name> ...`. Dispatched from
# cli/main.py before the legacy `sched_sub` argparse tree (a reserved kind
# token right after "create"), leaving the flat `li schedule create NAME
# --cron ... --prompt ...` form untouched. Compiles into a ScheduleMember and
# runs it through the same resolve_member()/create_quick_schedule() path a
# ScheduleSet member uses.

QUICK_CREATE_KINDS = ("agent", "flow", "playbook", "command")


def _quick_create_add_trigger_flags(parser: argparse.ArgumentParser) -> None:
    trigger = parser.add_mutually_exclusive_group(required=True)
    trigger.add_argument(
        "--at",
        metavar="RFC3339",
        help=(
            "Fire once at this absolute instant. RFC 3339 with a mandatory "
            "UTC offset and 'T' date/time separator, e.g. "
            "2026-07-15T09:00:00-04:00. Implies max-runs=1."
        ),
    )
    trigger.add_argument(
        "--cron",
        metavar="EXPR",
        help='Cron expression, e.g. "0 2 * * *". Requires --timezone.',
    )
    trigger.add_argument(
        "--every",
        metavar="DURATION",
        help="Strict positive duration, e.g. 30s / 15m / 6h / 2d.",
    )
    trigger.add_argument(
        "--github",
        metavar="OWNER/NAME",
        help="Poll this GitHub repository. Optional --github-filter narrows which PRs fire.",
    )
    parser.add_argument(
        "--timezone",
        metavar="IANA_TZ",
        help="IANA timezone for --cron, e.g. America/New_York. Required with --cron.",
    )
    parser.add_argument(
        "--github-filter",
        dest="github_filter",
        metavar="JSON",
        help='JSON object filtering which PRs fire --github, e.g. \'{"state": "open"}\'.',
    )


def _quick_create_add_policy_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cwd",
        metavar="PATH",
        help="Execution root for the spawned process (default: this CLI's invocation directory).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Sugar for --max-runs 1 — fire once, then auto-disable.",
    )
    parser.add_argument(
        "--max-runs",
        dest="max_runs",
        type=int,
        metavar="N",
        help="Auto-disable once N total runs have fired (default: unlimited). "
        "Mutually exclusive with --once.",
    )
    parser.add_argument(
        "--overlap",
        choices=("skip", "allow"),
        default="skip",
        help="Overlap policy when a prior run is still in-flight (default: skip).",
    )
    parser.add_argument(
        "--missed-fire",
        dest="missed_fire",
        choices=("skip", "run_once"),
        default="skip",
        help="Missed-fire policy (default: skip).",
    )
    parser.add_argument("--budget-usd", dest="budget_usd", type=float, metavar="USD")
    parser.add_argument("--budget-tokens", dest="budget_tokens", type=int, metavar="N")
    parser.add_argument(
        "--rate-limit",
        dest="rate_limit",
        metavar="JSON",
        help='Rolling-window fire cap, e.g. \'{"max_fires": 3, "window_sec": 3600}\'.',
    )
    parser.add_argument("--description", help="Human-readable description.")
    parser.add_argument("--disabled", action="store_true", help="Create the schedule disabled.")


def build_quick_create_parser(kind: str) -> argparse.ArgumentParser:
    """Build the standalone parser for `li schedule create <kind>`."""
    parser = argparse.ArgumentParser(
        prog=f"li schedule create {kind}",
        description=f"Create a typed {kind!r} schedule.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("name", help="Schedule name.")
    if kind == "agent":
        parser.add_argument("--profile", required=True, help="Agent profile name.")
        parser.add_argument("--prompt", help="Prompt text (alternative to --prompt-file).")
        parser.add_argument(
            "--prompt-file",
            dest="prompt_file",
            metavar="PATH",
            help="Read the prompt from a file; '-' reads stdin.",
        )
        parser.add_argument("--model", help="Explicit model override (default: profile's model).")
    elif kind == "flow":
        parser.add_argument("--file", required=True, help="Path to an `li o flow` YAML spec file.")
    elif kind == "playbook":
        parser.add_argument("--playbook", required=True, help="Playbook name.")
        parser.add_argument(
            "--arg",
            dest="args",
            action="append",
            metavar="KEY=VALUE",
            help="Typed playbook argument, repeatable.",
        )
    elif kind == "command":
        # The executable + its argv are captured separately, by splitting the
        # raw argv on a literal '--' *before* this parser ever runs (see
        # run_schedule_quick_create) — nargs=REMAINDER can't be used here
        # since it would greedily swallow the trigger/policy flags that
        # follow `name` too, not just the tokens after '--'.
        pass
    else:  # pragma: no cover — gated by QUICK_CREATE_KINDS at the dispatch site
        raise ValueError(f"unknown quick-create kind: {kind!r}")
    _quick_create_add_trigger_flags(parser)
    _quick_create_add_policy_flags(parser)
    return parser


def _quick_create_trigger(args: argparse.Namespace) -> Any:
    from lionagi.studio.services.schedule_declaration import CronTrigger, GithubTriggerSpec, Trigger

    if args.at:
        return Trigger(at=args.at)
    if args.cron:
        if not args.timezone:
            print("Error: --cron requires --timezone.", file=sys.stderr)
            return None
        return Trigger(cron=CronTrigger(expression=args.cron, timezone=args.timezone))
    if args.every:
        return Trigger(every=args.every)
    # args.github — the only remaining branch, guaranteed by the required
    # mutually-exclusive trigger group.
    github_filter = None
    if getattr(args, "github_filter", None):
        try:
            github_filter = json.loads(args.github_filter)
        except (ValueError, TypeError) as exc:
            print(f"Error: --github-filter must be valid JSON: {exc}", file=sys.stderr)
            return None
        if not isinstance(github_filter, dict):
            print("Error: --github-filter must be a JSON object.", file=sys.stderr)
            return None
    return Trigger(github=GithubTriggerSpec(repo=args.github, filter=github_filter))


def _quick_create_policies(args: argparse.Namespace) -> Any:
    from pydantic import ValidationError

    from lionagi.studio.services.schedule_declaration import Budget, Policies

    if args.once and args.max_runs is not None:
        print("Error: --once and --max-runs are mutually exclusive.", file=sys.stderr)
        return None
    max_runs = 1 if args.once else args.max_runs

    budget = None
    if args.budget_usd is not None or args.budget_tokens is not None:
        try:
            budget = Budget(usd=args.budget_usd, tokens=args.budget_tokens)
        except ValidationError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return None

    rate_limit = None
    if getattr(args, "rate_limit", None):
        try:
            rate_limit = json.loads(args.rate_limit)
        except (ValueError, TypeError) as exc:
            print(f"Error: --rate-limit must be valid JSON: {exc}", file=sys.stderr)
            return None

    try:
        return Policies(
            missedFire=args.missed_fire,
            overlap=args.overlap,
            maxRuns=max_runs,
            budget=budget,
            rateLimit=rate_limit,
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _quick_create_target(kind: str, args: argparse.Namespace) -> Any:
    from lionagi.studio.services.schedule_declaration import (
        AgentTarget,
        CommandTarget,
        FlowTarget,
        PlaybookTarget,
    )

    if kind == "agent":
        prompt = args.prompt
        if args.prompt_file:
            if prompt is not None:
                print("Error: --prompt and --prompt-file are mutually exclusive.", file=sys.stderr)
                return None
            if args.prompt_file == "-":
                prompt = sys.stdin.read()
            else:
                try:
                    prompt = Path(args.prompt_file).read_text()
                except OSError as exc:
                    print(f"Error: could not read --prompt-file: {exc}", file=sys.stderr)
                    return None
        if not prompt or not prompt.strip():
            print("Error: agent target requires --prompt or --prompt-file.", file=sys.stderr)
            return None
        return AgentTarget(kind="agent", profile=args.profile, prompt=prompt, model=args.model)

    if kind == "flow":
        return FlowTarget(kind="flow", file=args.file)

    if kind == "playbook":
        arg_dict: dict[str, str] = {}
        for item in args.args or []:
            if "=" not in item:
                print(f"Error: --arg must be key=value, got {item!r}.", file=sys.stderr)
                return None
            key, _, value = item.partition("=")
            arg_dict[key] = value
        return PlaybookTarget(kind="playbook", name=args.playbook, args=arg_dict)

    # kind == "command": trailing `-- argv...`, never a shell string. The
    # executable/args tokens were already split off before argparse ran (see
    # run_schedule_quick_create) and attached as args.command_argv.
    rest = getattr(args, "command_argv", None)
    if not rest:
        print(
            "Error: command target requires a trailing '--' before the "
            "executable, e.g. `li schedule create command NAME --every 15m "
            "-- refresh-index --incremental`.",
            file=sys.stderr,
        )
        return None
    return CommandTarget(kind="command", executable=rest[0], args=rest[1:])


def run_schedule_quick_create(kind: str, argv: list[str]) -> int:
    """`li schedule create <kind> <name> ...` entry point."""
    if _refused_inside_an_agent():
        return 1
    from pydantic import ValidationError

    from lionagi.state.db import StateDB
    from lionagi.studio.services.schedule_declaration import (
        Execution,
        ScheduleMember,
        ScheduleSetError,
        create_quick_schedule,
    )

    parser = build_quick_create_parser(kind)
    command_argv: list[str] = []
    if kind == "command":
        # Split off the executable/argv at a literal '--' *before* argparse
        # runs: nargs=REMAINDER on a positional would otherwise greedily
        # swallow the trigger/policy flags that come after `name` too.
        if "--" not in argv:
            print(
                "Error: command target requires a trailing '--' before the "
                "executable, e.g. `li schedule create command NAME --every 15m "
                "-- refresh-index --incremental`.",
                file=sys.stderr,
            )
            return 1
        i = argv.index("--")
        argv, command_argv = argv[:i], argv[i + 1 :]
    args = parser.parse_args(argv)
    if kind == "command":
        args.command_argv = command_argv

    target = _quick_create_target(kind, args)
    if target is None:
        return 1
    trigger = _quick_create_trigger(args)
    if trigger is None:
        return 1
    policies = _quick_create_policies(args)
    if policies is None:
        return 1

    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else Path.cwd()

    # Best-effort project auto-detection, same cascade as the legacy create
    # path; any failure here must never block schedule creation.
    project: str | None = None
    with contextlib.suppress(Exception):
        from lionagi.cli._project import detect_project
        from lionagi.studio.scheduler.subprocess import _validate_identifier

        detected, _source = detect_project(cwd)
        if detected:
            _validate_identifier(detected, "action_project")
            project = detected

    try:
        member = ScheduleMember(
            description=args.description,
            enabled=not args.disabled,
            trigger=trigger,
            target=target,
            execution=Execution(cwd=str(cwd)),
            policies=policies,
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    async def _run():
        async with StateDB() as db:
            return await create_quick_schedule(db, args.name, member, cwd=cwd, project=project)

    try:
        schedule_id, resolved = asyncio.run(_run())
    except ScheduleSetError as exc:
        for _name, message in exc.errors:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    print(f"Created: {schedule_id}  {resolved.qualified_name}")
    return 0


# The schedule subcommands that can have the Studio daemon start a run. The daemon runs
# outside the asking process's tree, so no check at spawn can see that the request came
# from inside an agent; the refusal happens here, before the request leaves.
_STARTS_RUNS = frozenset({"create", "enable", "trigger", "apply"})


def _refused_inside_an_agent() -> bool:
    """Print the refusal and return True when this process runs inside an agent lionagi
    started (see refuse_nested_agent)."""
    from lionagi._provider_errors import NestedAgentRefusedError
    from lionagi.providers._subagent_admission import refuse_nested_agent

    try:
        refuse_nested_agent()
    except NestedAgentRefusedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return True
    return False


_ACTION_MAP = {
    "list": _cmd_list,
    "get": _cmd_get,
    "limits": _cmd_limits,
    "create": _cmd_create,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "trigger": _cmd_trigger,
    "delete": _cmd_delete,
    "runs": _cmd_runs,
    "run": _cmd_run,
    "status": _cmd_status,
    "validate": _cmd_validate_set,
    "apply": _cmd_apply_set,
    "export": _cmd_export,
}


@auto_register(
    area="schedule", cli=CliDeclaration(seed="schedule", parser_factory=add_schedule_subparser)
)
def run_schedule(args: argparse.Namespace) -> int:
    action = getattr(args, "schedule_action", None)
    fn = _ACTION_MAP.get(action)
    if fn is None:
        print(
            "Usage: li schedule <subcommand>  (list|get|limits|create|enable|disable|"
            "trigger|delete|runs|run|status|validate|apply|export)"
        )
        return 1
    if action == "runs" and not (1 <= args.limit <= 200):
        print(f"Error: --limit must be between 1 and 200, got {args.limit}.", file=sys.stderr)
        return 1
    if action in _STARTS_RUNS and _refused_inside_an_agent():
        return 1
    return fn(args)

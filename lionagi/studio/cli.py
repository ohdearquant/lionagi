# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li studio` / `li schedule` — Studio launcher and schedule API client."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from lionagi.cli._logging import warn

_STUDIO_IMAGE = "ghcr.io/ohdearquant/lion-studio:latest"
_HOSTED_URL = "https://lion-studio.khive.ai"

# Keys the scheduler engine's chain-fire merge (`{**schedule, **chain_action}`,
# see studio/scheduler/engine.py) actually understands. Anything else would
# still shallow-merge into the fired child schedule row and silently clobber
# unrelated columns (id, trigger_type, cron_expr, ...) — reject it up front.
_CHAIN_ACTION_ALLOWED_KEYS = frozenset(
    {"kind", "action_kind", "model", "prompt", "agent", "playbook", "on_success", "on_fail"}
)


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
    # The same flags are registered on both the parent `studio` parser and the
    # `start` subparser. The subparser must SUPPRESS its defaults, otherwise its
    # unset defaults overwrite values parsed at the parent level (e.g.
    # `li studio --docker start` would silently lose --docker).
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
        help="Host to bind (default: 127.0.0.1)",
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
        help="Run the bundled frontend + backend via Docker",
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
    # Mutual exclusion can be split across the parent parser and the `start`
    # subparser (e.g. `li studio --docker start --web`), which argparse's
    # per-parser groups cannot see. Validate the combined namespace.
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


def run_studio(args: argparse.Namespace) -> int:
    if not getattr(args, "studio_action", None):
        args.studio_action = "start"
    _validate_mode_flags(args)
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


def _start_hosted(host: str, port: int, no_open: bool) -> int:
    daemon_url = f"http://127.0.0.1:{port}"
    print(f"Lion Studio: {_HOSTED_URL}")
    print(f"  connects to your local daemon at {daemon_url}")
    print()
    if not no_open and sys.stdin.isatty() and sys.stdout.isatty():
        import webbrowser

        with contextlib.suppress(Exception):
            webbrowser.open(_HOSTED_URL)
    return _start_backend_only(host, port)


def _start_backend_only(host: str, port: int) -> int:
    import uvicorn

    if not _ensure_apps_importable():
        print(
            "Error: studio backend not found. Run from the lionagi repo root or install "
            "the full studio package.",
            file=sys.stderr,
        )
        return 1

    print(f"Lion Studio API: http://{host}:{port}")
    # Export the actually-resolved bind host so the app's startup security
    # warning (lionagi.studio.app) reflects the real bind address rather than a
    # stale default — the app is loaded via import string and only sees env.
    os.environ["LIONAGI_STUDIO_HOST"] = host
    uvicorn.run("lionagi.studio.app:app", host=host, port=port)
    return 0


def _start_docker(host: str, api_port: int, frontend_port: int) -> int:
    lionagi_home = Path.home() / ".lionagi"
    lionagi_home.mkdir(parents=True, exist_ok=True)

    print(f"Pulling {_STUDIO_IMAGE}...")
    pull = subprocess.run(  # noqa: S603
        ["docker", "pull", _STUDIO_IMAGE],  # noqa: S607
        capture_output=True,
    )
    if pull.returncode != 0:
        stderr = pull.stderr.decode(errors="replace").strip()
        print(f"Warning: docker pull failed: {stderr}", file=sys.stderr)
        print("Trying to use cached image...", file=sys.stderr)

    print()
    print(f"Lion Studio: http://localhost:{api_port}")
    print("Press Ctrl+C to stop")
    print()

    claude_plugins = Path.home() / ".claude" / "plugins"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-p",
        f"{api_port}:8765",
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
        subprocess.run(docker_cmd, check=False)  # noqa: S603
    except KeyboardInterrupt:
        print("\nStopping Lion Studio...")
        subprocess.run(  # noqa: S603
            ["docker", "stop", "lion-studio"],  # noqa: S607
            capture_output=True,
        )
    return 0


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
        frontend_proc = _launch_vite_dev(frontend_dir, frontend_port)
        if frontend_proc:
            print(f"Lion Studio UI (dev):  http://{host}:{frontend_port}")
        print(f"Lion Studio API:       http://{host}:{port}")
    else:
        # Production mode: build dist/ once, then uvicorn serves both UI and API
        # from the same origin — no second process needed.
        if _ensure_frontend_built(frontend_dir):
            # Point the app at the built dist so the SPA fallback activates.
            # app.py reads this env var at module import time; uvicorn loads the
            # app fresh via the import string so the var must be set before the call.
            dist_path = frontend_dir / "dist"
            os.environ["LIONAGI_STUDIO_FRONTEND_DIST"] = str(dist_path)
            print(f"Lion Studio: http://{host}:{port}")
        else:
            print("Warning: frontend build failed; starting API-only mode.", file=sys.stderr)
            print(f"Lion Studio API: http://{host}:{port}")

    print("Press Ctrl+C to stop")

    # Export the actually-resolved bind host so the app's startup security
    # warning (lionagi.studio.app) reflects the real bind address rather than a
    # stale default — the app is loaded via import string and only sees env.
    os.environ["LIONAGI_STUDIO_HOST"] = host
    try:
        uvicorn.run("lionagi.studio.app:app", host=host, port=port)
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


def _launch_vite_dev(
    frontend_dir: Path,
    frontend_port: int,
) -> subprocess.Popen | None:
    """Spawn `npx vite --port <N>` for hot-reload dev mode."""
    env = {**os.environ, "PORT": str(frontend_port)}
    try:
        return subprocess.Popen(  # noqa: S603
            ["npx", "vite", "--port", str(frontend_port)],  # noqa: S607
            cwd=str(frontend_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("Warning: npx not found.", file=sys.stderr)
        return None


# --- `li schedule` — manage lionagi Studio schedules from the CLI ---


_warned_api_suffix = False


def _base_url() -> str:
    if url := os.environ.get("LIONAGI_STUDIO_URL"):
        url = url.rstrip("/")
        # Endpoint paths below add /api themselves; tolerate a base URL that
        # already carries it (an older documented workaround) so requests
        # don't hit /api/api/... and 404. Warn (once) rather than strip
        # silently: a reverse proxy whose public prefix genuinely ends in
        # /api needs to see why its path was rewritten.
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
    return f"http://{host}:{port}"


def _api(path: str, method: str = "GET", body: dict | None = None) -> Any:
    """Minimal HTTP helper — no extra deps beyond stdlib urllib."""
    import urllib.error
    import urllib.request

    url = f"{_base_url()}/api/schedules{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        msg = exc.read().decode(errors="replace")
        print(f"Error {exc.code}: {msg}", file=sys.stderr)
        return None
    except OSError as exc:
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
    print(f"Max concurrent fires: {cap_display}")
    print(f"Current in-flight:    {result.get('current_inflight', 0)}")
    return 0


def _validate_chain_action_node(
    action: Any,
    label: str,
    self_field: str,
    chain_depth: int,
    max_chain_depth: int,
) -> str | None:
    """Validate one chain_action node, recursing into its own nested
    on_success/on_fail the same way the engine's chain-fire would reach them.

    `label` is a human-readable path for error messages (e.g. "--on-success"
    or "--on-success.on_success"). `self_field` is the chain field this node
    was reached through — used for the re-fire warning: does this node set
    its own copy of that field, or will it inherit the parent's via the
    shallow merge? `chain_depth` is the engine chain_depth this node fires at
    if reached (see scheduler/engine.py); recursion stops once that reaches
    `max_chain_depth`, matching the engine's own `chain_depth < _MAX_CHAIN_DEPTH`
    gate — beyond it, a node's own on_success/on_fail is never read.
    """
    if not isinstance(action, dict):
        return f"{label}: must be a JSON object, got {type(action).__name__}"

    unknown = set(action) - _CHAIN_ACTION_ALLOWED_KEYS
    if unknown:
        allowed = ", ".join(sorted(_CHAIN_ACTION_ALLOWED_KEYS))
        return f"{label}: unknown key(s) {sorted(unknown)}; allowed: {allowed}"

    if self_field not in action:
        warn(
            f'{label} does not set its own "{self_field}" key — under the '
            f"engine's shallow merge, the chained run will inherit its "
            f"parent's {self_field} and may re-fire again at the next chain "
            f'depth. Add "{self_field}": null to the JSON to stop the chain '
            "here."
        )

    if chain_depth >= max_chain_depth:
        return None

    for nested_field in ("on_success", "on_fail"):
        if nested_field in action and action[nested_field] is not None:
            err = _validate_chain_action_node(
                action[nested_field],
                f"{label}.{nested_field}",
                nested_field,
                chain_depth + 1,
                max_chain_depth,
            )
            if err:
                return err
    return None


def _parse_chain_action(raw: str, flag: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse+validate a --on-success/--on-fail JSON blob, recursively —
    nested on_success/on_fail chain actions are validated the same way as
    the top level, since they ride the same shallow merge into the engine's
    fired child schedules.

    Returns (parsed_dict, error_message); error_message is None on success.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{flag}: invalid JSON ({exc})"

    from lionagi.studio.scheduler.engine import _MAX_CHAIN_DEPTH

    field = "on_success" if flag == "--on-success" else "on_fail"
    err = _validate_chain_action_node(
        parsed, flag, field, chain_depth=1, max_chain_depth=_MAX_CHAIN_DEPTH
    )
    if err:
        return None, err
    return parsed, None


def _warn_if_cron_far_out(cron_expr: str) -> None:
    """Best-effort heads-up when a cron expression's next fire is far out.

    Addresses the date-pinned one-shot footgun: a cron schedule created
    after its literal moment for this year (but meant to fire "today")
    silently resolves to the same date *next* year instead. croniter is
    part of the `studio` extra, not a core dependency, so this degrades to
    a no-op rather than failing schedule creation when it isn't installed.
    """
    try:
        from croniter import croniter
    except ImportError:
        return
    import time as _time
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    try:
        next_fire = croniter(cron_expr, start_time=now).get_next(float)
    except Exception:
        return
    days_out = (next_fire - _time.time()) / 86400
    if days_out > 360:
        warn(
            f"cron {cron_expr!r} next fires in about {days_out:.0f} days. "
            "If you meant a one-shot for a specific date this year, the "
            "schedule may have been created after that date's moment has "
            "already passed (cron resolves in UTC) and silently waits a "
            "full year. Consider --max-runs / --once plus a nearer date."
        )


def _cmd_create(args: argparse.Namespace) -> int:
    if args.once and args.max_runs is not None:
        print("Error: --once and --max-runs are mutually exclusive.", file=sys.stderr)
        return 1
    max_runs = 1 if args.once else args.max_runs
    if max_runs is not None and max_runs < 1:
        print(f"Error: --max-runs must be a positive integer, got {max_runs}.", file=sys.stderr)
        return 1

    # 'github' is a friendly alias; the DB CHECK and scheduler engine only
    # recognize the canonical 'github_poll' token.
    trigger_type = "github_poll" if args.trigger_type == "github" else args.trigger_type

    body: dict[str, Any] = {
        "name": args.name,
        "trigger_type": trigger_type,
        "action_kind": args.action_kind,
    }
    if args.cron:
        body["cron_expr"] = args.cron
        _warn_if_cron_far_out(args.cron)
    if args.interval:
        body["interval_sec"] = args.interval
    if getattr(args, "github_repo", None):
        body["github_repo"] = args.github_repo
    if getattr(args, "github_filter", None):
        try:
            parsed_filter = json.loads(args.github_filter)
        except (ValueError, TypeError) as exc:
            print(f"Error: --github-filter must be valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(parsed_filter, dict):
            print("Error: --github-filter must be a JSON object.", file=sys.stderr)
            return 1
        body["github_filter"] = parsed_filter
    if getattr(args, "threshold_config", None):
        try:
            parsed_threshold = json.loads(args.threshold_config)
        except (ValueError, TypeError) as exc:
            print(f"Error: --threshold-config must be valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(parsed_threshold, dict):
            print("Error: --threshold-config must be a JSON object.", file=sys.stderr)
            return 1
        # Shape/value validation (metric/op vocab, positive window_minutes,
        # etc.) happens server-side in _svc_validate_threshold_config --
        # this is just the same JSON-object shape check --github-filter does.
        body["threshold_config"] = parsed_threshold
    if getattr(args, "poll_interval", None) is not None:
        if args.poll_interval < 1:
            print("Error: --poll-interval must be a positive integer.", file=sys.stderr)
            return 1
        body["poll_interval_sec"] = args.poll_interval
    if max_runs is not None:
        body["max_runs"] = max_runs
    if getattr(args, "max_cost_usd", None) is not None:
        if not math.isfinite(args.max_cost_usd) or args.max_cost_usd <= 0:
            print(
                f"Error: --max-cost-usd must be a finite positive number, got {args.max_cost_usd}.",
                file=sys.stderr,
            )
            return 1
        body["budget_usd"] = args.max_cost_usd
    if getattr(args, "max_tokens", None) is not None:
        if args.max_tokens <= 0:
            print(
                f"Error: --max-tokens must be a positive integer, got {args.max_tokens}.",
                file=sys.stderr,
            )
            return 1
        body["budget_tokens"] = args.max_tokens
    if args.prompt:
        body["action_prompt"] = args.prompt
    if args.model:
        body["action_model"] = args.model
    if args.agent:
        body["action_agent"] = args.agent
    if args.playbook:
        body["action_playbook"] = args.playbook
    if getattr(args, "flow_yaml", None):
        p = Path(args.flow_yaml).expanduser()
        if not p.is_file():
            print(f"Error: flow-yaml file not found: {p}", file=sys.stderr)
            return 1
        body["action_flow_yaml"] = p.read_text()
    if args.project:
        body["action_project"] = args.project
    else:
        # Best-effort: auto-capture the project from cwd (ADR-0026 detection
        # cascade) so a schedule created inside a registered project resolves
        # its spawn cwd at trigger time (see scheduler.engine._resolve_action_cwd).
        # Any failure here must never block schedule creation.
        with contextlib.suppress(Exception):
            from lionagi.cli._project import detect_project
            from lionagi.studio.scheduler.subprocess import _validate_identifier

            detected, _source = detect_project(Path.cwd())
            if detected:
                _validate_identifier(detected, "action_project")
                body["action_project"] = detected
    if args.description:
        body["description"] = args.description
    if args.on_success:
        parsed, err = _parse_chain_action(args.on_success, "--on-success")
        if err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        body["on_success"] = parsed
    if args.on_fail:
        parsed, err = _parse_chain_action(args.on_fail, "--on-fail")
        if err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        body["on_fail"] = parsed
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
    result = _api(f"/{args.id}/disable", method="POST")
    if result is None:
        return 1
    print(f"Disabled: {args.id}")
    return 0


def _cmd_trigger(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/trigger", method="POST")
    if result is None:
        return 1
    print(f"Triggered: {args.id}")
    if isinstance(result, dict) and result.get("run_id"):
        print(f"Run: {result['run_id']}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}", method="DELETE")
    if result is None:
        return 1
    print(f"Deleted: {args.id}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    result = _api(f"/{args.id}/runs")
    if result is None:
        return 1
    runs = result.get("runs", [])
    if not runs:
        print("(no runs)")
        return 0
    for r in runs:
        print(f"  {r['id']}  [{r.get('status', '?')}]  {r.get('started_at', '?')}")
    return 0


# Common wrong spellings/guesses mapped to the real flag they were probably
# aiming for — checked before the generic difflib fuzzy match below, since
# some of these (e.g. --every for --interval) aren't close enough by edit
# distance for difflib to catch on its own.
_SCHEDULE_FLAG_SYNONYMS: dict[str, str] = {
    "--every": "--interval",
    "--at": "--cron",
    "--action": "--action-kind",
    "--on_success": "--on-success",
    "--on_fail": "--on-fail",
    "--max_runs": "--max-runs",
}

# Populated by add_schedule_subparser() with every long option string across
# all `li schedule` subcommands, for fuzzy did-you-mean matching.
_ALL_SCHEDULE_FLAGS: set[str] = set()


def suggest_schedule_flag(token: str) -> str | None:
    """Return a suggested correction for an unrecognized `li schedule` flag.

    Checks the explicit synonym map first (catches guesses too far from the
    real flag for edit-distance matching, e.g. "--every" for "--interval"),
    then falls back to a fuzzy match against every registered flag.
    """
    if token in _SCHEDULE_FLAG_SYNONYMS:
        return _SCHEDULE_FLAG_SYNONYMS[token]
    import difflib

    matches = difflib.get_close_matches(token, _ALL_SCHEDULE_FLAGS, n=1, cutoff=0.6)
    return matches[0] if matches else None


def add_schedule_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register `li schedule` sub-command. Returns the `schedule` parser."""
    sched = subparsers.add_parser(
        "schedule",
        help="Manage lionagi Studio schedules.",
        description=(
            "Create, list, enable, disable, trigger, and delete "
            "schedules via the Studio API (default http://127.0.0.1:8765). "
            "Set LIONAGI_STUDIO_URL to use a different base URL."
        ),
    )
    sched_sub = sched.add_subparsers(dest="schedule_action")
    sched_sub.required = True

    # list
    sched_sub.add_parser(
        "list",
        help="List all schedules.",
        epilog="Example: li schedule list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # get
    get_p = sched_sub.add_parser(
        "get",
        help="Show schedule details.",
        epilog="Example: li schedule get sched-abc123",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    get_p.add_argument("id", help="Schedule ID.")

    # limits
    sched_sub.add_parser(
        "limits",
        help="Show the global concurrent-fire cap and current in-flight count.",
        epilog="Example: li schedule limits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # create
    create_p = sched_sub.add_parser(
        "create",
        help="Create a new schedule.",
        epilog=(
            "Examples:\n"
            '  li schedule create daily-digest --cron "0 9 * * *" \\\n'
            '      --prompt "summarize overnight activity"\n'
            "  li schedule create hourly-poll --interval 3600 --agent researcher\n"
            '  li schedule create one-shot-backfill --cron "0 18 2 7 *" --once\n'
            '  li schedule create nightly-chain --cron "0 2 * * *" --prompt build \\\n'
            '      --on-success \'{"prompt": "notify done", "on_success": null}\'\n'
            "      # WARNING: --on-success/--on-fail shallow-merge into the chained\n"
            "      # run — any key you omit is INHERITED from this schedule,\n"
            '      # including on_success/on_fail themselves; set "on_success": null\n'
            "      # explicitly at each level to stop the chain there."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    create_p.add_argument("name", help="Schedule name.")
    create_p.add_argument(
        "--trigger-type",
        dest="trigger_type",
        default="cron",
        choices=("cron", "interval", "github", "github_poll"),
        help="Trigger type (default: cron). 'github' is an alias for 'github_poll'.",
    )
    create_p.add_argument("--cron", metavar="EXPR", help='Cron expression, e.g. "0 * * * *".')
    create_p.add_argument("--interval", type=int, metavar="SECONDS", help="Interval in seconds.")
    create_p.add_argument(
        "--github-repo",
        dest="github_repo",
        metavar="OWNER/NAME",
        help="GitHub repository to poll (required for --trigger-type github/github_poll).",
    )
    create_p.add_argument(
        "--github-filter",
        dest="github_filter",
        metavar="JSON",
        help=(
            "JSON object filtering which PRs fire the trigger, e.g. "
            '\'{"state": "open", "base": "main"}\'.'
        ),
    )
    create_p.add_argument(
        "--threshold-config",
        dest="threshold_config",
        metavar="JSON",
        help=(
            "Metric threshold alert config as a JSON object: "
            '{"metric": "failed_sessions|total_cost_usd|p95_latency_ms|'
            'github_poll_healthy_age_minutes|github_poll_consecutive_401", '
            '"op": "gt|gte", "value": N, "window_minutes": N}. When set, '
            "this schedule's own cron/interval cadence only evaluates the "
            "metric on each tick and fires the action only when the "
            "threshold is breached (cooldown = window_minutes). Full "
            'validation happens server-side, e.g. \'{"metric": '
            '"failed_sessions", "op": "gt", "value": 5, "window_minutes": 60}\'.'
        ),
    )
    create_p.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=int,
        metavar="SECONDS",
        help="How often to poll GitHub, in seconds (github_poll only).",
    )
    create_p.add_argument(
        "--action-kind",
        dest="action_kind",
        default="agent",
        choices=("agent", "playbook", "flow_yaml"),
        help="Action kind (default: agent).",
    )
    create_p.add_argument("--prompt", help="Prompt for agent action.")
    create_p.add_argument("--model", help="Model spec for agent action.")
    create_p.add_argument("--agent", help="Agent profile name.")
    create_p.add_argument("--playbook", help="Playbook name (for action-kind=playbook).")
    create_p.add_argument(
        "--flow-yaml",
        dest="flow_yaml",
        metavar="FILE",
        help="Path to a YAML flow spec file (for action-kind=flow_yaml).",
    )
    create_p.add_argument("--project", help="Project name.")
    create_p.add_argument("--description", help="Human-readable description.")
    create_p.add_argument(
        "--max-runs",
        dest="max_runs",
        type=int,
        metavar="N",
        help=(
            "Auto-disable this schedule once N total runs have fired "
            "(default: unlimited). Chained on_success/on_fail fires do not "
            "count toward N. Mutually exclusive with --once."
        ),
    )
    create_p.add_argument(
        "--once",
        dest="once",
        action="store_true",
        help="Sugar for --max-runs 1 — fire once, then auto-disable.",
    )
    create_p.add_argument(
        "--max-cost-usd",
        dest="max_cost_usd",
        type=float,
        metavar="USD",
        help=(
            "Auto-disable this schedule once its cumulative session spend "
            "reaches USD (default: unlimited). Pre-fire cumulative gate: an "
            "in-flight run is not interrupted, so the schedule may overshoot "
            "by up to one run's cost before the next fire is refused."
        ),
    )
    create_p.add_argument(
        "--max-tokens",
        dest="max_tokens",
        type=int,
        metavar="N",
        help=(
            "Auto-disable this schedule once its cumulative session token "
            "usage (input+output) reaches N (default: unlimited). Same "
            "pre-fire cumulative semantics as --max-cost-usd."
        ),
    )
    create_p.add_argument(
        "--on-success",
        dest="on_success",
        metavar="JSON",
        help=(
            "Chain action to fire when this run exits 0, as a JSON object "
            "(allowed keys: kind/action_kind, model, prompt, agent, playbook, "
            "on_success, on_fail). WARNING — shallow merge: the chain child is "
            "built as {**this_schedule, **on_success}, so any key you omit is "
            "INHERITED from this schedule, including on_success/on_fail "
            "themselves. A 2-level chain must set the inner level's own "
            '"on_success": null explicitly, or the chain keeps re-firing at '
            "each depth (capped, but rarely what you want). Example: "
            '--on-success \'{"prompt": "notify done", "on_success": null}\'.'
        ),
    )
    create_p.add_argument(
        "--on-fail",
        dest="on_fail",
        metavar="JSON",
        help=(
            "Chain action to fire when this run exits non-zero, as a JSON "
            "object (same allowed keys and shallow-merge caveat as "
            "--on-success — see above). Example: --on-fail "
            '\'{"prompt": "alert on-call", "on_fail": null}\'.'
        ),
    )

    # enable / disable / trigger / delete
    for sub_name, sub_help, example in (
        ("enable", "Enable a schedule.", "li schedule enable sched-abc123"),
        ("disable", "Disable a schedule.", "li schedule disable sched-abc123"),
        ("trigger", "Fire a schedule immediately.", "li schedule trigger sched-abc123"),
        ("delete", "Delete a schedule.", "li schedule delete sched-abc123"),
    ):
        p = sched_sub.add_parser(
            sub_name,
            help=sub_help,
            epilog=f"Example: {example}",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("id", help="Schedule ID.")

    # runs
    runs_p = sched_sub.add_parser(
        "runs",
        help="List runs for a schedule.",
        epilog="Example: li schedule runs sched-abc123",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    runs_p.add_argument("id", help="Schedule ID.")

    _ALL_SCHEDULE_FLAGS.clear()
    for action_parser in sched_sub.choices.values():
        _ALL_SCHEDULE_FLAGS.update(
            opt
            for opt in action_parser._option_string_actions
            if opt.startswith("--") and opt != "--help"
        )

    return sched


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
}


def run_schedule(args: argparse.Namespace) -> int:
    action = getattr(args, "schedule_action", None)
    fn = _ACTION_MAP.get(action)
    if fn is None:
        print(
            "Usage: li schedule <subcommand>  "
            "(list|get|limits|create|enable|disable|trigger|delete|runs)"
        )
        return 1
    return fn(args)

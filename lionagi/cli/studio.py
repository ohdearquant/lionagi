# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from lionagi.cli._logging import warn

_STUDIO_IMAGE = "ghcr.io/ohdearquant/lion-studio:latest"


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


def _add_studio_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Backend API port (default: LIONAGI_STUDIO_PORT env or 8765)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=3000,
        dest="frontend_port",
        help="Frontend port (default: 3000)",
    )
    parser.add_argument(
        "--no-frontend",
        action="store_true",
        dest="no_frontend",
        help="Only start the backend API server",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Run frontend in dev mode (hot-reload, no build step)",
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        dest="no_docker",
        help="Don't use Docker even if available",
    )


def add_studio_subparser(subparsers: argparse._SubParsersAction) -> None:
    studio_parser = subparsers.add_parser("studio", help="Lion Studio server")
    _add_studio_flags(studio_parser)

    studio_sub = studio_parser.add_subparsers(dest="studio_action")
    studio_sub.required = False

    start_parser = studio_sub.add_parser("start", help="Start Lion Studio")
    _add_studio_flags(start_parser)


def run_studio(args: argparse.Namespace) -> int:
    if not getattr(args, "studio_action", None):
        args.studio_action = "start"
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
    no_docker: bool = getattr(args, "no_docker", False)
    dev_mode: bool = getattr(args, "dev", False)
    frontend_port: int = getattr(args, "frontend_port", 3000)

    frontend_dir = _find_frontend_dir()

    if no_frontend:
        return _start_backend_only(host, port)

    if dev_mode or frontend_dir:
        return _start_local(host, port, frontend_port, frontend_dir, dev_mode)

    if not no_docker and _has_docker():
        return _start_docker(host, port, frontend_port)

    # Fallback: backend only
    print("Lion Studio: starting backend only (no frontend available)")
    print()
    print("To get the full UI, either:")
    print(f"  1. Install Docker and run: li studio        (auto-pulls {_STUDIO_IMAGE})")
    print("  2. Clone the repo and build once:")
    print("       git clone https://github.com/ohdearquant/lionagi.git")
    print("       cd lionagi && li studio")
    print()
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

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_STUDIO_IMAGE = "ghcr.io/ohdearquant/lion-studio:latest"


def _mount_allowed_roots() -> list[Path]:
    """Return the ordered list of host path prefixes that may be bind-mounted.

    Only resolved (real) paths whose first component is one of these roots are
    permitted. Anything outside — including /etc, /proc, /var, or paths that
    escape via double-symlink chains — is rejected before it reaches the docker
    run argv.

    The set is intentionally conservative: the user's home directory and the
    XDG config home (which defaults to ~/.config). Projects that need additional
    roots should symlink from inside an allowed root rather than pointing
    directly at a system path.
    """
    roots: list[Path] = [Path.home().resolve()]
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        xdg_path = Path(xdg_config).resolve()
        if xdg_path not in roots:
            roots.append(xdg_path)
    return roots


def _is_mount_allowed(resolved_path: Path, allowed_roots: list[Path]) -> bool:
    """Return True when *resolved_path* is strictly under an allowed root.

    Both *resolved_path* and every entry in *allowed_roots* must already be
    fully resolved (no symlinks). The check uses Path.is_relative_to so that
    a root of ``/home/user`` does not match ``/home/username``.
    """
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
    """Return the lionagi repo root if we're running from a source checkout."""
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
    """Add repo root to sys.path so `apps.studio.server.app` is importable.

    Returns True if the apps package is available (either already on path or
    we added the repo root). Returns False if running from an installed wheel
    with no source checkout nearby.
    """
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

    # Decision tree:
    # 1. --no-frontend → backend only
    # 2. --dev or local frontend found → local Node.js
    # 3. Docker available → pull and run container (serves both)
    # 4. Fallback → backend only with instructions

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
    print("  2. Clone the repo:        git clone https://github.com/ohdearquant/lionagi.git")
    print("                            cd lionagi && li studio --dev")
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
    print(f"Lion Studio UI:  http://localhost:{frontend_port}")
    print(f"Lion Studio API: http://localhost:{api_port}")
    print("Press Ctrl+C to stop")
    print()

    # Mount Claude Code's third-party plugin cache (if present) so Studio's
    # Library tab can enumerate installed plugins beyond the bundled marketplace.
    claude_plugins = Path.home() / ".claude" / "plugins"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-p",
        f"{api_port}:8765",
        "-p",
        f"{frontend_port}:3000",
        "-v",
        f"{lionagi_home}:/root/.lionagi",
    ]
    if claude_plugins.is_dir():
        docker_cmd.extend(["-v", f"{claude_plugins}:/root/.claude/plugins:ro"])

    # Auto-discover symlink targets in ~/.lionagi/{agents,skills,playbooks,teams}
    # and mount their parent directories read-only so Studio can follow the
    # symlinks inside the container. Many power-user setups symlink content
    # from external project dirs (e.g. ~/projects/firm/agents/*) into
    # ~/.lionagi/agents/ — without these extra mounts the symlinks dangle.
    #
    # Security constraint: symlink targets are resolved to their real path
    # (no symlink chain games) and then checked against an allowlist of safe
    # roots before they are added to the docker run argv. Any target that
    # resolves outside the allowlist is silently dropped so the docker run
    # argv is never contaminated by an escape path.
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
                continue  # broken symlink — skip
            # Mount the target's parent directory at its real host path so the
            # symlink resolves identically inside the container. For directory
            # targets, mount the target itself.
            mount_src = target if target.is_dir() else target.parent
            if not _is_mount_allowed(mount_src, allowed_roots):
                print(
                    f"Warning: symlink target {mount_src} is outside the allowed mount "
                    "roots and will not be mounted.",
                    file=sys.stderr,
                )
                continue
            symlink_mounts.add(mount_src)

    for mount_src in sorted(symlink_mounts):
        # Same host path inside the container so the symlink target string
        # in ~/.lionagi/* resolves correctly. Read-only — Studio should
        # never write to source-of-truth project dirs.
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

    frontend_proc = _launch_frontend(frontend_dir, frontend_port, port, dev_mode)

    if frontend_proc:
        print(f"Lion Studio UI:  http://{host}:{frontend_port}")
    print(f"Lion Studio API: http://{host}:{port}")
    print("Press Ctrl+C to stop")

    if not _ensure_apps_importable():
        if frontend_proc:
            frontend_proc.terminate()
        print(
            "Error: studio backend not found. Run from the lionagi repo root or install "
            "the full studio package.",
            file=sys.stderr,
        )
        return 1

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
    """Return True when the production build is absent or older than source files.

    The check compares the mtime of ``.next/BUILD_ID`` — written by Next.js on
    every successful ``next build`` — against the newest mtime found under the
    source trees that affect the compiled bundle: ``app/``, ``lib/``,
    ``components/``, ``package.json``, and ``next.config.mjs``.  Any source file
    newer than the build marker means the cached bundle is stale.

    Returns True (rebuild required) in three situations:
    - ``.next/BUILD_ID`` is absent (no prior build).
    - A source file is newer than ``.next/BUILD_ID``.
    - ``os.stat`` raises for any path (safe default: rebuild).
    """
    build_marker = frontend_dir / ".next" / "BUILD_ID"
    if not build_marker.exists():
        return True

    try:
        marker_mtime = build_marker.stat().st_mtime
    except OSError:
        return True  # cannot stat marker — rebuild to be safe

    # Source subtrees that, when changed, invalidate the compiled bundle.
    source_roots = [
        frontend_dir / "app",
        frontend_dir / "lib",
        frontend_dir / "components",
    ]
    # Top-level config files that affect the build.
    source_files = [
        frontend_dir / "package.json",
        frontend_dir / "next.config.mjs",
    ]

    for f in source_files:
        try:
            if f.exists() and f.stat().st_mtime > marker_mtime:
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
                return True  # unreadable source file — rebuild to be safe

    return False


def _launch_frontend(
    frontend_dir: Path,
    frontend_port: int,
    api_port: int,
    dev_mode: bool,
) -> subprocess.Popen | None:
    env = {
        **os.environ,
        "NEXT_PUBLIC_STUDIO_API_BASE": f"http://localhost:{api_port}",
        "PORT": str(frontend_port),
    }

    if not (frontend_dir / "node_modules").exists():
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
            return None

    if dev_mode:
        cmd = ["npx", "next", "dev", "--port", str(frontend_port)]  # noqa: S607
    else:
        # Rebuild whenever the source is newer than the last successful build.
        # NEXT_PUBLIC_STUDIO_API_BASE is inlined at build time, so a stale
        # .next/ bundle may point at the wrong API origin — always rebuild on
        # source changes to prevent "API unreachable" from stale bundles.
        if _is_build_stale(frontend_dir):
            print("Building frontend...")
            try:
                subprocess.run(  # noqa: S603
                    ["npx", "next", "build"],  # noqa: S607
                    cwd=str(frontend_dir),
                    env=env,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as e:
                print(f"Warning: frontend build failed: {e}", file=sys.stderr)
                return None
        cmd = ["npx", "next", "start", "--port", str(frontend_port)]  # noqa: S607

    try:
        return subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(frontend_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        print("Warning: npx not found.", file=sys.stderr)
        return None

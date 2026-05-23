from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_STUDIO_IMAGE = "ghcr.io/ohdearquant/lion-studio:latest"


def add_studio_subparser(subparsers: argparse._SubParsersAction) -> None:
    studio_parser = subparsers.add_parser("studio", help="Lion Studio server")
    studio_sub = studio_parser.add_subparsers(dest="studio_action")
    studio_sub.required = False

    start_parser = studio_sub.add_parser("start", help="Start Lion Studio")
    start_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Backend API port (default: LIONAGI_STUDIO_PORT env or 8765)",
    )
    start_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1)",
    )
    start_parser.add_argument(
        "--frontend-port",
        type=int,
        default=3000,
        dest="frontend_port",
        help="Frontend port (default: 3000)",
    )
    start_parser.add_argument(
        "--no-frontend",
        action="store_true",
        dest="no_frontend",
        help="Only start the backend API server",
    )
    start_parser.add_argument(
        "--dev",
        action="store_true",
        help="Run frontend in dev mode (requires repo clone)",
    )
    start_parser.add_argument(
        "--no-docker",
        action="store_true",
        dest="no_docker",
        help="Don't use Docker even if available",
    )


def run_studio(args: argparse.Namespace) -> int:
    if not getattr(args, "studio_action", None):
        args.studio_action = "start"
    return _studio_start(args)


def _find_frontend_dir() -> Path | None:
    pkg_root = Path(__file__).resolve().parents[1]
    repo_root = pkg_root.parent
    candidate = repo_root / "apps" / "studio" / "frontend"
    if (candidate / "package.json").exists():
        return candidate
    return None


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
        getattr(args, "port", None)
        or (int(port_from_env) if port_from_env else None)
        or 8765
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

    print(f"Lion Studio API: http://{host}:{port}")
    uvicorn.run("apps.studio.server.app:app", host=host, port=port)
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

    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker", "run", "--rm",
                "-p", f"{api_port}:8765",
                "-p", f"{frontend_port}:3000",
                "-v", f"{lionagi_home}:/root/.lionagi",
                "--name", "lion-studio",
                _STUDIO_IMAGE,
            ],
        )
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
        print("Error: Node.js required for local frontend. Install from https://nodejs.org/", file=sys.stderr)
        return 1

    frontend_proc = _launch_frontend(frontend_dir, frontend_port, port, dev_mode)

    if frontend_proc:
        print(f"Lion Studio UI:  http://{host}:{frontend_port}")
    print(f"Lion Studio API: http://{host}:{port}")

    try:
        uvicorn.run("apps.studio.server.app:app", host=host, port=port)
    finally:
        if frontend_proc:
            frontend_proc.terminate()
            frontend_proc.wait()
    return 0


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
        if not (frontend_dir / ".next").exists():
            print("Building frontend (first run)...")
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
        )
    except FileNotFoundError:
        print("Warning: npx not found.", file=sys.stderr)
        return None

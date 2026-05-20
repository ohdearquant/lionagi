from __future__ import annotations

import argparse
import os
import sys


def add_studio_subparser(subparsers: argparse._SubParsersAction) -> None:
    studio_parser = subparsers.add_parser("studio", help="Lion Studio server")
    studio_sub = studio_parser.add_subparsers(dest="studio_action", required=True)

    start_parser = studio_sub.add_parser("start", help="Start the Lion Studio backend server")
    start_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: LIONAGI_STUDIO_PORT env or 8765)",
    )
    start_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1)",
    )
    start_parser.add_argument(
        "--frontend-mode",
        choices=["dev", "start", "none"],
        default="none",
        dest="frontend_mode",
        help="Frontend launch mode (default: none)",
    )
    start_parser.add_argument(
        "--no-frontend",
        action="store_true",
        dest="no_frontend",
        help="Do not launch the frontend (implied when --frontend-mode=none)",
    )


def run_studio(args: argparse.Namespace) -> int:
    return _studio_start(args)


def _studio_start(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is required. Install it with: pip install 'lionagi[studio]'",
            file=sys.stderr,
        )
        return 1

    port_from_env = os.environ.get("LIONAGI_STUDIO_PORT")
    port: int = (
        args.port
        or (int(port_from_env) if port_from_env else None)
        or 8765
    )
    host: str = getattr(args, "host", "127.0.0.1")
    frontend_mode: str = getattr(args, "frontend_mode", "none")
    no_frontend: bool = getattr(args, "no_frontend", False)

    if frontend_mode != "none" and not no_frontend:
        print(
            "Warning: frontend not yet lifted — skipping frontend launch.",
            file=sys.stderr,
        )

    print(f"Starting Lion Studio Server on http://{host}:{port}")
    uvicorn.run("apps.studio.server.app:app", host=host, port=port)
    return 0

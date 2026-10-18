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

    # Production:
    uvicorn server.app:app --host 0.0.0.0 --port 8000 --workers 4

    # Or run directly:
    python -m server.app
"""

try:
    from openenv.core.env_server.http_server import create_app
except Exception as e:  # pragma: no cover
    raise ImportError(
        "openenv is required for the web interface. Install dependencies with '\n    uv sync\n'"
    ) from e

try:
    from ..models import WorkbenchAction, WorkbenchObservation
    from .agent_workbench_environment import AgentWorkbenchEnvironment
except ImportError:
    from models import WorkbenchAction, WorkbenchObservation
    from server.agent_workbench_environment import AgentWorkbenchEnvironment


# Create the app with web interface and README integration
app = create_app(
    AgentWorkbenchEnvironment,
    WorkbenchAction,
    WorkbenchObservation,
    env_name="agent_workbench",
    max_concurrent_envs=8,
)


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

    For production deployments, consider using uvicorn directly with
    multiple workers:
        uvicorn agent_workbench.server.app:app --workers 4
    """
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    main(port=args.port)

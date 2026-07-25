# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Paths and CLI resolution for the lionagi MCP server.

The server is a thin control plane over the ``li`` CLI. It never re-implements a
run; it spawns the same command a human would type, then reads the run state
lionagi already persists under ``LIONAGI_HOME/runs/{run_id}/``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from lionagi._paths import LIONAGI_HOME, RUNS_ROOT

# Authoritative per-run state written by the CLI (run.json, branches, artifacts).
RUNS_DIR = RUNS_ROOT

# The MCP server's own per-job records (pid, argv, console log) — data the CLI
# does not keep because it never needed a background handle before.
JOBS_DIR = LIONAGI_HOME / "mcp" / "jobs"

# The env var the CLI reads to inherit a caller-chosen run_id (subprocess
# handoff, lionagi/cli/_runs.py). Setting it lets submit() name the run before
# the child starts, so the id we return is race-free.
RUN_ID_ENV_VAR = "LIONAGI_RUN_ID"

# Explicit override for the argv prefix that invokes the ``li`` CLI, split on
# whitespace. Rarely needed: the server runs inside lionagi's own environment,
# so the interpreter running it already resolves the CLI (see li_command).
LI_BIN_ENV_VAR = "LIONAGI_MCP_LI_BIN"


def li_command() -> list[str]:
    """Return the argv prefix that invokes the ``li`` CLI.

    The server runs inside lionagi's own environment, so the CLI it spawns is
    the one installed alongside the running interpreter — no working-tree
    hunting, no dependency resync on spawn. Resolution order:

      1. ``LIONAGI_MCP_LI_BIN`` (explicit override, split on whitespace).
      2. The ``li`` console script next to ``sys.executable`` (same venv/bin),
         invoked by absolute path so it never depends on ``PATH``.
      3. ``<this-interpreter> -m lionagi.cli`` as a last resort.
    """
    override = os.environ.get(LI_BIN_ENV_VAR)
    if override:
        return override.split()

    bin_li = Path(sys.executable).resolve().parent / "li"
    if bin_li.exists():
        return [str(bin_li)]

    return [sys.executable, "-m", "lionagi.cli"]


def run_dir(run_id: str) -> Path:
    """Directory of authoritative CLI state for *run_id*."""
    return RUNS_DIR / run_id


def run_manifest(run_id: str) -> Path:
    """The run.json the CLI writes for *run_id*."""
    return run_dir(run_id) / "run.json"


def job_dir(run_id: str) -> Path:
    """The MCP server's own record directory for *run_id*."""
    return JOBS_DIR / run_id

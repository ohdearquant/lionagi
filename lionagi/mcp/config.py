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

# Stamped into every job's child environment at spawn and read back off live
# processes to confirm they belong to the run that started them. Deliberately
# not RUN_ID_ENV_VAR, which the CLI consumes to pick a run directory and which
# a descendant is free to rewrite for its own sub-run: a kill decision must not
# rest on a variable another subsystem owns and can reassign.
JOB_MARKER_ENV_VAR = "LIONAGI_MCP_JOB_RUN_ID"

# Where a run writes the typed class of the exception that ended it, for the
# terminal hook to lift into the job record (lionagi/mcp/_terminal_cause.py).
# Named per job rather than derived from the run id inside the child: the child
# is free to reassign RUN_ID_ENV_VAR for a sub-run, and a cause file placed by
# that name would land in a directory this server never reads.
CAUSE_FILE_ENV_VAR = "LIONAGI_MCP_CAUSE_FILE"

# Inside the job directory, so it is removed with the job and never outlives it.
CAUSE_FILENAME = "terminal_cause.json"

# Explicit override for the argv prefix that invokes the ``li`` CLI, split on
# whitespace. Rarely needed: the server runs inside lionagi's own environment,
# so the interpreter running it already resolves the CLI (see li_command).
LI_BIN_ENV_VAR = "LIONAGI_MCP_LI_BIN"


def li_command() -> list[str]:
    """Return the argv prefix that invokes the ``li`` CLI.

    Resolution order: 1) ``LIONAGI_MCP_LI_BIN`` override, split on whitespace;
    2) the ``li`` console script next to ``sys.executable``, by absolute path;
    3) ``<this-interpreter> -m lionagi.cli`` as a last resort.
    """
    override = os.environ.get(LI_BIN_ENV_VAR)
    if override:
        return override.split()

    # A venv's bin/python symlinks to the base interpreter, so try the
    # interpreter's own directory before the resolved (base install) one, or
    # this would miss the `li` the venv itself installed.
    interpreter = Path(sys.executable)
    for bindir in (interpreter.parent, interpreter.resolve().parent):
        bin_li = bindir / "li"
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

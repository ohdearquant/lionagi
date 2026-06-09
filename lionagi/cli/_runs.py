# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped file persistence — one invocation of the CLI = one RunDir.

Split between authoritative state (always in ``LIONAGI_HOME/runs/{run_id}/``)
and user-facing artifacts (``--save`` dir if provided, else nested under the
state root). This separation lets you resume any branch from anywhere
(``li agent -r <branch_id>``) while giving users control over where their
deliverables land.

Layout
------

``~/.lionagi/runs/{run_id}/``   — authoritative state (always)

- ``run.json``                    run manifest (command, ts, branches, artifact_root)
- ``branches/{branch_id}.json``   final branch snapshot — canonical location
- ``stream/{branch_id}.buffer.jsonl``  live chunk buffer during a stream
- ``artifacts/``                  only when ``--save`` was NOT provided

``<user --save dir>/``           — user-facing artifacts (optional)

Contains whatever the agents wrote (per-agent cwds, synthesis.md),
plus ``flow.log`` and ``flow_dag.png`` if applicable. This directory
is not authoritative state — deleting it does not break ``-r``.

Legacy layout (``~/.lionagi/logs/agents/{provider}/{branch_id}``) is still
read on resume as a fallback for pre-run-scoped sessions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from lionagi._paths import RUNS_ROOT
from lionagi.libs.path_safety import validate_path_component
from lionagi.utils import LIONAGI_HOME

__all__ = (
    "LIONAGI_HOME",
    "RUNS_ROOT",
    "RunDir",
    "allocate_run",
    "find_branch",
    "load_last_branch",
    "save_last_branch_pointer",
    "list_runs",
    "current_run_id",
)
_LEGACY_AGENTS_ROOT = LIONAGI_HOME / "logs" / "agents"
_LAST_BRANCH_POINTER = LIONAGI_HOME / "last_branch.json"
_RUN_ID_ENV_VAR = "LIONAGI_RUN_ID"


def _new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


def current_run_id() -> str | None:
    """Return the run_id inherited from the environment (subprocess case)."""
    return os.environ.get(_RUN_ID_ENV_VAR) or None


@dataclass(frozen=True, slots=True)
class RunDir:
    """Resolved paths for one run's state and artifacts.

    ``state_root`` is always under ``LIONAGI_HOME/runs/``. ``artifact_root``
    is the user's ``--save`` directory when provided, otherwise a subdir
    of state_root. The two roots can point to the same directory (when
    ``--save`` is nested inside ``LIONAGI_HOME``) or diverge entirely.
    """

    run_id: str
    state_root: Path
    artifact_root: Path

    # ── Path helpers ────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.state_root / "run.json"

    @property
    def branches_dir(self) -> Path:
        return self.state_root / "branches"

    @property
    def stream_dir(self) -> Path:
        return self.state_root / "stream"

    def branch_path(self, branch_id: str) -> Path:
        return self.branches_dir / f"{branch_id}.json"

    def stream_buffer_path(self, branch_id: str) -> Path:
        return self.stream_dir / f"{branch_id}.buffer.jsonl"

    def agent_artifact_dir(self, agent_id: str) -> Path:
        """Return the artifact directory for an agent.

        Authoritative path-containment guard: agent ids become filesystem path
        segments, so reject any with separators, leading dots, or that resolve
        outside the artifact root. Orchestrate ids are CLI-generated from
        roster-validated role names, but any caller constructing a RunDir path
        from an untrusted identifier picks up the same safety guarantee.
        """
        try:
            validate_path_component(agent_id, label="agent_id")
        except ValueError as exc:
            raise ValueError(f"agent_id {agent_id!r} is not a safe path component") from exc
        candidate = (self.artifact_root / agent_id).resolve()
        root = self.artifact_root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"agent_id {agent_id!r} resolves outside artifact_root {root}"
            ) from exc
        return self.artifact_root / agent_id

    @property
    def synthesis_path(self) -> Path:
        return self.artifact_root / "synthesis.md"

    @property
    def flow_log_path(self) -> Path:
        return self.artifact_root / "flow.log"

    @property
    def dag_image_path(self) -> Path:
        return self.artifact_root / "flow_dag.png"

    # ── Manifest I/O ────────────────────────────────────────────────

    def write_manifest(self, data: dict) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "state_root": str(self.state_root),
            "artifact_root": str(self.artifact_root),
            **data,
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2))

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text())

    # ── Directory setup ─────────────────────────────────────────────

    def ensure_state_dirs(self) -> None:
        self.branches_dir.mkdir(parents=True, exist_ok=True)
        self.stream_dir.mkdir(parents=True, exist_ok=True)

    def ensure_artifact_root(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)


def allocate_run(
    save_dir: str | os.PathLike | None = None,
    run_id: str | None = None,
) -> RunDir:
    """Allocate a new run (or attach to an inherited one via env var).

    Parameters
    ----------
    save_dir
        If provided, becomes the ``artifact_root``. Otherwise artifacts land
        under ``state_root/artifacts/``.
    run_id
        Override for the run identifier. When ``None``, tries
        ``LIONAGI_RUN_ID`` env var (subprocess handoff) then generates new.
    """
    rid = run_id or current_run_id() or _new_run_id()
    state_root = RUNS_ROOT / rid

    if save_dir is not None:
        artifact_root = Path(save_dir).expanduser().resolve()
    else:
        artifact_root = state_root / "artifacts"

    run = RunDir(run_id=rid, state_root=state_root, artifact_root=artifact_root)
    run.ensure_state_dirs()
    return run


# ── Branch lookup (canonical + legacy fallback) ─────────────────────────


def find_branch(branch_id: str) -> tuple[str | None, Path]:
    """Locate a branch JSON by id.

    Returns (run_id, path). ``run_id`` is ``None`` when the branch was
    found in legacy ``logs/agents/{provider}/`` storage. Raises
    ``FileNotFoundError`` if no match exists.

    Lookup order:
      1. ``runs/*/branches/{branch_id}.json`` — the canonical location
      2. ``logs/agents/{provider}/{branch_id}`` — legacy fallback
    """
    if RUNS_ROOT.exists():
        # Prefer an exact hit, fall back to prefix match (branch UUIDs may
        # have been truncated by the user when resuming).
        for run_dir in sorted(
            RUNS_ROOT.iterdir(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if not run_dir.is_dir():
                continue
            branches = run_dir / "branches"
            if not branches.exists():
                continue
            exact = branches / f"{branch_id}.json"
            if exact.exists():
                return run_dir.name, exact
            for match in branches.glob(f"{branch_id}*.json"):
                return run_dir.name, match

    if _LEGACY_AGENTS_ROOT.exists():
        for provider_dir in sorted(_LEGACY_AGENTS_ROOT.iterdir()):
            if not provider_dir.is_dir():
                continue
            exact = provider_dir / branch_id
            if exact.exists():
                return None, exact
            for match in provider_dir.glob(f"{branch_id}*"):
                return None, match

    raise FileNotFoundError(f"No branch log found for id {branch_id!r}")


# ── Last-branch pointer (with legacy schema compat) ─────────────────────


def load_last_branch() -> tuple[str | None, str]:
    """Read the last-branch pointer. Returns (run_id, branch_id).

    ``run_id`` is ``None`` when the pointer predates the run-scoped
    refactor (old schema: ``{provider, branch_id}``).
    """
    if not _LAST_BRANCH_POINTER.exists():
        raise FileNotFoundError(
            f"No last-branch pointer at {_LAST_BRANCH_POINTER}. "
            "Run `li agent <model> <prompt>` at least once before using -c."
        )
    data = json.loads(_LAST_BRANCH_POINTER.read_text())
    branch_id = data["branch_id"]
    run_id = data.get("run_id")  # None for legacy pointers
    return run_id, branch_id


def save_last_branch_pointer(run_id: str, branch_id: str) -> None:
    LIONAGI_HOME.mkdir(parents=True, exist_ok=True)
    _LAST_BRANCH_POINTER.write_text(json.dumps({"run_id": run_id, "branch_id": branch_id}))


# ── Introspection ───────────────────────────────────────────────────────


def list_runs(limit: int | None = None) -> list[RunDir]:
    """Return all runs under RUNS_ROOT, newest first (by mtime)."""
    if not RUNS_ROOT.exists():
        return []
    dirs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        dirs = dirs[:limit]
    out: list[RunDir] = []
    for d in dirs:
        manifest_path = d / "run.json"
        artifact_root = d / "artifacts"
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text())
                art = m.get("artifact_root")
                if art:
                    artifact_root = Path(art)
            except (OSError, json.JSONDecodeError):
                pass
        out.append(RunDir(run_id=d.name, state_root=d, artifact_root=artifact_root))
    return out

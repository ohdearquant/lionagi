# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped file layout: authoritative state in LIONAGI_HOME/runs/{run_id}/, artifacts in --save dir or state_root/artifacts/."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lionagi._paths import RUNS_ROOT
from lionagi.libs.path_safety import validate_path_component
from lionagi.ln._utils import now_utc
from lionagi.utils import LIONAGI_HOME

if TYPE_CHECKING:
    from lionagi import Branch
    from lionagi.state.db import StateDB

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
    "resolve_run_reason",
    "setup_agent_persist",
    "teardown_persist",
    "teardown_agent_persist",
    "teardown_orchestration_persist",
)
_LEGACY_AGENTS_ROOT = LIONAGI_HOME / "logs" / "agents"
_LAST_BRANCH_POINTER = LIONAGI_HOME / "last_branch.json"
_RUN_ID_ENV_VAR = "LIONAGI_RUN_ID"


def _new_run_id() -> str:
    ts = now_utc().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


def current_run_id() -> str | None:
    """Return the run_id inherited from the environment (subprocess case)."""
    return os.environ.get(_RUN_ID_ENV_VAR) or None


@dataclass(frozen=True, slots=True)
class RunDir:
    """Resolved state and artifact paths for one CLI run."""

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
        """Return artifact dir for agent_id, rejecting any id that resolves outside artifact_root (path-traversal guard)."""
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
    """Allocate a run dir, inheriting run_id from LIONAGI_RUN_ID env var if set (subprocess handoff)."""
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
    """Locate a branch JSON; returns (run_id, path), run_id=None for legacy logs/agents/ storage."""
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
    """Read the last-branch pointer; returns (run_id, branch_id), run_id=None for pre-run-scoped schema."""
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


# ── Live-persist lifecycle (absorbed from _persist.py) ──────────────────────

_log = logging.getLogger("lionagi.cli")


def resolve_run_reason(
    *,
    status: str,
    exception: BaseException | None,
) -> tuple[str, str, list[dict] | None]:
    from lionagi.state.reasons import RunReasons

    if status == "completed":
        return RunReasons.COMPLETED_OK, "Run completed successfully.", None
    if status == "timed_out":
        return RunReasons.TIMED_OUT_DEADLINE, "Run exceeded the configured timeout.", None
    if status == "aborted":
        return RunReasons.CANCELLED_SIGINT, "User pressed Ctrl-C (SIGINT).", None
    if status == "cancelled":
        return (
            RunReasons.CANCELLED_SYSTEM,
            "Task cancelled by the runtime (anyio CancelledError).",
            None,
        )
    if exception is not None:
        return RunReasons.FAILED_EXCEPTION, f"{type(exception).__name__}: {exception}", None
    return RunReasons.FAILED_EXCEPTION, "Run failed.", None


async def _teardown_common(
    db: StateDB,
    *,
    session_id: str,
    session_prog_id: str,
    status: str,
    exception: BaseException | None,
    artifacts_path: str | None,
    artifact_contract: dict | None,
    extras: dict | None = None,
    identity_markers: dict | None = None,
) -> str:
    from lionagi.state.artifact_verifier import (
        missing_artifact_evidence,
        missing_artifact_summary,
        verify_artifact_contract,
    )

    all_msgs = await db.get_progression(session_prog_id)
    update_kwargs: dict[str, Any] = {"ended_at": time.time()}
    if all_msgs:
        update_kwargs["first_msg_id"] = all_msgs[0]
        update_kwargs["last_msg_id"] = all_msgs[-1]

    if extras:
        markers = identity_markers or {}
        update_kwargs["node_metadata"] = json.dumps({**extras, **markers})

    await db.update_session(session_id, **update_kwargs)

    reason_code, reason_summary, evidence_refs = resolve_run_reason(
        status=status, exception=exception
    )
    metadata: dict | None = None
    if exception is not None:
        metadata = {"exception_class": type(exception).__name__}

    session_row = await db.get_session(session_id) or {}
    contract = artifact_contract or session_row.get("artifact_contract_json")
    artifacts_root = artifacts_path or session_row.get("artifacts_path")
    verification = verify_artifact_contract(contract, artifacts_root=artifacts_root)
    await db.update_artifact_verification(session_id, verification)

    final_status = status
    final_reason_code = reason_code
    final_reason_summary = reason_summary
    final_evidence_refs = evidence_refs

    if verification and verification["status"] == "failed":
        from lionagi.state.reasons import RunReasons

        missing = verification["missing_required"]
        if status == "completed":
            final_status = "failed"
            final_reason_code = RunReasons.FAILED_MISSING_ARTIFACT
            final_reason_summary = missing_artifact_summary(missing)
            final_evidence_refs = missing_artifact_evidence(missing)
        else:
            metadata = dict(metadata or {})
            metadata["artifact_verification_status"] = verification["status"]
            metadata["missing_required_artifact_ids"] = [
                str(entry.get("id", "")) for entry in missing
            ]

    await db.update_status(
        "session",
        session_id,
        new_status=final_status,
        reason_code=final_reason_code,
        reason_summary=final_reason_summary,
        evidence_refs=final_evidence_refs,
        source="executor",
        actor=session_id,
        metadata=metadata,
    )
    return final_status


def _resolve_project(project: str | None) -> tuple[str | None, str | None]:
    if project:
        return project, "explicit"
    from lionagi.cli._project import detect_project

    return detect_project()


async def teardown_persist(
    ctx: dict | None,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
    extras: dict | None = None,
) -> str:
    if ctx is None:
        return status

    db = ctx["db"]
    try:
        final_status = await _teardown_common(
            db,
            session_id=ctx["session_id"],
            session_prog_id=ctx["session_prog_id"],
            status=status,
            exception=exception,
            artifacts_path=ctx.get("artifacts_path"),
            artifact_contract=ctx.get("artifact_contract"),
            extras=extras,
            identity_markers=ctx.get("identity_markers"),
        )

        from lionagi.hooks import unroute_message_persistence
        from lionagi.hooks.bus import HookPoint

        hook = ctx.get("hook")
        if hook is not None:
            unroute_message_persistence(ctx["branch"], hook)
        for branch, h in ctx.get("hooks", []):
            unroute_message_persistence(branch, h)

        session_obj = ctx.get("session")
        if session_obj is not None:
            err_str = str(exception) if exception is not None else None
            await session_obj.hooks.emit(
                HookPoint.SESSION_END,
                session_id=ctx["session_id"],
                status=final_status,
                error=err_str,
            )

        # Detach signal persistence so the observer handler cannot fire after
        # teardown (the db handle is about to be closed in the finally block).
        if session_obj is not None:
            try:
                session_obj.observer.unbind_db_persistence()
            except Exception as _exc:  # noqa: BLE001
                _log.debug("signal persist unbind failed: %s", _exc)

        return final_status
    except Exception as exc:
        _log.warning("live persist teardown failed: %s", exc, exc_info=True)
        return status
    finally:
        try:
            await db.close()
        except Exception as exc:
            _log.warning("live persist db.close failed: %s", exc, exc_info=True)


# Keep old names as aliases so callers don't break.
teardown_agent_persist = teardown_persist
teardown_orchestration_persist = teardown_persist


async def setup_agent_persist(
    branch: Branch,
    *,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
) -> dict | None:
    from lionagi.session.session import Session
    from lionagi.state import provenance as _provenance
    from lionagi.state.db import StateDB

    db: StateDB | None = None
    try:
        db = StateDB()
        await db.open()

        session = Session(name="agent", default_branch=branch)
        session_id = str(session.id)
        branch_id = str(branch.id)

        existing_branch = await db.get_branch(branch_id)
        if existing_branch:
            session_id = existing_branch["session_id"]
            existing_session = await db.get_session(session_id)
            session_prog_id = existing_session["progression_id"]
            branch_prog_id = existing_branch["progression_id"]

            if session_prog_id is None:
                candidate = str(uuid.uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_session_progression(session_id, candidate)
                session_prog_id = effective or candidate
            if branch_prog_id is None:
                candidate = str(uuid.uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_branch_progression(branch_id, candidate)
                branch_prog_id = effective or candidate

            existing_msg_ids = set(await db.get_progression(branch_prog_id))
        else:
            session_prog_id = str(uuid.uuid4())
            branch_prog_id = str(uuid.uuid4())
            existing_msg_ids = set()

            await db.create_progression(session_prog_id)
            await db.create_progression(branch_prog_id)

            session_dict = session.to_dict(mode="db")
            _proj, _proj_src = _resolve_project(project)
            from lionagi.cli.kill import current_pid_markers

            _node_meta = {**(session_dict.get("node_metadata") or {}), **current_pid_markers()}
            await db.create_session(
                {
                    "id": session_id,
                    "created_at": session_dict["created_at"],
                    "node_metadata": _node_meta,
                    "name": session_dict.get("name"),
                    "user": session_dict.get("user"),
                    "progression_id": session_prog_id,
                    "first_msg_id": None,
                    "last_msg_id": None,
                    "invocation_kind": "agent",
                    "agent_name": agent_name,
                    "artifacts_path": artifacts_path,
                    "artifact_contract_json": artifact_contract,
                    "status": "running",
                    "started_at": time.time(),
                    "invocation_id": invocation_id,
                    "model": model,
                    "provider": provider,
                    "effort": effort,
                    "agent_hash": _provenance.agent_definition_hash(agent_name),
                    "project": _proj,
                    "project_source": _proj_src,
                }
            )

            system_msg_id = None
            if branch.system:
                sys_dict = branch.system.to_dict(mode="db")
                system_msg_id = sys_dict["id"]
                await db.insert_message(sys_dict)

            branch_dict = branch.to_dict(mode="db")
            node_meta = branch_dict.get("node_metadata") or {}
            if isinstance(node_meta, str):
                node_meta = json.loads(node_meta)
            if "chat_model" in branch_dict:
                node_meta["chat_model"] = branch_dict["chat_model"]

            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": branch_dict["created_at"],
                    "node_metadata": node_meta,
                    "user": branch_dict.get("user"),
                    "name": branch_dict.get("name"),
                    "session_id": session_id,
                    "progression_id": branch_prog_id,
                    "system_msg_id": system_msg_id,
                    "model": model,
                    "provider": provider,
                    "agent_name": agent_name,
                }
            )

            from lionagi.hooks.bus import HookPoint

            await session.hooks.emit(
                HookPoint.SESSION_START,
                session_id=session_id,
                model=model,
                provider=provider,
                effort=effort,
                agent_name=agent_name,
                agent_hash=_provenance.agent_definition_hash(agent_name),
                invocation_id=invocation_id,
            )
            await session.hooks.emit(
                HookPoint.BRANCH_CREATE,
                branch_id=branch_id,
                model=model,
                provider=provider,
                agent_name=agent_name,
            )

        ctx = {
            "db": db,
            "session": session,
            "branch": branch,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_id": branch_prog_id,
            "existing_msg_ids": existing_msg_ids,
            "new_msg_ids": [],
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
        }

        async def _on_message(msg):
            try:
                msg_dict = msg.to_dict(mode="db")
                msg_id = msg_dict["id"]
                await db.insert_message(msg_dict)
                if msg_id not in ctx["existing_msg_ids"]:
                    await db.append_to_progression(branch_prog_id, msg_id)
                    await db.append_to_progression(session_prog_id, msg_id)
                    ctx["new_msg_ids"].append(msg_id)
                await db.touch_session_activity(session_id, at=msg_dict.get("created_at"))
                if msg_dict.get("role") == "system":
                    await db.update_branch(branch_id, system_msg_id=msg_id)
            except Exception as exc:
                _log.warning(
                    "live persist write failed for branch %s: %s",
                    branch_id,
                    exc,
                    exc_info=True,
                )

        # Bind signal persistence through the already-open DB so every Signal
        # emitted on this session's observer lands in session_signals without
        # opening a new connection per signal (matches message-write cost).
        session.observer.bind_db_persistence(session_id, db=db)

        from lionagi.hooks import route_message_persistence

        ctx["hook"] = route_message_persistence(session, branch, _on_message)
        return ctx
    except Exception as exc:
        _log.warning(
            "live persist setup failed (%s) — disabling persistence for this run",
            exc,
            exc_info=True,
        )
        if db is not None:
            try:
                await db.close()
            except Exception as close_exc:
                _log.warning("fallback db.close after setup failure also failed: %s", close_exc)
        return None

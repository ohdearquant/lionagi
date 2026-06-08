# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared persistence lifecycle for agent and orchestration CLI paths."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lionagi import Branch
    from lionagi.state.db import StateDB

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


# ---------------------------------------------------------------------------
# Single-branch path (agent.py)
# ---------------------------------------------------------------------------


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
    from lionagi.state.db import StateDB

    db: StateDB | None = None
    try:
        db = StateDB()
        await db.open()

        from lionagi.state import provenance as _provenance

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
            if project:
                _proj, _proj_src = project, "explicit"
            else:
                from lionagi.cli._project import detect_project

                _proj, _proj_src = detect_project()
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

        ctx = {
            "db": db,
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


async def teardown_agent_persist(
    ctx: dict | None,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
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
        )

        from lionagi.hooks import unroute_message_persistence

        unroute_message_persistence(ctx["branch"], ctx["hook"])
        return final_status
    except Exception as exc:
        _log.warning("live persist teardown failed: %s", exc, exc_info=True)
        return status
    finally:
        try:
            await db.close()
        except Exception as exc:
            _log.warning("live persist db.close failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Multi-branch path (orchestration)
# ---------------------------------------------------------------------------


async def setup_orchestration_persist(
    session: Any,
    *,
    invocation_kind: str | None = None,
    playbook_name: str | None = None,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
    branches: list[Branch] | None = None,
) -> dict | None:
    from lionagi.state import provenance as _provenance
    from lionagi.state.db import StateDB

    db: StateDB | None = None
    try:
        db = StateDB()
        await db.open()

        session_id = str(session.id)
        session_dict = session.to_dict(mode="db")

        session_prog_id = str(uuid.uuid4())
        await db.create_progression(session_prog_id)

        if project:
            _proj, _proj_src = project, "explicit"
        else:
            from lionagi.cli._project import detect_project

            _proj, _proj_src = detect_project()

        from lionagi.cli.kill import current_pid_markers

        _identity_markers = current_pid_markers()
        _node_meta = {**(session_dict.get("node_metadata") or {}), **_identity_markers}
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
                "invocation_kind": invocation_kind,
                "playbook_name": playbook_name,
                "agent_name": agent_name,
                "artifacts_path": artifacts_path,
                "artifact_contract_json": artifact_contract,
                "status": "running",
                "started_at": time.time(),
                "invocation_id": invocation_id,
                "model": _provenance.resolve_model_spec(provider, model),
                "provider": provider,
                "effort": effort,
                "agent_hash": _provenance.agent_definition_hash(agent_name),
                "project": _proj,
                "project_source": _proj_src,
            }
        )

        ctx: dict[str, Any] = {
            "db": db,
            "session": session,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_ids": {},
            "hooks": [],
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
            "identity_markers": _identity_markers,
        }

        for branch in branches or []:
            register_branch_hook(ctx, branch)

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


def register_branch_hook(ctx: dict[str, Any], branch: Branch) -> None:
    from lionagi.ln.concurrency import Lock

    db = ctx["db"]
    session_id = ctx["session_id"]
    session_prog_id = ctx["session_prog_id"]
    branch_id = str(branch.id)

    branch_prog_id = str(uuid.uuid4())
    ctx["branch_prog_ids"][branch_id] = branch_prog_id
    initialized = {"done": False}
    init_lock = Lock()

    async def _ensure_branch_row():
        if initialized["done"]:
            return
        async with init_lock:
            if initialized["done"]:
                return

            await db.create_progression(branch_prog_id)

            branch_dict = branch.to_dict(mode="db")
            node_meta = branch_dict.get("node_metadata") or {}
            if isinstance(node_meta, str):
                node_meta = json.loads(node_meta)
            if "chat_model" in branch_dict:
                node_meta["chat_model"] = branch_dict["chat_model"]
            node_meta = json.loads(json.dumps(node_meta, default=str))

            system_msg_id = None
            if branch.system:
                sys_dict = branch.system.to_dict(mode="db")
                system_msg_id = sys_dict["id"]
                await db.insert_message(sys_dict)

            br_model: str | None = None
            br_provider: str | None = None
            try:
                from lionagi.state import provenance as _provenance

                ep_cfg = branch.chat_model.endpoint.config
                br_provider = getattr(ep_cfg, "provider", None)
                br_model_raw = (ep_cfg.kwargs or {}).get("model")
                br_model = _provenance.resolve_model_spec(br_provider, br_model_raw)
            except Exception as _provenance_exc:
                _log.debug("branch provenance lookup failed for %s: %s", branch_id, _provenance_exc)

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
                    "model": br_model,
                    "provider": br_provider,
                    "agent_name": branch_dict.get("name"),
                }
            )
            initialized["done"] = True

    async def _on_message(msg):
        try:
            await _ensure_branch_row()
            msg_dict = msg.to_dict(mode="db")
            msg_id = msg_dict["id"]
            await db.insert_message(msg_dict)
            await db.append_to_progression(branch_prog_id, msg_id)
            await db.append_to_progression(session_prog_id, msg_id)
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

    from lionagi.hooks import route_message_persistence

    handler = route_message_persistence(ctx["session"], branch, _on_message)
    ctx["hooks"].append((branch, handler))


async def teardown_orchestration_persist(
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

        for branch, hook in ctx["hooks"]:
            unroute_message_persistence(branch, hook)
        return final_status
    except Exception as exc:
        _log.warning("live persist teardown failed: %s", exc, exc_info=True)
        return status
    finally:
        try:
            await db.close()
        except Exception as exc:
            _log.warning("live persist db.close failed: %s", exc, exc_info=True)

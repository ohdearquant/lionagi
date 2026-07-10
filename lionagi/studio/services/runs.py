from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query

from ..registry import studio_route
from . import sessions as _sessions_svc
from ._path_safety import public_path

_STATUS_ALIASES: dict[str, set[str]] = {
    "done": {"done", "completed", "success", "finished"},
    "cancelled": {"cancelled", "canceled"},
    "canceled": {"cancelled", "canceled"},
    "aborted": {"aborted", "aborted_after_finish"},
    "timed_out": {"timed_out", "timeout"},
    "timeout": {"timed_out", "timeout"},
    "pending": {"pending", "prepared"},
}


def _normalize_status_filter(status: str | list[str] | None) -> set[str] | None:
    if status is None:
        return None
    if isinstance(status, str):
        status = [status]
    result: set[str] = set()
    for s in status:
        result |= _STATUS_ALIASES.get(s, {s})
    return result or None


def _adapt_summary(
    manifest: dict[str, Any], run_id: str, state_root: Path, artifact_root: Path
) -> dict[str, Any]:
    branches_dir = state_root / "branches"
    step_count = 0
    if branches_dir.exists():
        try:
            step_count = len(list(branches_dir.glob("*.json")))
        except OSError:
            step_count = 0
    if not step_count and isinstance(manifest.get("steps"), list):
        step_count = len(manifest["steps"])

    worker_name = (
        manifest.get("worker_name") or manifest.get("worker") or manifest.get("kind") or ""
    )

    task = str(manifest.get("task") or manifest.get("prompt") or "")

    raw_status = manifest.get("status")
    started_at = manifest.get("started_at")
    finished_at = manifest.get("finished_at")

    if raw_status:
        status = str(raw_status)
    else:
        branches_exist = branches_dir.exists() and any(branches_dir.glob("*.json"))
        stream_dir = state_root / "stream"
        has_buffers = stream_dir.exists() and any(stream_dir.glob("*.buffer.jsonl"))
        if manifest.get("error"):
            status = "failed"
        elif branches_exist:
            status = "completed"
            if not finished_at:
                try:
                    latest = max(branches_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
                    finished_at = latest.stat().st_mtime
                except (OSError, ValueError):
                    pass
        elif has_buffers:
            status = "running"
        else:
            status = "pending"
    if not started_at and state_root.exists():
        try:
            started_at = state_root.stat().st_birthtime
        except AttributeError:
            started_at = state_root.stat().st_mtime

    return {
        "run_id": run_id,
        "state_root": public_path(state_root),
        "artifact_root": public_path(artifact_root),
        "worker_name": str(worker_name),
        "task": task,
        "status": status,
        "step_count": step_count,
        "started_at": started_at,
        "finished_at": finished_at,
        "model": str(manifest.get("model_spec") or manifest.get("model") or ""),
    }


def _build_graph(manifest: dict[str, Any]) -> dict[str, Any]:
    graph_raw = manifest.get("graph")
    if graph_raw and (graph_raw.get("nodes") or graph_raw.get("edges")):
        return {
            "nodes": graph_raw.get("nodes") or [],
            "edges": graph_raw.get("edges") or [],
        }

    agents = manifest.get("agents") or []
    operations = manifest.get("operations") or []
    if not operations:
        kind = manifest.get("kind", "")
        if kind:
            return {
                "nodes": [
                    {
                        "id": kind,
                        "label": kind,
                        "role": manifest.get("provider", ""),
                        "assignment": manifest.get("model_spec") or manifest.get("model") or "",
                        "prompt": "",
                        "capacity": 1,
                        "timeout": None,
                        "inputs": [],
                        "outputs": [],
                    }
                ],
                "edges": [],
            }
        return {"nodes": [], "edges": []}

    agent_map = {a["id"]: a for a in agents if isinstance(a, dict) and "id" in a}

    nodes = []
    edges = []
    for op in operations:
        if not isinstance(op, dict) or "id" not in op:
            continue
        agent_id = op.get("agent_id", "")
        agent = agent_map.get(agent_id, {})
        nodes.append(
            {
                "id": op["id"],
                "label": op["id"],
                "role": agent.get("name", ""),
                "assignment": agent.get("model", ""),
                "prompt": "",
                "capacity": 1,
                "timeout": None,
                "inputs": op.get("depends_on", []),
                "outputs": [],
            }
        )
        for dep in op.get("depends_on", []):
            edges.append(
                {
                    "id": f"e-{dep}-{op['id']}",
                    "source": dep,
                    "target": op["id"],
                    "mode": "simple",
                }
            )

    return {"nodes": nodes, "edges": edges}


def _summarize_args(fn: str, args: dict[str, Any]) -> str:
    if not isinstance(args, dict):
        return str(args)[:200]
    for key in ("cmd", "command", "file_path", "pattern", "url", "query"):
        val = args.get(key)
        if val:
            return str(val)
    if fn in ("apply_patch", "Edit", "Write"):
        path = args.get("path") or args.get("file_path") or ""
        return str(path) if path else "(patch)"
    for k, v in args.items():
        if isinstance(v, str | int | float) and v:
            return f"{k}={v}"
    return ""


def _detect_status(output: str, function: str) -> tuple[str, int | None]:
    if not output:
        return ("ok", None)
    lower = output.lower()
    exit_code: int | None = None
    for line in output.splitlines()[:8]:
        if "process exited with code" in line.lower():
            try:
                exit_code = int(line.rsplit(maxsplit=1)[-1].rstrip("."))
            except (ValueError, IndexError):
                pass
            break
        if line.lower().startswith("exit code:"):
            try:
                exit_code = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
            break
    if exit_code is not None and exit_code != 0:
        return ("error", exit_code)
    if any(kw in lower[:300] for kw in ("error:", "failed", "permission denied", "not found")):
        if "no such file or directory" in lower:
            return ("error", exit_code)
    return ("ok", exit_code)


def _extract_messages(branch: dict[str, Any]) -> list[dict[str, Any]]:
    msgs_raw = branch.get("messages", {})
    if isinstance(msgs_raw, list):
        collections = msgs_raw
    elif isinstance(msgs_raw, dict):
        collections = msgs_raw.get("collections", [])
    else:
        return []

    response_by_id: dict[str, dict[str, Any]] = {}
    for item in collections:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata", {}) or {}
        if "ActionResponse" not in str(meta.get("lion_class", "")):
            continue
        rid = item.get("id", "")
        if rid:
            response_by_id[rid] = item

    out: list[dict[str, Any]] = []
    skip_ids: set[str] = set()

    for item in collections:
        if not isinstance(item, dict):
            continue
        if item.get("id") in skip_ids:
            continue
        role = item.get("role", "")
        content = item.get("content", "")
        sender = item.get("sender") or ""
        ts = item.get("created_at")
        meta = item.get("metadata", {}) or {}
        cls = str(meta.get("lion_class", "")).split(".")[-1]

        if cls == "ActionResponse":
            # Surface only via ActionRequest pairing; orphan responses fall
            # through as bare tool_result entries.
            if any(
                isinstance(other, dict)
                and other.get("content", {}).get("action_response_id") == item.get("id")
                for other in collections
            ):
                continue

        if role == "system":
            text = content.get("system_message", "") if isinstance(content, dict) else str(content)
            if text:
                out.append(
                    {
                        "role": "system",
                        "content": text,
                        "sender": sender[:8],
                        "timestamp": ts,
                    }
                )
            continue

        if role == "user":
            if isinstance(content, dict):
                text = content.get("instruction") or ""
                guidance = content.get("guidance") or ""
                if guidance and text:
                    text = f"{text}\n\n[guidance] {guidance}"
                elif guidance:
                    text = guidance
            else:
                text = str(content)
            if text:
                out.append(
                    {
                        "role": "user",
                        "content": text,
                        "sender": sender[:8],
                        "timestamp": ts,
                    }
                )
            continue

        if role == "assistant":
            text = (
                content.get("assistant_response", "") if isinstance(content, dict) else str(content)
            )
            if text:
                out.append(
                    {
                        "role": "assistant",
                        "content": text,
                        "sender": sender[:8],
                        "timestamp": ts,
                    }
                )
            continue

        if role == "action" and cls == "ActionRequest":
            args = content.get("arguments", {}) if isinstance(content, dict) else {}
            fn = content.get("function", "") if isinstance(content, dict) else ""
            response_id = content.get("action_response_id") if isinstance(content, dict) else None
            response_msg = response_by_id.get(response_id, {}) if response_id else {}
            response_content = (
                response_msg.get("content", {}) if isinstance(response_msg, dict) else {}
            )
            output_text = ""
            if isinstance(response_content, dict):
                output_text = str(response_content.get("output", ""))
            if response_msg:
                skip_ids.add(response_msg.get("id", ""))

            status, exit_code = _detect_status(output_text, fn)
            summary = _summarize_args(fn, args if isinstance(args, dict) else {})

            out.append(
                {
                    "role": "tool_call",
                    "function": fn,
                    "summary": summary,
                    "arguments": args if isinstance(args, dict) else {},
                    "output": output_text,
                    "status": status,
                    "exit_code": exit_code,
                    "sender": sender[:8],
                    "timestamp": ts,
                }
            )
            continue

        if role == "action":
            args = content.get("arguments", {}) if isinstance(content, dict) else {}
            fn = content.get("function", "") if isinstance(content, dict) else ""
            output_text = content.get("output", "") if isinstance(content, dict) else ""
            status, exit_code = _detect_status(str(output_text), fn)
            out.append(
                {
                    "role": "tool_call",
                    "function": fn,
                    "summary": _summarize_args(fn, args if isinstance(args, dict) else {}),
                    "arguments": args if isinstance(args, dict) else {},
                    "output": str(output_text),
                    "status": status,
                    "exit_code": exit_code,
                    "sender": sender[:8],
                    "timestamp": ts,
                }
            )

    return out


def _build_steps(
    manifest: dict[str, Any], branches: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    operations = manifest.get("operations") or []
    agents = manifest.get("agents") or []

    if not operations:
        if not branches:
            return None
        steps = []
        for b in branches:
            if not isinstance(b, dict):
                continue
            name = b.get("name") or manifest.get("kind", "agent")
            messages = _extract_messages(b)
            role_counts: dict[str, int] = {}
            for m in messages:
                r = m["role"]
                role_counts[r] = role_counts.get(r, 0) + 1
            steps.append(
                {
                    "step": name,
                    "status": "completed" if messages else "pending",
                    "result": {
                        "agent": name,
                        "model": manifest.get("model_spec") or manifest.get("model") or "",
                        "message_count": len(messages),
                        "roles": role_counts,
                    },
                    "messages": messages,
                    "timestamp": None,
                }
            )
        return steps if steps else None

    agent_map = {a["id"]: a for a in agents if isinstance(a, dict) and "id" in a}
    branch_by_name: dict[str, dict[str, Any]] = {}
    for b in branches:
        if isinstance(b, dict) and b.get("name"):
            branch_by_name[b["name"]] = b

    steps = []
    for op in operations:
        if not isinstance(op, dict) or "id" not in op:
            continue
        agent_id = op.get("agent_id", "")
        agent = agent_map.get(agent_id, {})
        agent_name = agent.get("name", "")
        branch = branch_by_name.get(agent_name, {})
        has_branch = bool(branch)

        messages = _extract_messages(branch) if has_branch else []
        role_counts: dict[str, int] = {}
        for m in messages:
            r = m["role"]
            role_counts[r] = role_counts.get(r, 0) + 1

        steps.append(
            {
                "step": op["id"],
                "status": "completed" if has_branch else "pending",
                "result": (
                    {
                        "agent": agent_name,
                        "model": agent.get("model", ""),
                        "message_count": len(messages),
                        "roles": role_counts,
                    }
                    if has_branch
                    else None
                ),
                "messages": messages if has_branch else [],
                "timestamp": None,
            }
        )

    seen_agents: set[str] = set()
    for step in steps:
        agent = (step.get("result") or {}).get("agent", "")
        if agent in seen_agents and step.get("messages"):
            step["shared_branch"] = True
        if agent:
            seen_agents.add(agent)

    return steps if steps else None


def _session_liveness(s: dict[str, Any], ps_snapshot: str | None = None) -> bool | None:
    """Tri-state liveness for a running session row, via the shared admin oracle."""
    from .admin import process_liveness

    ap = s.get("artifacts_path")
    return process_liveness(s, Path(ap) if ap else None, ps_snapshot)


def _run_row(s: dict[str, Any], now: float, *, process_alive: bool | None = None) -> dict[str, Any]:
    """The canonical Run row shape. Shared by the list and detail routes so the
    two can never drift out of contract (the detail route used to drop fields the
    list route emits, e.g. invocation_id). Caller supplies a session dict carrying
    branch_count / message_count / last_message_at, plus tri-state process
    liveness (None = unknown) so a process-dead run cannot render as healthy.
    """
    from lionagi.state.health import classify_session_health

    health = classify_session_health(
        s,
        now=now,
        process_alive=process_alive,
        has_artifacts=bool(s.get("artifacts_path")),
        has_stale_locks=False,
    )
    # Expose the classifier verdict verbatim; the dashboard maps UNRESPONSIVE→"stuck".
    effective_health = health.value
    return {
        "run_id": s["id"],
        "id": s["id"],
        "name": s.get("name"),
        "playbook_name": s.get("playbook_name"),
        "agent_name": s.get("agent_name"),
        "invocation_kind": s.get("invocation_kind"),
        "show_topic": s.get("show_topic"),
        "show_play_name": s.get("show_play_name"),
        "source_kind": s.get("source_kind", "live"),
        "artifact_contract_json": s.get("artifact_contract_json"),
        "artifact_verification_json": s.get("artifact_verification_json"),
        "invocation_id": s.get("invocation_id"),
        "model": s.get("model"),
        "provider": s.get("provider"),
        "effort": s.get("effort"),
        "agent_hash": s.get("agent_hash"),
        "status": s.get("status", "completed"),
        "started_at": s.get("started_at"),
        "ended_at": s.get("ended_at"),
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "last_message_at": s.get("last_message_at"),
        "effective_health": effective_health,
        "branch_count": s.get("branch_count", 0),
        "message_count": s.get("message_count", 0),
        "project": s.get("project"),
        "project_source": s.get("project_source"),
        "status_reason_code": s.get("status_reason_code"),
        "status_reason_summary": s.get("status_reason_summary"),
        "tags": [],
    }


async def list_runs(
    playbook: str | None = None,
    status: str | list[str] | None = None,
    project: str | None = None,
    project_null: bool = False,
    tag: list[str] | None = None,
) -> list[dict[str, Any]]:
    from . import run_tags

    sessions = await _sessions_svc.list_sessions()
    status_set = _normalize_status_filter(status)
    tagged = await run_tags.session_ids_with_tags(tag) if (tag and sessions) else None
    now = time.time()
    out = []
    snapshot: str | None = None
    for s in sessions:
        if playbook and playbook.lower() not in (s.get("playbook_name") or "").lower():
            continue
        if project_null:
            if s.get("project") is not None:
                continue
        elif project and s.get("project") != project:
            continue
        if status_set and s.get("status") not in status_set:
            continue
        if tagged is not None and s["id"] not in tagged:
            continue
        alive: bool | None = None
        if s.get("status") == "running":
            if snapshot is None:
                from .admin import _ps_snapshot

                snapshot = _ps_snapshot()
            alive = _session_liveness(s, snapshot)
        out.append(_run_row(s, now, process_alive=alive))

    tagmap = await run_tags.tags_for_sessions([r["id"] for r in out])
    for r in out:
        r["tags"] = tagmap.get(r["id"], [])
    return out


def paginate_runs(
    runs: list[dict[str, Any]],
    *,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    total = len(runs)
    total_pages = math.ceil(total / per_page) if total else 0
    start = (page - 1) * per_page
    page_runs = runs[start : start + per_page]
    return {
        "runs": page_runs,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


async def get_run(
    run_id: str,
    *,
    message_limit: int = _sessions_svc.DEFAULT_MESSAGE_LIMIT,
    message_cursor: str | None = None,
) -> dict[str, Any] | None:
    """Return run detail from StateDB; flat-file run.json path was removed (write_manifest had zero callers).

    The response is a superset of the list Run row (via _run_row) plus detail-only
    fields. get_session() omits the JOIN aggregates (branch_count / message_count),
    so they are derived from the hydrated branches here; last_message_at is read
    straight from get_session()'s session-table aggregate.
    Fields absent from DB (state_root, artifact_root, task, error, cwd, manifest)
    return None/""/{} to keep the frontend contract unchanged.
    """
    session = await _sessions_svc.get_session(
        run_id, message_limit=message_limit, message_cursor=message_cursor
    )
    if session is None:
        return None

    artifacts_path = session.get("artifacts_path")
    artifact_root: Path | None = Path(artifacts_path) if artifacts_path else None

    branches: list[dict[str, Any]] = session.get("branches") or []
    step_count = len(branches)

    state_root: Path | None = artifact_root.parent if artifact_root else None

    # message_stats is computed over the full session progression (never the
    # tail-windowed branches[].messages page), so the run-level count is
    # correct regardless of the display window. Fall back to the windowed
    # page length only for legacy payloads that predate message_stats.
    message_stats = (
        session.get("message_stats") if isinstance(session.get("message_stats"), dict) else None
    )
    if message_stats is not None:
        message_count = message_stats.get("message_count", 0)
    else:
        message_count = sum(
            b.get("message_total") or len(b.get("messages") or [])
            for b in branches
            if isinstance(b, dict)
        )
    # session["last_message_at"] is the DB-maintained full-session aggregate
    # (bumped on every message write, see state/db.py); prefer it over
    # recomputing from branches[].messages, which is only the display window
    # and reports the wrong value once the caller pages to an older cursor.
    last_message_at = session.get("last_message_at")
    if last_message_at is None:
        last_message_at = max(
            (
                m.get("timestamp")
                for b in branches
                if isinstance(b, dict)
                for m in (b.get("messages") or [])
                if isinstance(m, dict) and m.get("timestamp") is not None
            ),
            default=None,
        )

    detail_session = {
        **session,
        "branch_count": len(branches),
        "message_count": message_count,
        "last_message_at": last_message_at,
    }
    alive = _session_liveness(detail_session) if detail_session.get("status") == "running" else None
    row = _run_row(detail_session, time.time(), process_alive=alive)

    from . import run_tags

    tagmap = await run_tags.tags_for_sessions([run_id])
    row["tags"] = tagmap.get(run_id, [])

    return {
        **row,
        # Detail-only fields layered on top of the shared Run row.
        "state_root": public_path(state_root) if state_root else None,
        "artifact_root": public_path(artifact_root) if artifact_root else None,
        "worker_name": session.get("agent_name") or session.get("playbook_name") or "",
        "task": "",
        "step_count": step_count,
        "finished_at": session.get("ended_at"),
        "error": None,
        "cwd": None,
        "steps": _build_steps_from_db(branches),
        "graph": session.get("graph"),
        "manifest": {},
        "branches": branches,
        "message_limit": session.get("message_limit"),
        "message_cursor": session.get("message_cursor"),
        "message_next_cursor": session.get("message_next_cursor"),
        "message_stats": message_stats,
        # Failure-reason contract consumed by the run-detail panel's banner.
        "status_reason_code": session.get("status_reason_code"),
        "status_reason_summary": session.get("status_reason_summary"),
        "status_evidence_refs": session.get("status_evidence_refs"),
    }


def _build_steps_from_db(branches: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Build a steps list from DB-hydrated branch dicts.

    `messages` on each branch is a tail-windowed display page; `message_count`
    and `roles` must reflect the full branch progression, so they are read
    from the branch's full-session `message_stats` (falling back to
    `message_total` and finally the windowed page length for legacy payloads
    that predate message_stats).

    The `message_stats.message_count` fallback is KEY-PRESENCE based, not
    truthiness based: a stale progression referencing pruned/never-persisted
    message ids legitimately aggregates to 0, and `0 or fallback` would
    silently replace that correct zero with the (wrong) progression length.
    """
    if not branches:
        return None
    steps = []
    for b in branches:
        if not isinstance(b, dict):
            continue
        name = b.get("name") or b.get("agent_name") or "agent"
        messages = b.get("messages") or []
        branch_stats = b.get("message_stats") if isinstance(b.get("message_stats"), dict) else {}
        role_counts = dict(branch_stats.get("roles") or {})
        if "message_count" in branch_stats:
            message_count = branch_stats["message_count"]
        else:
            message_count = b.get("message_total") or len(messages)
        message_count = int(message_count)
        steps.append(
            {
                "step": name,
                "status": "completed" if message_count else "pending",
                "result": {
                    "agent": name,
                    "model": b.get("model") or "",
                    "message_count": message_count,
                    "roles": role_counts,
                },
                "messages": messages,
                "timestamp": b.get("started_at"),
            }
        )
    return steps if steps else None


@studio_route("/runs/", method="GET", area="runs", name="list_runs")
async def list_runs_route(
    page: int = Query(default=1, ge=1, description="1-based page number"),
    per_page: int = Query(default=20, ge=1, le=5000, description="Rows per page"),
    status: list[str] | None = Query(default=None, description="Repeated status filter"),  # noqa: B008
    # ADR-0005: renamed from ?worker= to ?playbook= — "worker" is
    # not in lionagi's Studio vocabulary per ADR-0005.
    playbook: str | None = Query(
        default=None, description="Case-insensitive playbook contains filter"
    ),
    project: str | None = Query(default=None, description="Exact project name filter (ADR-0026)"),
    project_null: bool = Query(default=False, description="Filter to runs with no project"),
    tag: list[str] | None = Query(  # noqa: B008
        default=None, description="Repeated tag filter (AND-composed)"
    ),
) -> dict[str, Any]:
    runs = await list_runs(
        playbook=playbook,
        status=status,
        project=project,
        project_null=project_null,
        tag=tag,
    )
    return paginate_runs(runs, page=page, per_page=per_page)


# Registered before /runs/{run_id} so the literal path is not captured as a run id.
@studio_route("/runs/projects", method="GET", area="runs", name="list_run_projects")
async def list_run_projects_route() -> dict[str, Any]:
    counts = await _sessions_svc.list_project_counts()
    counts.sort(key=lambda c: c.get("last_activity") or 0, reverse=True)
    total = sum(c["count"] for c in counts)
    return {"projects": counts, "total": total}


@studio_route("/runs/{run_id}", method="GET", area="runs", name="get_run")
async def get_run_route(
    run_id: str,
    message_limit: int = Query(default=_sessions_svc.DEFAULT_MESSAGE_LIMIT, ge=1, le=1000),
    message_cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    # get_run reads from StateDB (same source as list_runs); no thread offload needed.
    try:
        run = await get_run(run_id, message_limit=message_limit, message_cursor=message_cursor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


# ADR-0008: /api/runs/{id}/events SSE (read stream/*.buffer.jsonl, forbidden
# by ADR-0004) and the rerun/delete stub routes were removed — run data is
# read-only per ADR-0008. Live monitoring: /api/sessions/{id}/stream;
# re-running: the terminal (`li play ...`). Restoring either requires an
# ADR-0008 amendment.

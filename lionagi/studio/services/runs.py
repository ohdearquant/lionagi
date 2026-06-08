from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from lionagi.cli._runs import RUNS_ROOT

from . import sessions as _sessions_svc
from ._io import read_json_file as _read_json_file
from ._path_safety import public_path, safe_path_join

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _adapt_detail(
    run_id: str,
    state_root: Path,
    artifact_root: Path,
    manifest: dict[str, Any],
    branches: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _adapt_summary(manifest, run_id, state_root, artifact_root)
    graph = _build_graph(manifest)
    steps = manifest.get("steps")
    if not steps:
        steps = _build_steps(manifest, branches)

    return {
        **summary,
        "error": manifest.get("error") or None,
        "cwd": manifest.get("cwd") or None,
        "steps": steps,
        "graph": graph,
        "manifest": manifest,
        "branches": branches,
        "artifact_contract_json": manifest.get("artifact_contract_json")
        or manifest.get("artifact_contract"),
        "artifact_verification_json": manifest.get("artifact_verification_json")
        or manifest.get("artifact_verification"),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_runs(
    playbook: str | None = None,
    status: str | list[str] | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    from lionagi.state.health import SessionHealth, classify_session_health

    sessions = await _sessions_svc.list_sessions()
    status_set = _normalize_status_filter(status)
    now = time.time()
    out = []
    for s in sessions:
        if playbook and playbook.lower() not in (s.get("playbook_name") or "").lower():
            continue
        if project and s.get("project") != project:
            continue
        if status_set and s.get("status") not in status_set:
            continue
        health = classify_session_health(
            s,
            now=now,
            process_alive=True,
            has_artifacts=bool(s.get("artifacts_path")),
            has_stale_locks=False,
        )
        # Map UNRESPONSIVE -> 'stale' for dashboard consistency.
        effective_health = (
            SessionHealth.STALE.value if health == SessionHealth.UNRESPONSIVE else health.value
        )
        out.append(
            {
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
            }
        )
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


def get_run(run_id: str) -> dict[str, Any] | None:
    if not RUNS_ROOT.exists():
        return None

    safe_path_join(RUNS_ROOT, run_id)

    state_root = RUNS_ROOT / run_id
    if not state_root.is_dir():
        matches = [d for d in RUNS_ROOT.iterdir() if d.is_dir() and d.name.startswith(run_id)]
        if not matches:
            return None
        state_root = sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        run_id = state_root.name

    manifest_path = state_root / "run.json"
    artifact_root = state_root / "artifacts"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        loaded = _read_json_file(manifest_path)
        if loaded is not None:
            manifest = loaded
            art = manifest.get("artifact_root")
            if art:
                artifact_root = Path(art)

    branches: list[dict[str, Any]] = []
    branches_dir = state_root / "branches"
    if branches_dir.exists():
        for bf in sorted(
            branches_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            loaded = _read_json_file(bf)
            if loaded is not None:
                branches.append(loaded)

    return _adapt_detail(run_id, state_root, artifact_root, manifest, branches)

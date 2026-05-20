"""Migrate historical codex run branches to include tool-call messages.

Codex runs prior to the streaming fix in `stream_codex_cli` persisted only
system/user/assistant messages. The tool call history exists in the codex
CLI's session rollout at ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``.

This script:
  1. Finds every lionagi codex run with a branch that has < 4 messages.
  2. Matches the run to a codex rollout file by UTC->local timestamp.
  3. Parses function_call / function_call_output / custom_tool_call events
     from the rollout.
  4. Reconstructs ActionRequest + ActionResponse messages in lionagi's
     branch format (Pile + Progression).
  5. Writes the augmented branch JSON back atomically.

Idempotent: skips branches that already have action messages.

Run with: uv run python scripts/migrate_codex_runs.py
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LIONAGI_HOME = Path("~/.lionagi").expanduser()
CODEX_SESSIONS_ROOT = Path("~/.codex/sessions").expanduser()


def find_rollout(run_id: str) -> Path | None:
    if not CODEX_SESSIONS_ROOT.exists():
        return None
    try:
        date_str, _ = run_id.split("-", 1)
        utc_dt = datetime.strptime(date_str, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None
    local_dt = utc_dt.astimezone()
    day_dir = CODEX_SESSIONS_ROOT / f"{local_dt.year:04d}" / f"{local_dt.month:02d}" / f"{local_dt.day:02d}"
    if not day_dir.exists():
        return None
    prefix = local_dt.strftime("rollout-%Y-%m-%dT%H-%M-%S-")
    matches = list(day_dir.glob(f"{prefix}*.jsonl"))
    if matches:
        return matches[0]
    target_ts = local_dt.timestamp()
    candidates = [(abs(p.stat().st_mtime - target_ts), p) for p in day_dir.glob("rollout-*.jsonl")]
    candidates.sort()
    if candidates and candidates[0][0] <= 60:
        return candidates[0][1]
    return None


def parse_rollout_events(rollout_path: Path) -> list[dict[str, Any]]:
    """Return ordered list of {kind, ...} dicts from rollout payloads."""
    events: list[dict[str, Any]] = []
    with rollout_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type", "")
            ts = event.get("timestamp", "")

            if ptype == "function_call":
                name = payload.get("name", "")
                args_raw = payload.get("arguments", "")
                args: Any = args_raw
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except json.JSONDecodeError:
                        args = {"raw": args_raw}
                if not isinstance(args, dict):
                    args = {"raw": str(args)}
                events.append({
                    "kind": "function_call",
                    "call_id": payload.get("call_id", ""),
                    "name": name,
                    "arguments": args,
                    "ts": ts,
                })
            elif ptype == "function_call_output":
                output = payload.get("output", "")
                if isinstance(output, dict):
                    output = output.get("content") or json.dumps(output)
                events.append({
                    "kind": "function_call_output",
                    "call_id": payload.get("call_id", ""),
                    "output": str(output),
                    "ts": ts,
                })
            elif ptype == "custom_tool_call":
                inp = payload.get("input", "")
                args: Any
                if isinstance(inp, str):
                    args = {"input": inp}
                elif isinstance(inp, dict):
                    args = inp
                else:
                    args = {"input": str(inp)}
                events.append({
                    "kind": "function_call",
                    "call_id": payload.get("call_id", ""),
                    "name": payload.get("name", ""),
                    "arguments": args,
                    "ts": ts,
                })
            elif ptype == "custom_tool_call_output":
                events.append({
                    "kind": "function_call_output",
                    "call_id": payload.get("call_id", ""),
                    "output": str(payload.get("output", "")),
                    "ts": ts,
                })
            elif ptype == "agent_message":
                text = payload.get("message", "")
                if text:
                    events.append({"kind": "agent_message", "text": str(text), "ts": ts})

    return events


def parse_ts(ts: str, fallback: float) -> float:
    if not ts:
        return fallback
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return fallback


def make_action_request(
    *,
    function: str,
    arguments: dict[str, Any],
    sender: str,
    recipient: str,
    response_id: str,
    created_at: float,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "created_at": created_at,
        "metadata": {"lion_class": "lionagi.protocols.messages.action_request.ActionRequest"},
        "content": {
            "arguments": arguments,
            "function": function,
            "action_response_id": response_id,
        },
        "embedding": None,
        "sender": sender,
        "recipient": recipient,
        "channel": None,
        "role": "action",
    }


def make_action_response(
    *,
    function: str,
    arguments: dict[str, Any],
    output: str,
    sender: str,
    recipient: str,
    request_id: str,
    created_at: float,
    response_id: str,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "created_at": created_at,
        "metadata": {"lion_class": "lionagi.protocols.messages.action_response.ActionResponse"},
        "content": {
            "arguments": arguments,
            "function": function,
            "output": output,
            "action_request_id": request_id,
        },
        "embedding": None,
        "sender": recipient,
        "recipient": sender,
        "channel": None,
        "role": "action",
    }


def make_assistant_response(
    *,
    text: str,
    sender: str,
    recipient: str,
    created_at: float,
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "created_at": created_at,
        "metadata": {"lion_class": "lionagi.protocols.messages.assistant_response.AssistantResponse"},
        "content": {"assistant_response": text},
        "embedding": None,
        "sender": sender,
        "recipient": recipient,
        "channel": None,
        "role": "assistant",
    }


def reconstruct_messages(
    original_collections: list[dict[str, Any]],
    rollout_events: list[dict[str, Any]],
    branch_id: str,
) -> list[dict[str, Any]]:
    """Build the new collection: keep system + user, replace assistant tail
    with the proper ordered sequence of action_request/action_response pairs
    interleaved with assistant messages from the rollout."""
    if not original_collections:
        return original_collections

    # Locate the assistant message — everything we want to replace lives at
    # the end.
    new_collections: list[dict[str, Any]] = []
    user_sender = "user"
    branch_sender = branch_id
    last_ts = original_collections[0]["created_at"]

    for msg in original_collections:
        if msg.get("role") in ("system", "user"):
            new_collections.append(msg)
            last_ts = max(last_ts, msg.get("created_at", last_ts))
            if msg.get("role") == "user":
                sender = msg.get("sender")
                if sender and sender != "system":
                    user_sender = sender
            recipient = msg.get("recipient")
            if recipient:
                branch_sender = recipient

    # Replay rollout: function_call -> ActionRequest, function_call_output ->
    # ActionResponse linked by call_id, agent_message -> AssistantResponse.
    pending_requests: dict[str, dict[str, Any]] = {}
    for ev in rollout_events:
        kind = ev["kind"]
        ts = parse_ts(ev.get("ts", ""), last_ts + 0.001)
        last_ts = max(last_ts, ts)

        if kind == "function_call":
            response_id = str(uuid.uuid4())
            req = make_action_request(
                function=ev["name"],
                arguments=ev["arguments"],
                sender=branch_sender,
                recipient=user_sender,
                response_id=response_id,
                created_at=ts,
            )
            new_collections.append(req)
            pending_requests[ev["call_id"]] = {"req_id": req["id"], "res_id": response_id, "req": req}

        elif kind == "function_call_output":
            paired = pending_requests.pop(ev["call_id"], None)
            if paired is None:
                # Orphan output — synthesize a request stub
                req_stub = make_action_request(
                    function="unknown",
                    arguments={},
                    sender=branch_sender,
                    recipient=user_sender,
                    response_id=str(uuid.uuid4()),
                    created_at=ts,
                )
                new_collections.append(req_stub)
                response_id = req_stub["content"]["action_response_id"]
                req_id = req_stub["id"]
                fn = "unknown"
                args = {}
            else:
                response_id = paired["res_id"]
                req_id = paired["req_id"]
                fn = paired["req"]["content"]["function"]
                args = paired["req"]["content"]["arguments"]
            resp = make_action_response(
                function=fn,
                arguments=args,
                output=ev["output"],
                sender=branch_sender,
                recipient=user_sender,
                request_id=req_id,
                created_at=ts,
                response_id=response_id,
            )
            new_collections.append(resp)

        elif kind == "agent_message":
            asst = make_assistant_response(
                text=ev["text"],
                sender=branch_sender,
                recipient=user_sender,
                created_at=ts,
            )
            new_collections.append(asst)

    return new_collections


def has_action_messages(branch_data: dict[str, Any]) -> bool:
    msgs = branch_data.get("messages", {})
    cols = msgs.get("collections", []) if isinstance(msgs, dict) else []
    return any(isinstance(c, dict) and c.get("role") == "action" for c in cols)


def get_provider(branch_data: dict[str, Any]) -> str:
    cm = branch_data.get("chat_model") or {}
    ep = cm.get("endpoint") or {}
    cfg = ep.get("config") or {}
    return str(cfg.get("provider", ""))


def migrate_branch(branch_path: Path, rollout_path: Path) -> int:
    """Returns number of messages added (0 if nothing changed)."""
    with branch_path.open() as fh:
        branch = json.load(fh)

    if has_action_messages(branch):
        return 0

    rollout_events = parse_rollout_events(rollout_path)
    if not rollout_events:
        return 0

    msgs = branch.get("messages", {})
    if not isinstance(msgs, dict):
        return 0
    original = msgs.get("collections", [])
    if not isinstance(original, list):
        return 0

    branch_id = branch.get("id", "")
    new_collections = reconstruct_messages(original, rollout_events, branch_id)
    if len(new_collections) <= len(original):
        return 0

    msgs["collections"] = new_collections
    msgs["progression"]["order"] = [c["id"] for c in new_collections if isinstance(c, dict)]

    backup = branch_path.with_suffix(".json.pre-migrate")
    if not backup.exists():
        backup.write_text(json.dumps(branch))

    tmp = branch_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(branch))
    tmp.replace(branch_path)
    return len(new_collections) - len(original)


def main() -> int:
    runs_root = LIONAGI_HOME / "runs"
    if not runs_root.exists():
        print(f"No runs at {runs_root}", file=sys.stderr)
        return 1

    total_runs = 0
    matched_runs = 0
    migrated_branches = 0
    added_messages = 0
    skipped_existing = 0
    no_rollout = 0

    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        branches_dir = run_dir / "branches"
        if not branches_dir.exists():
            continue

        # Look at every branch — flow runs have multiple codex branches.
        rollout = find_rollout(run_dir.name)

        for branch_file in branches_dir.glob("*.json"):
            try:
                with branch_file.open() as fh:
                    branch = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue

            provider = get_provider(branch)
            if provider != "codex":
                continue

            total_runs += 1

            if has_action_messages(branch):
                skipped_existing += 1
                continue

            # For per-branch runs in flows, the rollout is found by the run
            # timestamp. Single-agent runs match 1:1; flows have multiple
            # codex branches but each one was its own codex session — we
            # need a per-branch rollout match.
            branch_rollout = rollout
            branch_created_at = branch.get("created_at", 0)
            if branch_rollout is None or len(list(branches_dir.glob("*.json"))) > 1:
                branch_rollout = find_rollout_for_branch(branch_created_at)

            if branch_rollout is None:
                no_rollout += 1
                continue

            added = migrate_branch(branch_file, branch_rollout)
            if added > 0:
                matched_runs += 1
                migrated_branches += 1
                added_messages += added
                print(f"  {run_dir.name} :: {branch_file.name[:16]}  +{added} messages from {branch_rollout.name}")

    print()
    print(f"Codex branches scanned: {total_runs}")
    print(f"Already migrated (skipped): {skipped_existing}")
    print(f"No matching rollout: {no_rollout}")
    print(f"Branches migrated: {migrated_branches}")
    print(f"Messages added: {added_messages}")
    return 0


def find_rollout_for_branch(branch_created_at: float) -> Path | None:
    """Find rollout by branch creation timestamp (within ±60s)."""
    if not CODEX_SESSIONS_ROOT.exists() or not branch_created_at:
        return None
    local_dt = datetime.fromtimestamp(branch_created_at)
    day_dir = CODEX_SESSIONS_ROOT / f"{local_dt.year:04d}" / f"{local_dt.month:02d}" / f"{local_dt.day:02d}"
    if not day_dir.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in day_dir.glob("rollout-*.jsonl"):
        delta = abs(p.stat().st_mtime - branch_created_at)
        if delta <= 60:
            candidates.append((delta, p))
    candidates.sort()
    return candidates[0][1] if candidates else None


if __name__ == "__main__":
    raise SystemExit(main())

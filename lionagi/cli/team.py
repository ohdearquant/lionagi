# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li team` — persistent team messaging (inbox pattern)."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from lionagi.ln._utils import now_utc
from lionagi.utils import LIONAGI_HOME

from ._logging import log_error, warn

TEAMS_DIR = LIONAGI_HOME / "teams"

# ── Message kinds ────────────────────────────────────────────────────────
# "message" is ordinary content; the other three are lifecycle SIGNALS a
# worker emits about itself, read by `compute_quiescence` below.
MESSAGE_KIND = "message"
DONE_KIND = "done"
FINISHED_KIND = "finished"
WAKEUP_KIND = "wakeup"


def _teams_dir() -> Path:
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    return TEAMS_DIR


def read_team_json(path: Path) -> dict[str, Any] | None:
    """Read one team JSON file under a SHARED flock — the canonical
    safe-read every team-file reader goes through. Returns None (never
    raises) for a missing, unreadable, or corrupt file."""
    try:
        with open(path) as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_SH)
            try:
                raw = fp.read()
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        return json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        return None


def _team_file(team_id: str) -> Path:
    """Resolve a team by id/prefix/name to its JSON path."""
    for p in _teams_dir().glob("*.json"):
        data = read_team_json(p)
        if data is None:
            continue
        if data.get("id") == team_id or data.get("id", "").startswith(team_id):
            return p
        if data.get("name") == team_id:
            return p
    raise FileNotFoundError(f"No team found matching '{team_id}'")


@contextlib.contextmanager
def _locked_team(team_id: str, *, create_path: Path | None = None):
    """Read-modify-write a team file under an exclusive POSIX lock; concurrent sends serialize."""
    path = create_path if create_path is not None else _team_file(team_id)
    # r+ to read-then-rewrite; w+ to initialize on the create flow.
    mode = "r+" if path.exists() else "w+"
    with open(path, mode) as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            fp.seek(0)
            raw = fp.read()
            data = json.loads(raw) if raw.strip() else {}
            yield data
            fp.seek(0)
            fp.truncate()
            fp.write(json.dumps(data, indent=2, default=str))
            # flush+fsync before unlock: otherwise a waiting reader can
            # acquire the lock and observe stale content (write() only fills
            # a buffer) — see docs/internals/cli.md.
            fp.flush()
            os.fsync(fp.fileno())
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _load_team(team_id: str) -> dict:
    """Snapshot read under a shared lock. Raises FileNotFoundError
    uniformly for both a missing team and a failed decode."""
    path = _team_file(team_id)
    data = read_team_json(path)
    if data is None:
        raise FileNotFoundError(f"Team '{team_id}' is empty or missing")
    return data


def _read_by_map(read_by) -> dict[str, str]:
    """Normalize read_by to dict[name → ISO timestamp]; converts legacy list format."""
    if isinstance(read_by, dict):
        return dict(read_by)
    if isinstance(read_by, list):
        return {name: "" for name in read_by}
    return {}


def _message_targets(msg: Mapping[str, Any]) -> list[str]:
    """Normalize a message's ``to`` field to a list (``"*"`` stays a
    one-element broadcast marker, matching how ``cmd_receive`` already
    treats it)."""
    to = msg.get("to")
    if to is None:
        return []
    return [to] if isinstance(to, str) else list(to)


def _build_message(
    sender: str,
    to: str | list[str],
    content: str,
    *,
    kind: str = MESSAGE_KIND,
    from_op: str | None = None,
    artifacts: list[str] | None = None,
) -> dict:
    """Construct one team-inbox message dict — the single code path every
    writer (the `li team send` command, the done-signal helper below, the
    team-lifecycle coordinator) goes through, so the message shape can never
    drift between callers."""
    msg: dict = {
        "id": uuid4().hex[:12],
        "from": sender,
        "to": to if isinstance(to, list) else [to],
        "content": content,
        "timestamp": now_utc().isoformat(),
        "read_by": {},
        "kind": kind,
    }
    if from_op:
        msg["from_op"] = from_op
    if artifacts:
        msg["artifacts"] = list(artifacts)
    return msg


# ── Commands ─────────────────────────────────────────────────────────────


def cmd_create(args: argparse.Namespace) -> int:
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if not members:
        log_error("--members requires at least one name")
        return 1

    team_id = uuid4().hex[:12]
    path = _teams_dir() / f"{team_id}.json"
    with _locked_team(team_id, create_path=path) as data:
        data.update(
            {
                "id": team_id,
                "name": args.name,
                "members": members,
                "messages": [],
                "created_at": now_utc().isoformat(),
            }
        )
    print(f"Created team '{args.name}' ({team_id})")
    print(f"  Members: {', '.join(members)}")
    print(f"  File: {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    teams_dir = _teams_dir()
    files = sorted(teams_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("No teams.")
        return 0

    for p in files:
        data = read_team_json(p)
        if data is None:
            continue  # skip, don't crash the listing
        n_msgs = len(data.get("messages", []))
        members = ", ".join(data.get("members", []))
        print(f"  {data['id']}  {data['name']:20s}  [{members}]  {n_msgs} msgs")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    data = _load_team(args.team)
    print(f"Team: {data['name']} ({data['id']})")
    print(f"Created: {data['created_at']}")
    print(f"Members: {', '.join(data['members'])}")

    msgs = data.get("messages", [])
    if not msgs:
        print("\nNo messages.")
        return 0

    print(f"\n{'─' * 60}")
    for msg in msgs:
        to_str = msg["to"] if isinstance(msg["to"], str) else ", ".join(msg["to"])
        read_by = _read_by_map(msg.get("read_by"))
        marker = "" if not read_by else f"  (read by: {', '.join(read_by)})"
        ts = msg.get("timestamp", "")[:19]
        op = msg.get("from_op")
        op_str = f" op={op}" if op else ""
        print(f"  [{ts}] {msg['from']}{op_str} → {to_str}{marker}")
        for line in msg["content"].splitlines():
            print(f"    {line}")
        print()
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Append one message under an exclusive lock, so concurrent ``li team
    send`` invocations from parallel workers serialize cleanly."""
    with _locked_team(args.team) as data:
        if not data:
            log_error(f"Team '{args.team}' is empty or missing")
            return 1
        members = data.get("members", [])

        sender = args.sender or "_cli"
        if sender != "_cli" and sender not in members:
            warn(f"'{sender}' is not a team member")

        if args.to.lower() == "all":
            recipients = ["*"]
        else:
            recipients = [r.strip() for r in args.to.split(",") if r.strip()]
            for r in recipients:
                if r not in members:
                    warn(f"'{r}' is not a team member")

        artifacts = None
        if getattr(args, "artifacts", None):
            artifacts = [a.strip() for a in args.artifacts.split(",") if a.strip()]
        msg = _build_message(
            sender,
            recipients,
            args.content,
            kind=getattr(args, "kind", None) or MESSAGE_KIND,
            from_op=getattr(args, "from_op", None),
            artifacts=artifacts,
        )

        data.setdefault("messages", []).append(msg)
        team_name = data.get("name", args.team)

    to_display = "all" if recipients == ["*"] else ", ".join(recipients)
    print(f"Sent to {to_display} in '{team_name}'")
    return 0


# ── Lifecycle signals (done / finished / wakeup) ────────────────────────────
# Every writer here goes through `_build_message` and `_locked_team`'s flock
# discipline — the structure is always produced by this code, never an LLM.


def post_done_signal(
    team_id: str,
    *,
    worker: str,
    summary: str,
    artifacts: list[str] | None = None,
    from_op: str | None = None,
) -> dict:
    """Append a ``kind="done"`` message: *worker* has finished its current
    turn and may be revived later (unlike ``post_finished_signal``)."""
    with _locked_team(team_id) as data:
        if not data:
            raise FileNotFoundError(f"Team '{team_id}' is empty or missing")
        msg = _build_message(
            worker, ["*"], summary, kind=DONE_KIND, from_op=from_op, artifacts=artifacts
        )
        data.setdefault("messages", []).append(msg)
    return msg


def post_finished_signal(
    team_id: str,
    *,
    worker: str,
    summary: str,
    from_op: str | None = None,
) -> dict:
    """Append a ``kind="finished"`` message: *worker* is permanently done —
    ``compute_quiescence`` retires it and it is never revived by a round."""
    with _locked_team(team_id) as data:
        if not data:
            raise FileNotFoundError(f"Team '{team_id}' is empty or missing")
        msg = _build_message(worker, ["*"], summary, kind=FINISHED_KIND, from_op=from_op)
        data.setdefault("messages", []).append(msg)
    return msg


def post_wakeup_signal(
    team_id: str,
    *,
    target: str,
    sender: str = "coordinator",
    content: str = "",
    from_op: str | None = None,
) -> dict:
    """Append a ``kind="wakeup"`` message addressed to *target* — marks it
    active again in ``compute_quiescence``. Used both for peer-to-peer
    wakeups (the messenger tool's ``wakeup`` action) and for the
    coordinator's own round re-invocations."""
    with _locked_team(team_id) as data:
        if not data:
            raise FileNotFoundError(f"Team '{team_id}' is empty or missing")
        msg = _build_message(sender, [target], content, kind=WAKEUP_KIND, from_op=from_op)
        data.setdefault("messages", []).append(msg)
    return msg


def pop_unread_messages(team_id: str, member: str) -> list[dict]:
    """Read + consume *member*'s unread ``kind="message"`` mail under lock
    (lifecycle signals are bookkeeping, excluded here). Returns plain
    ``{"from", "content", "timestamp"}`` dicts for round-injection context.
    """
    with _locked_team(team_id) as data:
        if not data:
            return []
        msgs = data.get("messages", [])
        unread: list[dict] = []
        for msg in msgs:
            if msg.get("kind", MESSAGE_KIND) != MESSAGE_KIND:
                continue
            read_by = _read_by_map(msg.get("read_by"))
            if member in read_by:
                continue
            targets = _message_targets(msg)
            if targets == ["*"] or member in targets:
                unread.append(msg)

        now = now_utc().isoformat()
        for msg in unread:
            read_by = _read_by_map(msg.get("read_by"))
            read_by[member] = now
            msg["read_by"] = read_by

    return [
        {
            "from": m.get("from", "?"),
            "content": m.get("content", ""),
            "timestamp": m.get("timestamp", ""),
        }
        for m in unread
    ]


@dataclass(frozen=True)
class QuiescenceState:
    """Snapshot of a team-mode run's lifecycle at one coordinator tick.

    The pure result of `compute_quiescence` — plain data, no I/O — so tests
    can assert on it without spawning a single agent or touching a file.
    """

    quiescent: bool
    should_continue: bool
    active_workers: frozenset[str]
    idle_workers: frozenset[str]
    retired_workers: frozenset[str]
    pending_targets: frozenset[str]
    rounds_exhausted: bool


def compute_quiescence(
    messages: Sequence[Mapping[str, Any]],
    *,
    worker_names: Iterable[str],
    rounds_run: int,
    max_rounds: int,
    coordinator_wants_round: bool = False,
) -> QuiescenceState:
    """Pure predicate: is this team-mode run done, or does it need another
    wakeup round? Reads only message ``kind``/``from``/``to``/``read_by``,
    never a file/branch/agent. See docs/internals/cli.md for the lifecycle
    model (active/idle/retired) and the quiescence condition.
    """
    names = list(dict.fromkeys(worker_names))  # de-dup, preserve order
    state: dict[str, str] = dict.fromkeys(names, "active")

    for msg in messages:
        kind = msg.get("kind", MESSAGE_KIND)
        sender = msg.get("from")
        if kind == DONE_KIND and sender in state:
            state[sender] = "idle"
        elif kind == FINISHED_KIND and sender in state:
            state[sender] = "retired"
        elif kind == WAKEUP_KIND:
            for target in _message_targets(msg):
                if target == "*":
                    for w in state:
                        if state[w] != "retired":
                            state[w] = "active"
                elif target in state and state[target] != "retired":
                    state[target] = "active"

    active = frozenset(w for w, s in state.items() if s == "active")
    idle = frozenset(w for w, s in state.items() if s == "idle")
    retired = frozenset(w for w, s in state.items() if s == "retired")

    pending: set[str] = set()
    for msg in messages:
        if msg.get("kind", MESSAGE_KIND) != MESSAGE_KIND:
            continue
        targets = _message_targets(msg)
        broadcast = targets == ["*"]
        read_by = _read_by_map(msg.get("read_by"))
        for w in idle:
            if w in pending:
                continue
            if (broadcast or w in targets) and w not in read_by:
                pending.add(w)

    rounds_exhausted = rounds_run >= max_rounds
    all_settled = not active
    should_continue = (
        all_settled
        and bool(names)
        and not rounds_exhausted
        and (bool(pending) or coordinator_wants_round)
    )
    quiescent = all_settled and not should_continue

    return QuiescenceState(
        quiescent=quiescent,
        should_continue=should_continue,
        active_workers=active,
        idle_workers=idle,
        retired_workers=retired,
        pending_targets=frozenset(pending),
        rounds_exhausted=rounds_exhausted,
    )


def cmd_receive(args: argparse.Namespace) -> int:
    """Read unread messages under a lock, marking them with the read
    timestamp. Lock is held across read + write so two concurrent
    receives don't double-mark."""
    me = args.member

    with _locked_team(args.team) as data:
        if not data:
            log_error(f"Team '{args.team}' is empty or missing")
            return 1
        if me and me not in data.get("members", []):
            warn(f"'{me}' is not a member of '{data.get('name', args.team)}'")

        msgs = data.get("messages", [])
        unread: list[dict] = []
        for msg in msgs:
            read_by = _read_by_map(msg.get("read_by"))
            if me and me in read_by:
                continue
            targets = msg["to"]
            if targets == ["*"] or (me and me in targets) or not me:
                unread.append(msg)

        if not unread:
            print("No new messages." if me else "No messages.")
            return 0

        now = now_utc().isoformat()
        for msg in unread:
            read_by = _read_by_map(msg.get("read_by"))
            if me and me not in read_by:
                read_by[me] = now
                msg["read_by"] = read_by

    # Print outside the lock — display I/O shouldn't hold the lock.
    for msg in unread:
        to_str = "all" if msg["to"] == ["*"] else ", ".join(msg["to"])
        ts = msg.get("timestamp", "")[:19]
        op = msg.get("from_op")
        op_str = f" op={op}" if op else ""
        print(f"[{ts}] {msg['from']}{op_str} → {to_str}")
        print(f"  {msg['content']}")
        print()

    print(f"({len(unread)} message{'s' if len(unread) != 1 else ''})")
    return 0


# ── CLI registration ─────────────────────────────────────────────────────


def add_team_subparser(subparsers: argparse._SubParsersAction) -> None:
    team = subparsers.add_parser(
        "team",
        help="Team messaging — send/receive between named agents.",
        description="Persistent inbox-style messaging for agent teams.",
    )
    team_sub = team.add_subparsers(dest="team_command", required=True)

    # create
    cr = team_sub.add_parser("create", help="Create a new team.")
    cr.add_argument("name", help="Team name.")
    cr.add_argument(
        "-m",
        "--members",
        required=True,
        help="Comma-separated member names.",
    )

    # list
    team_sub.add_parser("list", aliases=["ls"], help="List all teams.")

    # show
    sh = team_sub.add_parser("show", help="Show team details and messages.")
    sh.add_argument("team", help="Team ID or name.")

    # send
    snd = team_sub.add_parser("send", help="Send a message to team members.")
    snd.add_argument("content", help="Message content.")
    snd.add_argument("--team", "-t", required=True, help="Team ID or name.")
    snd.add_argument(
        "--to",
        required=True,
        help="Recipients: 'all' or comma-separated names.",
    )
    snd.add_argument("--from", dest="sender", default=None, help="Sender name.")
    snd.add_argument(
        "--from-op",
        dest="from_op",
        default=None,
        help=(
            "The op id this message belongs to (e.g. 'o3'). Ties a coord "
            "signal to a specific invocation when the sender agent runs "
            "multiple ops on the same branch."
        ),
    )
    snd.add_argument(
        "--kind",
        default=None,
        choices=(MESSAGE_KIND, DONE_KIND, FINISHED_KIND, WAKEUP_KIND),
        help=(
            "Message kind (default: 'message'). Use 'done' when you've "
            "finished your part and may be revived later, 'finished' when "
            "you're permanently done — quiescence detection reads this."
        ),
    )
    snd.add_argument(
        "--artifacts",
        default=None,
        metavar="PATH,...",
        help="Comma-separated artifact paths to attach (used with --kind done).",
    )

    # receive
    rcv = team_sub.add_parser("receive", aliases=["recv"], help="Read inbox messages.")
    rcv.add_argument("--team", "-t", required=True, help="Team ID or name.")
    rcv.add_argument("--as", dest="member", default=None, help="Read as this member.")


def run_team(args: argparse.Namespace) -> int:
    cmd = args.team_command
    if cmd == "create":
        return cmd_create(args)
    if cmd in ("list", "ls"):
        return cmd_list(args)
    if cmd == "show":
        return cmd_show(args)
    if cmd == "send":
        return cmd_send(args)
    if cmd in ("receive", "recv"):
        return cmd_receive(args)
    log_error(f"Unknown team command: {cmd}")
    return 1

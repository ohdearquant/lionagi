# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li team` — persistent team messaging (inbox pattern)."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
from pathlib import Path
from uuid import uuid4

from lionagi.ln._utils import now_utc
from lionagi.utils import LIONAGI_HOME

from ._logging import log_error, warn

TEAMS_DIR = LIONAGI_HOME / "teams"


def _teams_dir() -> Path:
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    return TEAMS_DIR


def _team_file(team_id: str) -> Path:
    """Resolve a team by id/prefix/name to its JSON path."""
    for p in _teams_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
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
    # Open in r+ so we can read current contents AND truncate+rewrite.
    # If the file doesn't exist yet (create flow), the caller supplies
    # create_path and we open w+ to initialize.
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
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)


def _load_team(team_id: str) -> dict:
    """Snapshot read — no lock held after return. Use when only reading."""
    path = _team_file(team_id)
    return json.loads(path.read_text())


def _read_by_map(read_by) -> dict[str, str]:
    """Normalize read_by to dict[name → ISO timestamp].

    Old-format messages stored read_by as a list of names; convert on
    read so callers always see a dict. Callers writing back should also
    use the dict form.
    """
    if isinstance(read_by, dict):
        return dict(read_by)
    if isinstance(read_by, list):
        return {name: "" for name in read_by}
    return {}


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
        data = json.loads(p.read_text())
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

        msg = {
            "id": uuid4().hex[:12],
            "from": sender,
            "to": recipients,
            "content": args.content,
            "timestamp": now_utc().isoformat(),
            "read_by": {},
        }
        # Op context: when a worker sends mid-flow, --from-op ties the
        # message to the specific invocation (multiple ops on one agent
        # would otherwise be indistinguishable).
        if getattr(args, "from_op", None):
            msg["from_op"] = args.from_op

        data.setdefault("messages", []).append(msg)
        team_name = data.get("name", args.team)

    to_display = "all" if recipients == ["*"] else ", ".join(recipients)
    print(f"Sent to {to_display} in '{team_name}'")
    return 0


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

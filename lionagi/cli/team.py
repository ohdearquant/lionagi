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

from lionagi._auto import CliDeclaration, auto_register
from lionagi._paths import ensure_lionagi_dir
from lionagi.cli._util import AmbiguousIdError
from lionagi.ln._json_dump import raise_if_non_finite
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
    return ensure_lionagi_dir(TEAMS_DIR)


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
    """Resolve a team by id, name, or an unambiguous id prefix, to its JSON path.

    An id and a name are both complete answers and settle it. A prefix is a
    guess, so one that fits two teams is refused rather than resolved to
    whichever file the directory listing happened to yield first.
    """
    prefix_hits: list[tuple[str, Path]] = []
    for p in sorted(_teams_dir().glob("*.json")):
        data = read_team_json(p)
        if data is None:
            continue
        if data.get("id") == team_id or data.get("name") == team_id:
            return p
        if data.get("id", "").startswith(team_id):
            prefix_hits.append((data["id"], p))

    if len(prefix_hits) > 1:
        raise AmbiguousIdError(team_id, "team", [tid for tid, _ in prefix_hits])
    if prefix_hits:
        return prefix_hits[0][1]
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
            # Checked before the file is truncated, so a payload json.dumps
            # would write as the token NaN/Infinity — read back only by Python,
            # rejected by every strict reader — leaves the previous file intact.
            raise_if_non_finite(data, default=str)
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
    history_boundary: int = 0,
) -> QuiescenceState:
    """Pure predicate: is this team-mode run done, or does it need another
    wakeup round? Reads only message ``kind``/``from``/``to``/``read_by``,
    never a file/branch/agent. See docs/internals/cli.md for the lifecycle
    model (active/idle/retired) and the quiescence condition.

    ``history_boundary`` is the index (into ``messages``) at which the
    current run generation begins — everything before it is a prior run's
    history. ``--team-attach`` reuses one team file (and often the same
    role-derived worker names) across runs, so a prior run's ``done``/
    ``finished``/``wakeup`` signals must not classify this run's workers as
    already idle/retired before they have posted anything themselves; only
    signals at or after the boundary count toward active/idle/retired.
    Content (``kind="message"``) mail is exempt from the boundary — prior
    runs' unread mail must still be delivered to this run's workers, so the
    pending-mail scan below always looks at the full ``messages`` sequence.
    """
    names = list(dict.fromkeys(worker_names))  # de-dup, preserve order
    state: dict[str, str] = dict.fromkeys(names, "active")

    for msg in messages[history_boundary:]:
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
    cr.add_argument(
        "name",
        help="Name for the new team. Every other team command accepts it in place of the team id.",
    )
    cr.add_argument(
        "-m",
        "--members",
        required=True,
        help=(
            "Comma-separated member names. Only these names can send or receive as themselves; "
            "a message from or to anyone else still goes through, with a warning."
        ),
    )

    # list
    team_sub.add_parser("list", aliases=["ls"], help="List all teams.")

    # show
    sh = team_sub.add_parser("show", help="Show team details and messages.")
    sh.add_argument("team", help="Team to show — its id, its name, or an unambiguous id prefix.")

    # send
    snd = team_sub.add_parser("send", help="Send a message to team members.")
    snd.add_argument("content", help="Message body, as the recipients will read it.")
    snd.add_argument(
        "--team",
        "-t",
        required=True,
        help="Team to send into — its id, its name, or an unambiguous id prefix.",
    )
    snd.add_argument(
        "--to",
        required=True,
        help="Recipients: 'all' to broadcast to the whole team, or comma-separated member names.",
    )
    snd.add_argument(
        "--from",
        dest="sender",
        default=None,
        help=(
            "Name to send as, so recipients know who is asking (defaults to '_cli'). A name that "
            "is not a member still sends, and is reported as a warning."
        ),
    )
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
    rcv.add_argument(
        "--team",
        "-t",
        required=True,
        help="Team to read from — its id, its name, or an unambiguous id prefix.",
    )
    rcv.add_argument(
        "--as",
        dest="member",
        default=None,
        help=(
            "Read as this member: returns only their unread mail and marks it read. Omit it to "
            "dump every message and mark nothing read."
        ),
    )


@auto_register(area="team", cli=CliDeclaration(seed="team", parser_factory=add_team_subparser))
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


# ── machine result ────────────────────────────────────────────────────────────


def _machine_list_data() -> dict[str, Any]:
    from .machine import REASON_UNREADABLE, available, list_directory, unavailable

    # Read without creating: the human path ensures the directory exists as a
    # side effect of being about to write to it, and a listing has nothing to
    # write. A directory that was never created is a definitive zero teams,
    # which is what the human path also reports once it has made one.
    listing = list_directory(TEAMS_DIR, missing_is_empty=True)
    if not listing["available"]:
        return {"teams": listing, "unreadable": []}

    teams: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []
    for path in sorted(TEAMS_DIR.glob("*.json")):
        data = read_team_json(path)
        if data is None:
            # The printed listing skips these silently. A machine caller is told,
            # because "four teams" and "four teams and one file I could not read"
            # are different answers and only one of them is complete.
            unreadable.append(unavailable(REASON_UNREADABLE, str(path)))
            continue
        teams.append(
            {
                "id": data.get("id"),
                "name": data.get("name"),
                "members": data.get("members") or [],
                "created_at": data.get("created_at"),
                "message_count": len(data.get("messages") or []),
                "path": str(path),
            }
        )
    teams.sort(key=lambda t: (t["name"] or "", t["id"] or ""))
    return {"teams": available(teams), "unreadable": unreadable}


def _machine_list(argv: list[str]) -> dict[str, Any]:
    from .machine import MachineError

    if argv:
        raise MachineError("invalid_input", f"li team list takes no arguments: {' '.join(argv)}")
    return _machine_list_data()


def _machine_create(argv: list[str]) -> dict[str, Any]:
    """`li team create NAME --members A,B --machine`."""
    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li team create")
    parser.add_argument("name", help="Display name for the new team")
    parser.add_argument("-m", "--members", required=True, help="Comma-separated member names")
    args = parse_machine_argv(parser, argv)

    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if not members:
        raise MachineError("invalid_input", "--members requires at least one name")

    team_id = uuid4().hex[:12]
    path = _teams_dir() / f"{team_id}.json"
    created_at = now_utc().isoformat()
    with _locked_team(team_id, create_path=path) as data:
        data.update(
            {
                "id": team_id,
                "name": args.name,
                "members": members,
                "messages": [],
                "created_at": created_at,
            }
        )
    return {
        "id": team_id,
        "name": args.name,
        "members": members,
        "created_at": created_at,
        "path": str(path),
    }


def _machine_show(argv: list[str]) -> dict[str, Any]:
    """`li team show TEAM --machine`."""
    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li team show")
    parser.add_argument("team", help="Team id (or unambiguous prefix)")
    args = parse_machine_argv(parser, argv)

    try:
        data = _load_team(args.team)
    except FileNotFoundError as exc:
        raise MachineError("not_found", str(exc)) from exc
    except AmbiguousIdError as exc:
        raise MachineError("invalid_input", str(exc)) from exc
    return {"team": data}


def _machine_send(argv: list[str]) -> dict[str, Any]:
    """`li team send CONTENT --team T --to R --machine`."""
    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li team send")
    parser.add_argument("content", help="Message body")
    parser.add_argument("--team", "-t", required=True, help="Team id (or unambiguous prefix)")
    parser.add_argument("--to", required=True, help="Recipients: comma-separated names, or 'all'")
    parser.add_argument(
        "--from", dest="sender", default=None, help="Sender name recorded on the message"
    )
    parser.add_argument(
        "--from-op",
        dest="from_op",
        default=None,
        help="Originating op id, for tracing the emitting turn",
    )
    parser.add_argument(
        "--kind",
        default=None,
        choices=(MESSAGE_KIND, DONE_KIND, FINISHED_KIND, WAKEUP_KIND),
        help="Message kind; 'done'/'finished' signal completion",
    )
    parser.add_argument(
        "--artifacts", default=None, help="Comma-separated artifact paths to attach"
    )
    args = parse_machine_argv(parser, argv)

    try:
        with _locked_team(args.team) as data:
            if not data:
                raise MachineError("not_found", f"Team '{args.team}' is empty or missing")

            sender = args.sender or "_cli"
            if args.to.lower() == "all":
                recipients = ["*"]
            else:
                recipients = [r.strip() for r in args.to.split(",") if r.strip()]

            artifacts = None
            if args.artifacts:
                artifacts = [a.strip() for a in args.artifacts.split(",") if a.strip()]
            msg = _build_message(
                sender,
                recipients,
                args.content,
                kind=args.kind or MESSAGE_KIND,
                from_op=args.from_op,
                artifacts=artifacts,
            )
            data.setdefault("messages", []).append(msg)
            team_id = data.get("id", args.team)
    except FileNotFoundError as exc:
        raise MachineError("not_found", str(exc)) from exc
    except AmbiguousIdError as exc:
        raise MachineError("invalid_input", str(exc)) from exc

    return {
        "message_id": msg["id"],
        "team_id": team_id,
        "to": recipients,
        "timestamp": msg["timestamp"],
    }


def _machine_receive(argv: list[str]) -> dict[str, Any]:
    """`li team receive --team T [--as MEMBER] --machine`."""
    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li team receive")
    parser.add_argument("--team", "-t", required=True, help="Team id (or unambiguous prefix)")
    parser.add_argument(
        "--as", dest="member", default=None, help="Read as this member; marks their messages read"
    )
    args = parse_machine_argv(parser, argv)
    me = args.member

    try:
        with _locked_team(args.team) as data:
            if not data:
                raise MachineError("not_found", f"Team '{args.team}' is empty or missing")
            team_id = data.get("id", args.team)

            msgs = data.get("messages", [])
            unread: list[dict] = []
            for msg in msgs:
                read_by = _read_by_map(msg.get("read_by"))
                if me and me in read_by:
                    continue
                targets = msg["to"]
                if targets == ["*"] or (me and me in targets) or not me:
                    unread.append(msg)

            now = now_utc().isoformat()
            for msg in unread:
                read_by = _read_by_map(msg.get("read_by"))
                if me and me not in read_by:
                    read_by[me] = now
                    msg["read_by"] = read_by
    except FileNotFoundError as exc:
        raise MachineError("not_found", str(exc)) from exc
    except AmbiguousIdError as exc:
        raise MachineError("invalid_input", str(exc)) from exc

    return {
        "team_id": team_id,
        "messages": unread,
        "member": me,
        "count": len(unread),
    }


def machine_result(argv: list[str]) -> dict[str, Any]:
    """`li team <sub> --machine`."""
    from .machine import machine_subcommand

    return machine_subcommand(
        "team",
        argv,
        {
            "list": _machine_list,
            "ls": _machine_list,
            "create": _machine_create,
            "show": _machine_show,
            "send": _machine_send,
            "receive": _machine_receive,
            "recv": _machine_receive,
        },
        without_seam={},
    )

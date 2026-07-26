# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Build a large synthetic ``state.db`` so query cost can be measured at scale.

A studio store grows to gigabytes in normal use, and several read paths change
character only once the store is big -- a plan that looks fine over a hundred
sessions parses hundreds of megabytes of JSON over ten thousand. This builds a
store with the same shape and the same row counts, so those paths can be timed
without ever touching a real one.

Run it directly to write a store::

    uv run python -m tests.fixtures.synthetic_state_db /tmp/big.db --scale 1.0
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# Row counts observed on a store that had reached ~4 GB in ordinary use. Scale
# 1.0 reproduces them; the shape, not the absolute size, is what matters.
REFERENCE_SESSIONS = 12_900
REFERENCE_BRANCHES_PER_SESSION = 1.3
REFERENCE_MESSAGES_PER_BRANCH = 56
REFERENCE_CONTENT_BYTES = 3_000

_STATUSES = ("completed", "running", "failed", "timed_out", "aborted", "cancelled")
_ROLES = ("user", "assistant", "system", "action")
_LION_CLASSES = (2, 3, 1, 4)
_PROJECTS = (None, "lionagi", "khive", "studio", "docs")


@dataclass(frozen=True)
class StoreSpec:
    sessions: int
    branches_per_session: float
    messages_per_branch: int
    content_bytes: int
    write_messages: bool = True

    @classmethod
    def at_scale(cls, scale: float, *, write_messages: bool = True) -> StoreSpec:
        return cls(
            sessions=max(1, int(REFERENCE_SESSIONS * scale)),
            branches_per_session=REFERENCE_BRANCHES_PER_SESSION,
            messages_per_branch=REFERENCE_MESSAGES_PER_BRANCH,
            content_bytes=REFERENCE_CONTENT_BYTES,
            write_messages=write_messages,
        )


def _schema_sql() -> str:
    from lionagi.state.db import _SCHEMA_PATH

    return Path(_SCHEMA_PATH).read_text()


def build_store(path: Path, spec: StoreSpec, *, seed: int = 7) -> dict[str, int]:
    """Write a synthetic store at `path`. Returns the row counts written."""
    rng = random.Random(seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.unlink()

    conn = sqlite3.connect(str(path))
    conn.executescript(_schema_sql())
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = OFF")

    now = time.time()
    filler = "x" * spec.content_bytes
    counts = {"sessions": 0, "branches": 0, "progressions": 0, "messages": 0}

    for batch_start in range(0, spec.sessions, 200):
        batch = range(batch_start, min(batch_start + 200, spec.sessions))
        sessions: list[tuple] = []
        branches: list[tuple] = []
        progressions: list[tuple] = []
        messages: list[tuple] = []

        for i in batch:
            session_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            session_prog_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            # Sessions age backwards from now so updated_at DESC is meaningful.
            updated = now - i * 60.0
            progressions.append((session_prog_id, updated, "[]"))

            n_branches = 1 + (1 if rng.random() < (spec.branches_per_session - 1) else 0)
            for _ in range(n_branches):
                branch_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
                prog_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
                msg_ids = [
                    str(uuid.UUID(int=rng.getrandbits(128), version=4))
                    for _ in range(spec.messages_per_branch)
                ]
                progressions.append((prog_id, updated, json.dumps(msg_ids)))
                branches.append(
                    (
                        branch_id,
                        updated,
                        None,
                        None,
                        f"agent-{i % 17}",
                        session_id,
                        prog_id,
                        None,
                        "claude/claude-sonnet-4-6",
                        "claude",
                        f"role-{i % 5}",
                        "completed",
                        updated,
                        updated + 30,
                    )
                )
                if spec.write_messages:
                    for k, mid in enumerate(msg_ids):
                        messages.append(
                            (
                                mid,
                                updated + k,
                                None,
                                json.dumps({"instruction": filler}),
                                None,
                                f"agent-{i % 17}",
                                None,
                                None,
                                _ROLES[k % len(_ROLES)],
                                _LION_CLASSES[k % len(_LION_CLASSES)],
                            )
                        )

            status = _STATUSES[i % len(_STATUSES)]
            sessions.append(
                (
                    session_id,
                    None,
                    updated,
                    json.dumps({"pid": 999999, "pid_create_time": updated}),
                    f"run-{i}",
                    None,
                    session_prog_id,
                    updated,
                    f"playbook-{i % 11}",
                    f"agent-{i % 17}",
                    "agent",
                    status,
                    updated,
                    updated + 120,
                    updated + 100,
                    "claude/claude-sonnet-4-6",
                    "claude",
                    "high",
                    _PROJECTS[i % len(_PROJECTS)],
                    "cwd" if _PROJECTS[i % len(_PROJECTS)] else None,
                )
            )

        conn.executemany(
            "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
            progressions,
        )
        conn.executemany(
            """INSERT INTO sessions
               (id, cc_session_id, created_at, node_metadata, name, user,
                progression_id, updated_at, playbook_name, agent_name,
                invocation_kind, status, started_at, ended_at, last_message_at,
                model, provider, effort, project, project_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            sessions,
        )
        conn.executemany(
            """INSERT INTO branches
               (id, created_at, node_metadata, user, name, session_id,
                progression_id, system_msg_id, model, provider, agent_name,
                status, started_at, ended_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            branches,
        )
        if messages:
            conn.executemany(
                """INSERT INTO messages
                   (id, created_at, node_metadata, content, embedding,
                    sender, recipient, channel, role, lion_class)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                messages,
            )
        counts["sessions"] += len(sessions)
        counts["branches"] += len(branches)
        counts["progressions"] += len(progressions)
        counts["messages"] += len(messages)
        conn.commit()

    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Where to write the synthetic store")
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Multiple of the reference session count (default 1.0 = ~12,900 sessions)",
    )
    parser.add_argument(
        "--no-messages",
        action="store_true",
        help="Skip message rows; progressions still reference them (much faster to build)",
    )
    args = parser.parse_args(argv)

    spec = StoreSpec.at_scale(args.scale, write_messages=not args.no_messages)
    started = time.perf_counter()
    counts = build_store(args.path, spec)
    elapsed = time.perf_counter() - started
    size_mb = args.path.stat().st_size / 1024 / 1024
    print(f"wrote {args.path} ({size_mb:.0f} MB) in {elapsed:.1f}s")
    for name, n in counts.items():
        print(f"  {name}: {n:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

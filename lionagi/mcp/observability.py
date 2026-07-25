# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Observability and control tools: see what lionagi is doing and act on it.

Every tool here takes typed parameters only — the full surface of the command it
corresponds to, one parameter per flag — and returns structured data rather than
the text the CLI prints for humans, so a caller can branch on a field instead of
matching a string.

The CLI's own functions are the seam wherever one exists; the few that only
print are re-queried here against the same tables. Any helper that prints on the
way to its return value is called with stdout captured, because this server
speaks its protocol on stdout and a stray line would corrupt it.
"""

from __future__ import annotations

import contextlib
import io
import time
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

_ENTITY_TYPES = ("session", "invocation", "show", "play")


# --- parameter descriptions ---------------------------------------------------

_DESCRIPTIONS: dict[str, str] = {
    "entity_id": (
        "Id, or a unique id prefix, of a run, session, play or show — as returned by "
        "``monitor_running``. An ambiguous prefix is refused with the candidates named, rather "
        "than answered for the wrong entity."
    ),
    "wait_run_ids": (
        "schedule_run ids or session ids to wait on, full or by unique prefix. Ids that match "
        "nothing come back under ``unresolved`` instead of holding up the wait."
    ),
    "wait_interval": "Seconds between state.db polls while waiting. Must be positive.",
    "wait_chain": (
        "When a watched run goes terminal, keep following the scheduler's on_success/on_fail "
        "chain children so the wait tracks a chain to its last link (default). Set False to watch "
        "only the ids given."
    ),
    "wait_follow": (
        "Keep discovering schedule_runs created after the initial set drains, instead of "
        "returning once it is empty. It never ends on its own, so it is only useful with a "
        "deliberately short max_wait."
    ),
    "wait_max_wait": (
        "Total wall-clock bound in seconds; must be positive. The call always returns, with "
        "``timed_out=True`` if anything watched is still pending."
    ),
    "kill_entity_id": (
        "Id, or a unique id prefix, of the running session, invocation, play or show to "
        "terminate. An id that matches nothing, is ambiguous, or is already terminal is refused."
    ),
    "kill_reason": (
        "Why the kill happened, recorded on the entity's status transition so the history "
        "explains itself later. Free text; empty by default."
    ),
    "kill_recursive": (
        "Kill direct child entities before the named one. A play kill always reaps its linked "
        "workers regardless; a show kill never reaps its plays, whatever this says."
    ),
    "kill_grace_seconds": "Seconds between SIGTERM and the SIGKILL that follows it.",
    "stale_threshold_seconds": (
        "Only sweep rows that have been claiming to run for longer than this many seconds "
        "(default 3600). Raise it to be conservative on a machine with genuinely long runs."
    ),
    "stale_reason": (
        "Extra text recorded beside the automatic stale-cancel reason on every row this sweep "
        "moves to a terminal status."
    ),
    "stale_grace_seconds": "Seconds between SIGTERM and SIGKILL for rows that still get signalled.",
    "stale_dry_run": "Count what would be swept and write nothing.",
    "dispatch_id": "Id of the dispatch row, as listed by ``dispatch_list``.",
    "ack_token": (
        "The token this dispatch was delivered with, which ``dispatch_show`` reports as its "
        "``ack_token``. A mismatched token is refused, so an ack cannot be guessed."
    ),
    "checkpoint_mode": (
        "How hard to push the WAL back into the database: 'TRUNCATE' (default) frees the WAL file "
        "outright but needs no active readers; 'PASSIVE', 'FULL' and 'RESTART' give up "
        "completeness to avoid blocking. No run data is lost in any mode."
    ),
    "keep_days": (
        "Keep every session updated within this many days; older ones are candidates for deletion."
    ),
    "keep_n": (
        "Always keep this many most-recent sessions, however old they are — the floor that stops "
        "a long-idle machine from pruning its whole history."
    ),
    "prune_dry_run": (
        "Report the session and branch counts that would be deleted and delete nothing (the "
        "message count cannot be previewed and comes back 0)."
    ),
    "stale_hours": (
        "Only reset sessions whose ``started_at`` is older than this many hours (default 24). "
        "A session whose process is still running is left alone regardless."
    ),
    "new_status": (
        "Terminal status to write on each swept session: 'aborted' (default) for a run that was "
        "interrupted, or 'failed' to record it as a failure."
    ),
    "doctor_dry_run": "Count the sessions that would be reset and write nothing.",
}


def _desc(key: str) -> Any:
    """A typed parameter that carries its description into the tool schema.

    A caller decides what a tool can do by reading its schema, so a parameter
    with no description there is a capability they will not find.
    """
    return Field(description=_DESCRIPTIONS[key])


def _quiet(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a printing helper with stdout captured (the protocol owns stdout)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return func(*args, **kwargs)


def _run(coro: Any) -> Any:
    from lionagi.ln.concurrency import run_async

    with contextlib.redirect_stdout(io.StringIO()):
        return run_async(coro)


def _state_db_exists() -> bool:
    from lionagi.state.db import DEFAULT_DB_PATH

    return DEFAULT_DB_PATH.exists()


# ── monitor ───────────────────────────────────────────────────────────────────


async def _snapshot(
    *, since: float | None, entity_type: str | None, project: str | None
) -> dict[str, Any]:
    from lionagi.cli.monitor import _gather_table_rows
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        rows = await _gather_table_rows(db, since=since, entity_type=entity_type, project=project)
    return {"entities": rows, "count": len(rows)}


async def _entity_detail(entity_id: str) -> dict[str, Any]:
    from lionagi.cli.monitor import _fetch_branches, _find_entity, _query_plays_for_show
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        found = await _find_entity(db, entity_id)
        if found is None:
            return {"found": False, "entity_type": None, "entity": None, "branches": []}
        entity_type, row = found
        branches: list[dict[str, Any]] = []
        plays: list[dict[str, Any]] = []
        session_id = row["id"] if entity_type == "session" else row.get("session_id")
        if session_id:
            branches = await _fetch_branches(db, session_id)
        if entity_type == "show":
            plays = await _query_plays_for_show(db, row["id"])
    return {
        "found": True,
        "entity_type": entity_type,
        "entity": row,
        "branches": branches,
        "plays": plays,
    }


def register_observability_tools(mcp: FastMCP) -> dict[str, Any]:
    """Attach the observability/control tools to an MCP server instance.

    Returns the registered functions by name, so a caller (or a test) can reach
    a tool without going through the server's transport.
    """

    @mcp.tool
    def monitor_running(
        since: str | None = None,
        entity_type: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """What is lionagi running right now — every in-flight agent, flow, play and show.

        Reach for this first when you need to know whether work you or anyone else
        started is still going, how long it has been going, and how many agent legs
        it has open. It is the answer to "is anything still running?", "did my flow
        finish?", and "what is this machine busy with?".

        Returns ``{entities: [...], count: n}``. Each entity carries ``id`` (a
        16-char prefix), ``type`` (session/agent/play/show/invocation and the like),
        ``project``, ``status``, ``phase`` (live flow phase, agent name or play
        name), ``elapsed`` (human duration) and ``agents`` (branch count). Pass an
        entity's id to ``monitor_entity`` for the full row.

        This is a point-in-time snapshot. To track progress, call it again — there
        is no live-refresh mode here, since a tool call returns once.

        Args:
            since: Widen the view from "running right now" to "anything touched in
                this window", terminal runs included — a duration like ``30m``,
                ``2h`` or ``7d``. Omit it to see only what is still in flight.
            entity_type: Narrow the listing to one kind of entity: ``session``,
                ``invocation``, ``show`` or ``play``. Anything else is refused.
            project: Filter to one project name, matched exactly — the same name
                that appears in an entity's ``project`` field.
        """
        from lionagi.cli.monitor import _since_timestamp

        if entity_type is not None and entity_type not in _ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {_ENTITY_TYPES}, got {entity_type!r}")
        if not _state_db_exists():
            return {"entities": [], "count": 0}
        cutoff = _since_timestamp(since) if since else None
        return _run(_snapshot(since=cutoff, entity_type=entity_type, project=project))

    @mcp.tool
    def monitor_entity(entity_id: Annotated[str, _desc("entity_id")]) -> dict[str, Any]:
        """Everything recorded about one run, session, play or show, by id or id prefix.

        Use after ``monitor_running`` (or with an id from anywhere else) to see why
        something is slow, stuck or failed: the full state row, plus every agent leg
        on its session, plus a show's child plays.

        Returns ``{found, entity_type, entity, branches, plays}``. ``entity`` is the
        raw state row — status, model, provider, effort, timings, phase, and the
        entity-specific columns. ``branches`` is one entry per agent leg with
        ``name``, ``status``, ``started_at``, ``ended_at``. ``plays`` is populated
        only for a show. ``found`` is False when no row matches.

        Raises if the prefix is ambiguous, naming the candidates, rather than
        answering about a run you did not mean.
        """
        if not _state_db_exists():
            return {"found": False, "entity_type": None, "entity": None, "branches": []}
        return _run(_entity_detail(entity_id))

    @mcp.tool
    def monitor_wait_for_runs(
        run_ids: Annotated[list[str], _desc("wait_run_ids")],
        interval: Annotated[float, _desc("wait_interval")] = 3.0,
        chain: Annotated[bool, _desc("wait_chain")] = True,
        follow: Annotated[bool, _desc("wait_follow")] = False,
        max_wait: Annotated[float, _desc("wait_max_wait")] = 900.0,
    ) -> dict[str, Any]:
        """Block until named scheduled runs (or sessions) finish, then report outcomes.

        This is the synchronisation primitive: use it when the next thing you do
        depends on work already in flight. It polls state.db and returns as soon as
        every watched id is terminal, so you do not have to re-poll a snapshot in a
        loop.

        ``run_ids`` are schedule_run ids or session ids, full or prefix. By default
        (``chain=True``) a watched run going terminal keeps following the
        scheduler's on_success/on_fail chain children, so the wait tracks a chain to
        its last link instead of returning at the first hop; set ``chain=False`` to
        watch only the literal ids. ``interval`` is the poll period in seconds.
        ``max_wait`` bounds total wall-clock and must be positive — the call always
        returns, with ``timed_out=True`` if work is still pending. ``follow`` keeps
        discovering schedule_runs created after the initial set drains, until
        ``max_wait`` expires; it never ends on its own, so it is only useful with a
        deliberately short ``max_wait``.

        Returns ``{finished, timed_out, all_succeeded, runs, sessions, unresolved,
        still_pending}``. ``runs``/``sessions`` are the terminal rows observed;
        ``unresolved`` lists ids that matched nothing; ``all_succeeded`` is True only
        when everything watched resolved with a success exit code.
        """
        if max_wait <= 0:
            raise ValueError("max_wait must be positive: an unbounded wait would never return")
        if interval <= 0:
            raise ValueError("interval must be positive")
        if not run_ids:
            raise ValueError("run_ids must name at least one schedule_run or session id")
        if not _state_db_exists():
            return {
                "finished": False,
                "timed_out": False,
                "all_succeeded": False,
                "runs": [],
                "sessions": [],
                "unresolved": list(run_ids),
                "still_pending": [],
            }
        return _run(
            _wait_for_runs(
                run_ids, interval=interval, chain=chain, follow=follow, max_wait=max_wait
            )
        )

    # ── kill ──────────────────────────────────────────────────────────────────

    @mcp.tool
    def kill_entity(
        entity_id: Annotated[str, _desc("kill_entity_id")],
        reason: Annotated[str, _desc("kill_reason")] = "",
        recursive: Annotated[bool, _desc("kill_recursive")] = False,
        grace_seconds: Annotated[float, _desc("kill_grace_seconds")] = 5.0,
    ) -> dict[str, Any]:
        """Terminate a running run/session/play/show. The work stops and does not resume.

        What dies: the OS process behind the named entity is sent SIGTERM, then
        SIGKILL after ``grace_seconds``. Whatever that agent was mid-way through —
        an unfinished edit, an unwritten artifact, an in-flight model call — is lost.
        With ``recursive``, direct child entities are killed first (play kills always
        reap their linked workers, whatever ``recursive`` says).

        What survives: everything already persisted. The state row is not deleted;
        it moves to a terminal status — ``cancelled`` for sessions and invocations,
        ``blocked`` for plays, ``aborted`` for shows — with ``reason`` recorded in
        status transitions. Messages, branches and artifacts written before the kill
        stay on disk and in state.db.

        Irreversible: there is no resume. A killed run has to be started again from
        the beginning, and any partial in-memory work is gone.

        A show kill only marks the show row terminal; it does not reap the show's
        plays or their workers. Kill those play or session ids directly.

        Returns ``{ok, entity_type, entity_id, killed: [...], blocked: [...]}``.
        Each entry carries ``entity_type``, ``entity_id``, ``pid`` and ``signal``;
        ``blocked`` holds entries whose pid could not be confirmed as the recorded
        process (``signal="identity_mismatch"``), which were deliberately not
        signalled. Raises if the id matches nothing, is ambiguous, or is already
        terminal.
        """
        return _run(
            _kill_entity(entity_id, reason=reason, recursive=recursive, grace_seconds=grace_seconds)
        )

    @mcp.tool
    def kill_stale_entities(
        threshold_seconds: Annotated[int, _desc("stale_threshold_seconds")] = 3600,
        reason: Annotated[str, _desc("stale_reason")] = "",
        grace_seconds: Annotated[float, _desc("stale_grace_seconds")] = 5.0,
        dry_run: Annotated[bool, _desc("stale_dry_run")] = False,
    ) -> dict[str, Any]:
        """Clean up rows still marked running whose process is already dead.

        A crash or SIGKILL between start and teardown leaves a session, invocation,
        play or show claiming to be running forever, which makes ``monitor_running``
        lie. This sweep cancels exactly those: rows older than ``threshold_seconds``
        whose recorded pid is gone (or was recycled by an unrelated process), plus
        plays whose worker session already ended and shows whose plays are all
        terminal.

        What dies: nothing live. A pid that is still alive and verifiably the
        recorded process is skipped, as is one whose identity cannot be verified.
        Only bookkeeping changes — the swept rows move to a terminal status with a
        stale auto-cancel reason. That status change is not reversible, but no
        running work is stopped by it.

        ``dry_run=True`` counts what would be swept and writes nothing.
        ``grace_seconds`` is the SIGTERM-to-SIGKILL delay for the signalling path.

        Returns ``{cancelled, skipped_recent, skipped_live_pid,
        skipped_unverifiable_pid, dry_run}``.
        """
        from lionagi.cli.kill import sweep_stale

        counts = _run(
            sweep_stale(
                threshold_seconds=threshold_seconds,
                user_reason=reason,
                grace_seconds=grace_seconds,
                dry_run=dry_run,
            )
        )
        return {**counts, "dry_run": dry_run}

    # ── stats ─────────────────────────────────────────────────────────────────

    @mcp.tool
    def stats_runs(
        since: str = "7d",
        group_by: list[str] | None = None,
    ) -> dict[str, Any]:
        """Aggregate run counts over a time window — how much ran, where, and how it went.

        Use this to answer questions about volume and reliability rather than about
        one run: which projects are busy, which models or agents are being used,
        what fraction of runs failed this week. It reads state.db read-only and
        never writes to it.

        Returns ``{since, group_by, rows}`` where each row carries the requested
        group keys plus ``run_count``, ``completed``, ``failed``, and ISO-8601
        ``first_at``/``last_at``. An empty ``rows`` means nothing ran in the window.

        Args:
            since: Window over runs' last-update time — a duration like ``30m``,
                ``6h`` or ``7d`` (default ``7d``). Must be positive.
            group_by: Columns to aggregate by, any combination of ``project``,
                ``kind``, ``agent``, ``model`` and ``status`` (default
                ``["project", "kind"]``).
        """
        from lionagi.cli.monitor import _since_timestamp
        from lionagi.cli.stats import (
            _reject_non_positive_since,
            _rows_for_json,
            _run_stats_runs,
            _validate_group_by,
        )

        keys = _validate_group_by(",".join(group_by) if group_by else "project,kind")
        _reject_non_positive_since(since)
        cutoff = _since_timestamp(since)
        rows = _run(_run_stats_runs(since=cutoff, group_by=keys))
        return {"since": since, "group_by": keys, "rows": _rows_for_json(rows, keys)}

    # ── doctor ────────────────────────────────────────────────────────────────

    @mcp.tool
    def doctor() -> dict[str, Any]:
        """Check whether this lionagi install can actually run anything.

        Reach for this when a run fails for a reason that smells environmental —
        an import error, a permission error, a scheduled action that never fires —
        before assuming the problem is in the prompt or the code. It reports which
        lionagi is installed and from where, the Python and virtualenv in use, the
        import chain the CLI traverses, core dependency importability, whether the
        Studio daemon is reachable, and whether ``~/.lionagi`` is writable.

        Returns ``{ok, failed, warned, checks}``. ``checks`` maps check name to
        ``{status, detail}`` where status is ``ok``, ``warn`` or ``fail``; ``ok`` is
        False when anything failed. A ``warn`` is informational — an unreachable
        Studio daemon is normal unless scheduled or agent-spawn actions are in play.
        """
        from lionagi.cli.doctor import collect_checks

        checks = _quiet(collect_checks)
        failed = [name for name, r in checks.items() if r["status"] == "fail"]
        warned = [name for name, r in checks.items() if r["status"] == "warn"]
        return {"ok": not failed, "failed": failed, "warned": warned, "checks": checks}

    # ── dispatch ──────────────────────────────────────────────────────────────

    @mcp.tool
    def dispatch_list(
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List durable outbox rows — the notifications and hand-offs lionagi owes someone.

        Dispatches are enqueued by schedule actions and delivered by the Studio
        daemon's scheduler tick. Read this when a scheduled notification did not
        arrive, or to see what delivery is backed up: rows sitting in ``pending``
        or ``dead_letter`` are the evidence.

        Returns ``{rows, count}`` with each row's ``id``, ``kind``, ``deliver_to``,
        ``status``, ``attempt`` and ``created_at`` (epoch seconds) among its columns.

        Args:
            status: Return only rows in this delivery state: ``pending``,
                ``delivering``, ``delivered``, ``acked``, ``dead_letter`` or
                ``expired``. Matched exactly.
            limit: Maximum number of dispatch rows to return.
        """
        return _run(_dispatch_list(status=status, limit=limit))

    @mcp.tool
    def dispatch_show(dispatch_id: Annotated[str, _desc("dispatch_id")]) -> dict[str, Any]:
        """Full record of one dispatch, including its payload and ack state.

        Use when ``dispatch_list`` shows a row worth explaining: this returns the
        whole row — payload, attempt count, next attempt time, ack token and
        delivery status — so you can tell a delivery that failed from one nobody
        acknowledged.

        Returns ``{found, dispatch}``; ``dispatch`` is None when the id matches
        nothing.
        """
        return _run(_dispatch_show(dispatch_id))

    @mcp.tool
    def dispatch_ack(
        dispatch_id: Annotated[str, _desc("dispatch_id")],
        ack_token: Annotated[str, _desc("ack_token")],
    ) -> dict[str, Any]:
        """Acknowledge a delivered dispatch that is waiting on the consumer's confirmation.

        An ack-required dispatch stays open until its consumer confirms receipt with
        the token it was handed. Acking closes it so the scheduler stops retrying.

        The write is compare-and-swap: if the row's status changed concurrently the
        ack is rejected rather than applied, and ``applied`` comes back False.

        Returns ``{applied, dispatch_id}``.
        """
        return _run(_dispatch_ack(dispatch_id, ack_token))

    @mcp.tool
    def dispatch_retry(dispatch_id: Annotated[str, _desc("dispatch_id")]) -> dict[str, Any]:
        """Force an immediate re-delivery of a dead-lettered or expired dispatch.

        Use after fixing whatever made delivery fail. The row is put back in line
        for the next scheduler tick instead of waiting out its backoff.

        Rejected (``applied=False``) if the row's status changed concurrently, or if
        it is not in a state that can be retried.

        Returns ``{applied, dispatch_id}``.
        """
        return _run(_dispatch_retry(dispatch_id))

    @mcp.tool
    def dispatch_purge(
        dispatch_id: str | None = None,
        status: str | None = None,
        before: float | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete dispatch rows for good. The rows are gone; delivery never happens.

        Two modes. With ``dispatch_id``: delete that single row whatever its status.
        Without it: bulk-delete by ``status`` and/or ``before`` (at least one is
        required, so this can never mass-delete the whole outbox by accident).

        What dies: the row itself, permanently — there is no undo and no archive
        beyond the ``dispatch_purge`` entry written to admin_events. A purged
        pending dispatch will never be delivered.

        Scoping rule worth knowing: an explicit ``status`` is honoured exactly as
        given, including ``pending`` and ``delivering`` — naming an in-flight status
        is read as deliberate intent. A bare ``before`` (epoch seconds, matched
        against ``updated_at``) with no ``status`` is scoped to terminal statuses
        only and never sweeps in-flight rows.

        Returns ``{deleted, dry_run, ...}``: for a single row, ``found`` and
        ``status``; for a bulk purge, ``total`` and a per-status breakdown.

        Args:
            dispatch_id: Delete this one row whatever its status. Omit it to
                bulk-purge by status and/or before instead.
            status: Bulk-delete rows in this state (``pending``, ``delivering``,
                ``delivered``, ``acked``, ``dead_letter``, ``expired``). Naming an
                in-flight state is honoured as deliberate: those rows will never be
                delivered.
            before: Bulk-delete rows whose ``updated_at`` is older than this
                epoch-seconds timestamp. On its own, with no status, it is scoped
                to terminal states only and never sweeps in-flight rows.
            dry_run: Report the counts that would be deleted and delete nothing.
        """
        if dispatch_id is None and status is None and before is None:
            raise ValueError(
                "specify dispatch_id, or status/before for a bulk purge: "
                "a bare purge would delete the whole outbox"
            )
        if dispatch_id is not None:
            return _run(_dispatch_purge_one(dispatch_id, dry_run=dry_run))
        return _run(_dispatch_purge_bulk(status=status, before=before, dry_run=dry_run))

    # ── state ─────────────────────────────────────────────────────────────────

    @mcp.tool
    def state_list_sessions(
        limit: int = 50,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List sessions recorded in state.db, most recently updated first.

        This is the history view, where ``monitor_running`` is the live view: it
        includes finished runs. Use it to find a past session's id — to read its
        detail, or to see how many runs a status like ``failed`` has accumulated.

        Returns ``{sessions, count}`` with ``id``, ``name``, ``status``,
        ``updated_at`` (epoch seconds) and ``branch_count`` per session.

        Args:
            limit: Maximum number of sessions to return, most recently updated
                first.
            status: Return only sessions in this state: ``running``, ``completed``,
                ``failed`` or ``aborted``.
        """
        if not _state_db_exists():
            return {"sessions": [], "count": 0}
        return _run(_list_sessions(limit=limit, status=status))

    @mcp.tool
    def state_stats() -> dict[str, Any]:
        """Size, row counts and SQLite settings for state.db — is the store healthy?

        Reach for this when runs feel slow, writes seem to be contending, or disk is
        filling: a large WAL means checkpoints are not landing, and a skewed session
        status distribution (many stuck ``running``) means crashed runs need
        ``kill_stale_entities``.

        Returns ``{db_path, db_bytes, wal_bytes, row_counts, sessions_by_status,
        pragmas}``. ``row_counts`` covers messages, progressions, sessions,
        branches, definitions, shows and plays; ``pragmas`` reports journal_mode,
        wal_autocheckpoint, busy_timeout, synchronous and foreign_keys.
        """
        return _run(_state_stats())

    @mcp.tool
    def state_checkpoint(
        mode: Annotated[str, _desc("checkpoint_mode")] = "TRUNCATE",
    ) -> dict[str, Any]:
        """Flush the write-ahead log back into state.db to reclaim WAL disk.

        Use when ``state_stats`` shows a large ``wal_bytes``. ``TRUNCATE`` (default)
        is the most aggressive and frees the WAL file outright, but only succeeds
        when no readers are active; ``PASSIVE``, ``FULL`` and ``RESTART`` trade
        completeness for not blocking. No run data is lost in any mode.

        Returns ``{mode, result}`` where ``result`` is the checkpoint's
        busy/log_pages/checkpointed summary.
        """
        modes = ("PASSIVE", "FULL", "RESTART", "TRUNCATE")
        if mode not in modes:
            raise ValueError(f"mode must be one of {modes}, got {mode!r}")
        from lionagi.cli.state import _checkpoint

        return {"mode": mode, "result": _run(_checkpoint(mode))}

    @mcp.tool
    def state_vacuum() -> dict[str, Any]:
        """Rebuild state.db to reclaim space freed by deletes. Holds an exclusive lock.

        Run after ``state_prune``. Nothing is deleted here — VACUUM only compacts —
        but the exclusive lock blocks every reader and writer for the duration, so
        do not run it while agents are working.

        Returns ``{ok, bytes_before, bytes_after}``.
        """
        from lionagi.cli.state import _vacuum
        from lionagi.state.db import DEFAULT_DB_PATH

        before = DEFAULT_DB_PATH.stat().st_size if DEFAULT_DB_PATH.exists() else 0
        _run(_vacuum())
        after = DEFAULT_DB_PATH.stat().st_size if DEFAULT_DB_PATH.exists() else 0
        return {"ok": True, "bytes_before": before, "bytes_after": after}

    @mcp.tool
    def state_prune(
        keep_days: Annotated[int, _desc("keep_days")] = 30,
        keep_n: Annotated[int, _desc("keep_n")] = 100,
        dry_run: Annotated[bool, _desc("prune_dry_run")] = False,
    ) -> dict[str, Any]:
        """Delete old sessions and their history from state.db. The transcripts are gone.

        What dies: sessions not updated within ``keep_days``, except the ``keep_n``
        most recent, which are always kept. Their branches cascade away, and
        messages no longer referenced by any progression are deleted with them.
        This is permanent — those transcripts cannot be read back afterwards, though
        run artifacts on disk under ``~/.lionagi/runs`` are untouched.

        ``dry_run=True`` reports the session and branch counts that would go and
        deletes nothing (message count cannot be previewed and comes back 0).

        Returns ``{sessions, branches, messages, dry_run}``.
        """
        from lionagi.cli.state import _prune

        result = _run(_prune(keep_days=keep_days, keep_n=keep_n, dry_run=dry_run))
        return {**result, "dry_run": dry_run}

    @mcp.tool
    def state_doctor(
        stale_hours: Annotated[int, _desc("stale_hours")] = 24,
        new_status: Annotated[str, _desc("new_status")] = "aborted",
        dry_run: Annotated[bool, _desc("doctor_dry_run")] = False,
    ) -> dict[str, Any]:
        """Reset sessions stuck at running after a crash, so the history stops lying.

        Narrower than ``kill_stale_entities``: this touches session rows only, and
        only those whose ``started_at`` is older than ``stale_hours`` AND whose
        recorded process is not running — an actively running CLI is left alone even
        if its session started long ago.

        What dies: nothing live; only the status column moves, to ``aborted`` or
        ``failed``. The change is not reversible.

        ``dry_run=True`` counts and writes nothing.

        Returns ``{running, swept, skipped, new_status, dry_run}``.
        """
        if new_status not in ("aborted", "failed"):
            raise ValueError(f"new_status must be 'aborted' or 'failed', got {new_status!r}")
        from lionagi.cli.state import _doctor

        result = _run(_doctor(stale_hours=stale_hours, dry_run=dry_run, new_status=new_status))
        return {**result, "new_status": new_status, "dry_run": dry_run}

    @mcp.tool
    def state_import_runs() -> dict[str, Any]:
        """Backfill state.db from run directories on disk, so old runs become queryable.

        Scans ``~/.lionagi/runs`` for manifests and loads their sessions, branches
        and messages. Idempotent — already-imported sessions are skipped — so it is
        safe to run repeatedly. Use when ``monitor_entity`` or
        ``state_list_sessions`` cannot find a run you know happened.

        Returns ``{sessions, branches, messages, skipped, errors}``.
        """
        from lionagi.cli.state import _import_runs

        return _run(_import_runs())

    @mcp.tool
    def state_import_teams() -> dict[str, Any]:
        """Backfill team inboxes from JSON files into state.db so team traffic is queryable.

        Scans ``~/.lionagi/teams/*.json`` and inserts each team and its messages.
        Idempotent: teams already present (matched by id) are left alone. Run once
        after upgrading; the runtime keeps working off the JSON files either way.

        Returns ``{teams, messages, skipped_teams, errors}``.
        """
        from lionagi.cli.state import _import_teams

        return _run(_import_teams())

    tools = (
        monitor_running,
        monitor_entity,
        monitor_wait_for_runs,
        kill_entity,
        kill_stale_entities,
        stats_runs,
        doctor,
        dispatch_list,
        dispatch_show,
        dispatch_ack,
        dispatch_retry,
        dispatch_purge,
        state_list_sessions,
        state_stats,
        state_checkpoint,
        state_vacuum,
        state_prune,
        state_doctor,
        state_import_runs,
        state_import_teams,
    )
    return {fn.__name__: fn for fn in tools}


# ── async implementations ─────────────────────────────────────────────────────


async def _wait_for_runs(
    run_ids: list[str],
    *,
    interval: float,
    chain: bool,
    follow: bool,
    max_wait: float,
) -> dict[str, Any]:
    """Poll watched runs to terminal state, following chain children by default."""
    import anyio

    from lionagi.cli._util import EXIT_CODE_BY_STATUS
    from lionagi.cli.monitor import (
        _advance_chains,
        _new_chain_state,
        _poll_pending_once,
        _poll_pending_sessions_once,
        _query_schedule_runs_since,
        _resolve_watched_runs,
    )
    from lionagi.state.db import StateDB

    deadline = time.monotonic() + max_wait
    schedule_names: dict[str, str] = {}
    done: list[dict[str, Any]] = []
    session_done: list[dict[str, Any]] = []

    async with StateDB() as db:
        pending, session_pending, unresolved = await _resolve_watched_runs(db, run_ids)
        total_watched, total_sessions = len(pending), len(session_pending)
        chain_state = _new_chain_state(pending, chain=chain)
        processed = 0

        def open_() -> bool:
            return bool(pending or session_pending or chain_state["awaiting_grace"])

        while open_() and time.monotonic() < deadline:
            await _poll_pending_once(db, pending, schedule_names, done)
            await _poll_pending_sessions_once(db, session_pending, session_done)
            processed = await _advance_chains(
                db, pending, done, chain_state=chain_state, processed=processed
            )
            if not open_():
                break
            await anyio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

        followed: list[dict[str, Any]] = []
        if follow:
            baseline = time.time()
            follow_pending: dict[str, dict[str, Any]] = {}
            while time.monotonic() < deadline:
                new_rows = await _query_schedule_runs_since(db, baseline)
                for row in new_rows:
                    follow_pending.setdefault(row["id"], row)
                if new_rows:
                    baseline = max(baseline, *(r["created_at"] for r in new_rows))
                if follow_pending:
                    await _poll_pending_once(db, follow_pending, schedule_names, followed)
                await anyio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    resolved_roots = chain_state["resolved_roots"]
    chain_tail_exit = chain_state["chain_tail_exit"]
    complete = len(resolved_roots) >= total_watched and len(session_done) >= total_sessions
    sessions_ok = all(EXIT_CODE_BY_STATUS.get(r["status"], 1) == 0 for r in session_done)
    runs_ok = all(chain_tail_exit.get(root) == 0 for root in resolved_roots)
    return {
        "finished": complete and not unresolved,
        "timed_out": not complete,
        "all_succeeded": complete and not unresolved and runs_ok and sessions_ok,
        "runs": done + followed,
        "sessions": session_done,
        "unresolved": unresolved,
        "still_pending": sorted(set(pending) | set(session_pending)),
    }


async def _kill_entity(
    entity_id: str, *, reason: str, recursive: bool, grace_seconds: float
) -> dict[str, Any]:
    from lionagi.cli._util import resolve_entity
    from lionagi.cli.kill import _kill_one, _list_running_children, _walk_running_children
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        resolved = await resolve_entity(db, entity_id)
        if resolved is None:
            raise ValueError(f"entity not found for id: {entity_id!r}")
        _table, entity_type, row = resolved
        killable = "active" if entity_type == "show" else "running"
        if row.get("status") != killable:
            raise ValueError(
                f"{entity_type} {row['id'][:12]} is already terminal "
                f"(status={row.get('status')!r}) — nothing to kill"
            )

        if entity_type == "play":
            children = await _walk_running_children(db, entity_type, row["id"])
        elif entity_type == "show" or not recursive:
            children = []
        else:
            children = await _list_running_children(db, entity_type, row["id"])

        results = []
        for _child_table, child_type, child_row in children:
            results.append(
                await _kill_one(
                    db,
                    child_type,
                    child_row["id"],
                    child_row,
                    user_reason=reason,
                    grace_seconds=grace_seconds,
                )
            )
        results.append(
            await _kill_one(
                db,
                entity_type,
                row["id"],
                row,
                user_reason=reason,
                grace_seconds=grace_seconds,
            )
        )

    blocked = [r for r in results if r["signal"] == "identity_mismatch"]
    return {
        "ok": not blocked,
        "entity_type": entity_type,
        "entity_id": row["id"],
        "killed": [r for r in results if r["signal"] != "identity_mismatch"],
        "blocked": blocked,
    }


async def _dispatch_list(*, status: str | None, limit: int) -> dict[str, Any]:
    from lionagi.dispatch import list_dispatches
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        rows = await list_dispatches(db, status=status, limit=limit)
    return {"rows": rows, "count": len(rows)}


async def _dispatch_show(dispatch_id: str) -> dict[str, Any]:
    from lionagi.dispatch import get_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        row = await get_dispatch(db, dispatch_id)
    return {"found": row is not None, "dispatch": row}


async def _dispatch_ack(dispatch_id: str, ack_token: str) -> dict[str, Any]:
    from lionagi.dispatch import ack_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        applied = await ack_dispatch(db, dispatch_id, ack_token)
    return {"applied": applied, "dispatch_id": dispatch_id}


async def _dispatch_retry(dispatch_id: str) -> dict[str, Any]:
    from lionagi.dispatch import retry_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        applied = await retry_dispatch(db, dispatch_id)
    return {"applied": applied, "dispatch_id": dispatch_id}


async def _dispatch_purge_one(dispatch_id: str, *, dry_run: bool) -> dict[str, Any]:
    from lionagi.dispatch import get_dispatch, purge_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        if dry_run:
            row = await get_dispatch(db, dispatch_id)
            if row is None:
                return {"found": False, "deleted": 0, "dry_run": True, "status": None}
            await db.insert_admin_event(
                action="dispatch_purge",
                target_id=dispatch_id,
                details={
                    "dispatch_id": dispatch_id,
                    "dry_run": True,
                    "status": row["status"],
                    "total": 1,
                },
                actor="li_dispatch_purge",
            )
            return {"found": True, "deleted": 0, "dry_run": True, "status": row["status"]}
        deleted = await purge_dispatch(db, dispatch_id, actor="li_dispatch_purge")
    return {"found": bool(deleted), "deleted": int(bool(deleted)), "dry_run": False}


async def _dispatch_purge_bulk(
    *, status: str | None, before: float | None, dry_run: bool
) -> dict[str, Any]:
    from lionagi.dispatch import purge_dispatches
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        result = await purge_dispatches(
            db, status=status, before=before, dry_run=dry_run, actor="li_dispatch_purge"
        )
    by_status = {k: v for k, v in result.items() if k not in ("total", "dry_run")}
    return {
        "deleted": result["total"],
        "total": result["total"],
        "dry_run": dry_run,
        "by_status": by_status,
    }


async def _list_sessions(*, limit: int, status: str | None) -> dict[str, Any]:
    from lionagi.state.db import StateDB

    query = (
        "SELECT id, name, status, updated_at, "  # noqa: S608
        "(SELECT COUNT(*) FROM branches WHERE session_id = sessions.id) AS branch_count "
        "FROM sessions"
    )
    params: list[Any] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    async with StateDB() as db:
        rows = await db.fetch_all(query, params)
    return {"sessions": rows, "count": len(rows)}


_STATS_TABLES = (
    "messages",
    "progressions",
    "sessions",
    "branches",
    "definitions",
    "shows",
    "plays",
)
_STATS_PRAGMAS = (
    "journal_mode",
    "wal_autocheckpoint",
    "busy_timeout",
    "synchronous",
    "foreign_keys",
)


async def _state_stats() -> dict[str, Any]:
    from sqlalchemy import text

    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    db_path = DEFAULT_DB_PATH
    wal_path = db_path.with_name(db_path.name + "-wal")
    out: dict[str, Any] = {
        "db_path": str(db_path),
        "db_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
        "row_counts": {},
        "sessions_by_status": {},
        "pragmas": {},
    }
    if not db_path.exists():
        return out

    async with StateDB() as db:
        for table in _STATS_TABLES:
            async with db._read() as conn:
                row = (
                    (await conn.execute(text(f"SELECT COUNT(*) AS n FROM {table}")))  # noqa: S608
                    .mappings()
                    .first()
                )
            out["row_counts"][table] = row["n"]

        async with db._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT COALESCE(status, '(null)') AS s, COUNT(*) AS n "
                            "FROM sessions GROUP BY status ORDER BY n DESC"
                        )
                    )
                )
                .mappings()
                .all()
            )
        out["sessions_by_status"] = {r["s"]: r["n"] for r in rows}

        for pragma in _STATS_PRAGMAS:
            async with db._read() as conn:
                row = (await conn.execute(text(f"PRAGMA {pragma}"))).first()
            out["pragmas"][pragma] = row[0] if row else None
    return out

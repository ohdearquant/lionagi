# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Host-side persistence bridge for a flow running inside a sandbox (ADR-0083).

``SandboxBridge`` is the mirror image of the local live-persist machinery
(``cli/orchestrate/_orchestration.py``: ``start_live_persist`` / the
``_ensure_branch_row`` + ``_on_message`` hook / ``stop_live_persist``), driven by
serialized wire events that arrive over the sandbox's stdout channel instead of an
in-process hook bus. The host owns the ``StateDB`` and the session row; the
sandboxed flow only EMITS events. The point of the symmetry is that a sandboxed
run lands the *same* rows a local run would — so ``li monitor``, Studio, and
``li kill`` treat it identically, with zero new observability code.

Persistence-only by design: control (cancel/break) is the reverse channel and is
owned by the runner that holds the live sandbox handle (ADR-0083 Phase 3), not by
this bridge.

The write sequences below are deliberate copies of ``_on_message`` /
``_ensure_branch_row`` rather than a shared helper: the local path is the source
of truth, and ``tests/tools/test_sandbox_bridge.py`` pins the two to identical
StateDB output so they cannot drift. If/when the local path adopts a shared
``persist_message`` op, both should call it; until then the regression test is the
contract.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from lionagi.state.db import StateDB
from lionagi.tools.sandbox_protocol import decode_line

__all__ = ("SandboxBridge",)

_log = logging.getLogger("lionagi.cli")


class SandboxBridge:
    """Replays a sandboxed flow's persistence events into the host ``state.db``.

    Lifecycle::

        bridge = SandboxBridge(invocation_kind="flow", model="openrouter/…",
                               provider="pi", project="lionagi")
        await bridge.start()                       # session row, status=running
        async for line in sandbox_stdout:          # Phase-2 runner drains here
            await bridge.feed_line(line)
        await bridge.finish(status="completed")    # bookmarks + terminal status

    The session is visible in ``li monitor`` the instant ``start()`` returns —
    before the sandbox emits anything — because the host already knows the run
    config and owns the row.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        invocation_kind: str = "flow",
        model: str | None = None,
        provider: str | None = None,
        effort: str | None = None,
        project: str | None = None,
        project_source: str | None = None,
        agent_name: str | None = None,
        playbook_name: str | None = None,
        artifacts_path: str | None = None,
        node_metadata: dict[str, Any] | None = None,
        db: StateDB | None = None,
        db_path: str | None = None,
    ) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.invocation_kind = invocation_kind
        self.model = model
        self.provider = provider
        self.effort = effort
        self.project = project
        self.project_source = project_source
        self.agent_name = agent_name
        self.playbook_name = playbook_name
        self.artifacts_path = artifacts_path
        self.node_metadata = node_metadata
        # ``db`` lets a caller inject a shared connection; otherwise the bridge
        # opens (and owns, and closes) its own — mirroring start_live_persist.
        self._db = db
        self._owns_db = db is None
        self._db_path = db_path

        self.session_prog_id = str(uuid.uuid4())
        # branch_id -> branch progression id (host-assigned, like _register_branch_hook)
        self._branch_prog_ids: dict[str, str] = {}
        self._started = False

    # ── lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> str:
        """Open the DB, create the session progression + ``running`` session row.

        Returns the session id. On setup failure the DB is closed (if we own it)
        so the aiosqlite worker thread does not leak, and the exception is
        re-raised — a session that can't be created is a hard error for the
        caller (unlike the per-message hook, which must never abort a run).
        """
        if self._started:
            return self.session_id
        try:
            if self._db is None:
                self._db = StateDB(self._db_path) if self._db_path else StateDB()
                await self._db.open()
            await self._db.create_progression(self.session_prog_id)
            await self._db.create_session(
                {
                    "id": self.session_id,
                    "progression_id": self.session_prog_id,
                    "name": self.agent_name,
                    "invocation_kind": self.invocation_kind,
                    "playbook_name": self.playbook_name,
                    "agent_name": self.agent_name,
                    "artifacts_path": self.artifacts_path,
                    "node_metadata": self.node_metadata,
                    "status": "running",
                    "started_at": time.time(),
                    # ADR-0022 provenance — what the sandboxed run actually used.
                    "model": self.model,
                    "provider": self.provider,
                    "effort": self.effort,
                    # ADR-0026 project organization.
                    "project": self.project,
                    "project_source": self.project_source,
                }
            )
            self._started = True
            return self.session_id
        except Exception:
            if self._owns_db and self._db is not None:
                try:
                    await self._db.close()
                except Exception as close_exc:  # noqa: BLE001
                    _log.warning(
                        "sandbox bridge: db.close after start failure failed: %s", close_exc
                    )
                self._db = None
            raise

    async def feed_line(self, line: str) -> bool:
        """Decode one stdout line and apply it. Returns True if it was an event.

        Non-event lines (ordinary stdout, ``@@SIG@@`` summaries, log noise) return
        False so the Phase-2 runner can route them elsewhere (e.g. a live log tail).
        """
        ev = decode_line(line)
        if ev is None:
            return False
        await self.on_event(ev)
        return True

    async def on_event(self, ev: dict[str, Any]) -> None:
        """Dispatch one persistence event. Never raises — a bad event is logged
        and dropped so the stream survives (mirrors ``_on_message``'s swallow)."""
        try:
            kind = ev.get("ev")
            if kind == "message":
                await self._on_message(ev)
            elif kind == "branch":
                await self._on_branch(ev)
            elif kind == "phase":
                await self._on_phase(ev)
            # unknown kinds are ignored for forward-compatibility
        except Exception as exc:
            _log.warning(
                "sandbox bridge: event apply failed (ev=%s): %s",
                ev.get("ev"),
                exc,
                exc_info=True,
            )

    async def finish(
        self, *, status: str = "completed", exception: BaseException | None = None
    ) -> str:
        """Write session bookmarks + terminal status, then close the DB.

        Mirrors ``stop_live_persist``'s core: first/last message bookmarks from
        the session progression, then ``update_status`` with the same reason
        resolution the local path uses (so reason codes match). The close lives in
        ``finally`` so the aiosqlite worker is always reclaimed. Returns the
        terminal status.
        """
        if self._db is None:
            return status
        try:
            all_msgs = await self._db.get_progression(self.session_prog_id)
            update_kwargs: dict[str, Any] = {"ended_at": time.time()}
            if all_msgs:
                update_kwargs["first_msg_id"] = all_msgs[0]
                update_kwargs["last_msg_id"] = all_msgs[-1]
            await self._db.update_session(self.session_id, **update_kwargs)

            # Same reason resolution as stop_live_persist → identical reason codes.
            from lionagi.cli.agent import _resolve_run_reason

            reason_code, reason_summary, evidence_refs = _resolve_run_reason(
                status=status, exception=exception
            )
            metadata: dict[str, Any] | None = None
            if exception is not None:
                metadata = {"exception_class": type(exception).__name__}
            await self._db.update_status(
                "session",
                self.session_id,
                new_status=status,
                reason_code=reason_code,
                reason_summary=reason_summary,
                evidence_refs=evidence_refs,
                source="sandbox-bridge",
                actor=self.session_id,
                metadata=metadata,
            )
        except Exception as exc:
            _log.warning("sandbox bridge: finish failed: %s", exc, exc_info=True)
            return status
        finally:
            if self._owns_db:
                try:
                    await self._db.close()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("sandbox bridge: db.close failed: %s", exc, exc_info=True)
                self._db = None
        return status

    # ── event handlers (mirror _ensure_branch_row / _on_message) ─────────

    async def _on_branch(self, ev: dict[str, Any]) -> None:
        """Create a branch row + its progression — mirror ``_ensure_branch_row``.

        The system message (if any) is inserted first so the branch's
        ``system_msg_id`` FK resolves, exactly as the local path does.
        """
        branch = ev.get("branch") or {}
        branch_id = branch.get("id")
        if not branch_id:
            return
        await self._ensure_branch(
            branch_id,
            branch_dict=branch,
            system_msg=ev.get("system_msg"),
            model=ev.get("model"),
            provider=ev.get("provider"),
        )

    async def _ensure_branch(
        self,
        branch_id: str,
        *,
        branch_dict: dict[str, Any] | None = None,
        system_msg: dict[str, Any] | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> str:
        """Idempotently ensure a branch row + progression exist; return its prog id.

        Tolerates a ``message`` arriving before its ``branch`` event (ordering on
        the wire is best-effort): a minimal row is created so the message is never
        lost — the same robustness the local lazy path provides.
        """
        existing = self._branch_prog_ids.get(branch_id)
        if existing is not None:
            return existing
        branch_prog_id = str(uuid.uuid4())
        self._branch_prog_ids[branch_id] = branch_prog_id
        bd = branch_dict or {}

        await self._db.create_progression(branch_prog_id)

        system_msg_id = None
        if system_msg:
            system_msg_id = system_msg.get("id")
            await self._db.insert_message(system_msg)

        await self._db.create_branch(
            {
                "id": branch_id,
                # branches.created_at is NOT NULL; an empty branch_dict (a message
                # that arrived before its branch event) has none, and create_branch's
                # ``.get(default)`` won't fire for an explicit None — so default here
                # or INSERT OR IGNORE silently drops the row.
                "created_at": bd.get("created_at") or time.time(),
                "node_metadata": bd.get("node_metadata"),
                "user": bd.get("user"),
                "name": bd.get("name"),
                "session_id": self.session_id,
                "progression_id": branch_prog_id,
                "system_msg_id": system_msg_id,
                "model": model,
                "provider": provider,
                "agent_name": bd.get("name"),
            }
        )
        return branch_prog_id

    async def _on_message(self, ev: dict[str, Any]) -> None:
        """The 4-call write sequence — a deliberate copy of ``_on_message``
        (``_orchestration.py:914``). Drift is pinned by the parity test."""
        branch_id = ev.get("branch_id")
        msg_dict = ev.get("msg")
        if not branch_id or not isinstance(msg_dict, dict):
            return
        branch_prog_id = await self._ensure_branch(branch_id)
        msg_id = msg_dict["id"]
        await self._db.insert_message(msg_dict)
        await self._db.append_to_progression(branch_prog_id, msg_id)
        await self._db.append_to_progression(self.session_prog_id, msg_id)
        # ADR-0019: activity heartbeat for staleness detection.
        await self._db.touch_session_activity(self.session_id, at=msg_dict.get("created_at"))
        # ADR-0009: keep branches.system_msg_id current if the system is replaced.
        if msg_dict.get("role") == "system":
            await self._db.update_branch(branch_id, system_msg_id=msg_id)

    async def _on_phase(self, ev: dict[str, Any]) -> None:
        """Set ``sessions.current_phase`` — the ``li monitor`` PHASE column (#1235)."""
        phase = ev.get("phase")
        if phase:
            await self._db.update_session(self.session_id, current_phase=str(phase))

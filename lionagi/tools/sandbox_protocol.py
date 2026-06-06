# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Wire protocol for streaming a sandboxed flow's persistence events to the host.

A flow running inside a Daytona sandbox is *stateless* with respect to
persistence: instead of writing to an in-container ``state.db`` that nobody
reads, it serializes every reactive-bus message to a dedicated stdout line that
the host's :class:`~lionagi.tools.sandbox_bridge.SandboxBridge` replays into the
host's ``state.db`` — the exact ``_on_message`` write sequence
(``cli/orchestrate/_orchestration.py:908``). The container is the isolation
boundary; the bus is the protocol across it (ADR-0083, ADR-0023b).

This module is the shared contract. It is pure stdlib (``json`` only) so it can
be imported inside the minimal sandbox, where only stdlib + lionagi are
available. The sentinel is distinct from the SWE-bench harness's lossy
``@@SIG@@`` human-watch stream so the two coexist on the same stdout: a human
can tail ``@@SIG@@`` while the bridge consumes ``@@LIONDB@@``.

Event kinds (the ``ev`` discriminator):

- ``branch``  — a branch row (``Branch.to_dict(mode="db")``) plus an optional
  system message dict; the host mirrors ``_ensure_branch_row``.
- ``message`` — a full ``RoledMessage.to_dict(mode="db")`` for one branch; the
  host mirrors ``_on_message``.
- ``phase``   — a coarse flow phase string for the ``sessions.current_phase``
  column (#1235, the ``li monitor`` PHASE field).
"""

from __future__ import annotations

import json
from typing import Any

#: Stdout line prefix carrying one full-fidelity persistence event.
SENTINEL = "@@LIONDB@@ "

__all__ = (
    "SENTINEL",
    "encode_event",
    "decode_line",
    "branch_event",
    "message_event",
    "phase_event",
)


def encode_event(ev: dict[str, Any]) -> str:
    """Serialize one event to a single ``@@LIONDB@@`` stdout line (newline-terminated).

    ``default=str`` matches the rest of the sandbox wire (``_sandbox_entry.py``)
    so non-JSON-native values (UUIDs, datetimes that slipped through) degrade to
    their string form instead of raising mid-stream.
    """
    return SENTINEL + json.dumps(ev, default=str) + "\n"


def decode_line(line: str) -> dict[str, Any] | None:
    """Parse one stdout line. Returns the event dict, or ``None`` if the line is
    not a ``@@LIONDB@@`` event (ordinary stdout, a ``@@SIG@@`` summary, log noise).

    Malformed JSON after the sentinel returns ``None`` rather than raising — a
    single corrupt line must never abort the persistence stream.
    """
    if not line.startswith(SENTINEL):
        return None
    payload = line[len(SENTINEL) :].strip()
    if not payload:
        return None
    try:
        ev = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return ev if isinstance(ev, dict) else None


def branch_event(
    branch_dict: dict[str, Any],
    *,
    system_msg: dict[str, Any] | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Build a ``branch`` event.

    ``branch_dict`` is ``Branch.to_dict(mode="db")``. ``model``/``provider`` are
    the per-branch resolved provenance (ADR-0022) — for a sandboxed pi/OpenRouter
    flow these disclose what the branch actually used, not the orchestrator
    default. ``system_msg`` is the branch's system message as
    ``to_dict(mode="db")`` (inserted before the branch row so its
    ``system_msg_id`` FK resolves), or ``None``.
    """
    return {
        "ev": "branch",
        "branch": branch_dict,
        "system_msg": system_msg,
        "model": model,
        "provider": provider,
    }


def message_event(branch_id: str, msg_dict: dict[str, Any]) -> dict[str, Any]:
    """Build a ``message`` event: a full ``to_dict(mode="db")`` for one branch."""
    return {"ev": "message", "branch_id": branch_id, "msg": msg_dict}


def phase_event(phase: str) -> dict[str, Any]:
    """Build a ``phase`` event — sets ``sessions.current_phase`` on the host."""
    return {"ev": "phase", "phase": phase}

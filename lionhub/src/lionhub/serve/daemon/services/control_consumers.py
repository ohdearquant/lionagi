# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Whether a queued run control would ever be drained: one rule for the read routes and for admission."""

from __future__ import annotations

import json
from typing import Any

__all__ = ("session_has_control_consumer",)


def _runner_drains_controls(session: dict[str, Any]) -> bool:
    metadata = session.get("node_metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return False
    return bool(metadata.get("drains_controls")) if isinstance(metadata, dict) else False


_CONTROL_DELIVERY_MARKERS = frozenset({"pending", "active"})


def _flow_control_poller_state(session: dict[str, Any]) -> str | None:
    """The declared DAG-poller availability for a flow/play session, or ``None``.

    Mirrors ``cli/orchestrate/_control.py``'s reader of the same field: a
    missing or unparsable declaration reads as unknown, which is refused
    alongside ``stopped``.
    """
    metadata = session.get("node_metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return None
    return metadata.get("flow_control_poller") if isinstance(metadata, dict) else None


def session_has_control_consumer(session: dict[str, Any]) -> bool:
    """Whether a control queued against this session would ever be drained.

    An agent session fails this when the runner did not declare a drain: the
    agent runner stamps ``run_id`` on the sessions it creates and writes
    ``drains_controls`` when it starts, so a row missing either is a mirrored
    or imported session that no lionagi run owns.

    A flow/play session fails it unless its DAG poller declared itself live
    (``pending`` or ``active``). A poller that has stopped delivers nothing
    more, and a row without the declaration predates control-delivery
    tracking, so neither has a consumer for a newly queued control. The CLI's
    own admission refuses both and a control admitted for one would sit
    pending forever with nobody to deliver or close it.

    Exported because the read surfaces have to answer the same question the
    admission below answers. A Studio client that offers a steer this would
    refuse gets a control that can never queue, so the offer and the refusal
    read from one rule rather than from two that can drift apart.

    A session of any other kind (an engine run, or no recorded kind) has no
    consumer for any verb, so it fails too; admission refuses it as
    ``unsupported_kind``.
    """
    kind = session.get("invocation_kind")
    if kind in {"flow", "play"}:
        return _flow_control_poller_state(session) in _CONTROL_DELIVERY_MARKERS
    if kind != "agent":
        return False
    return bool(session.get("run_id")) and _runner_drains_controls(session)

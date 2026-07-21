# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow/play `--notify` compatibility sugar over the terminal-callback
registry: registers the legacy payload shape as a scoped, overriding exec
adapter for this run's entity. See docs/internals/cli.md.
"""

from __future__ import annotations

import json

from lionagi.cli.status import _classify
from lionagi.state.lifecycle.callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from lionagi.state.lifecycle.notify_settings import (
    build_handler,
    resolve_notify_config,
)

__all__ = ("register_flow_notify_scope", "unregister_flow_notify_scope")

_PAYLOAD_ENV = "LIONAGI_NOTIFY_PAYLOAD"
_STATUS_ENV = "LIONAGI_NOTIFY_STATUS"
_INVOCATION_ID_ENV = "LIONAGI_NOTIFY_INVOCATION_ID"


def _legacy_payload_builder(
    *,
    invocation_id: str | None,
    kind: str,
    playbook: str | None,
    save_dir: str | None,
    cwd: str,
    started_at: float,
):
    def _build(envelope: RunTerminalEnvelope) -> dict:
        _, exit_class, _ = _classify("invocation", envelope.terminal_status)
        return {
            "invocation_id": invocation_id,
            "kind": kind,
            "playbook": playbook,
            "status": envelope.terminal_status,
            "reason_code": envelope.reason_code,
            "save_dir": save_dir,
            "cwd": cwd,
            "exit_class": exit_class,
            "started_at": started_at,
            "ended_at": envelope.occurred_at,
        }

    return _build


def register_flow_notify_scope(
    registry: TerminalCallbackRegistry = DEFAULT_TERMINAL_CALLBACKS,
    *,
    override: str,
    entity_kind: str,
    entity_id: str,
    invocation_id: str | None,
    flow_kind: str,
    playbook: str | None,
    save_dir: str | None,
    cwd: str,
    started_at: float,
) -> str | None:
    """Register the `--notify` legacy-payload adapter scoped to this run's
    own terminal entity. Returns the registration name (pass to
    ``unregister_flow_notify_scope`` in a ``finally`` block), or ``None`` if
    *override* resolved to disabled (never raised).
    """
    resolved = resolve_notify_config(override=override)
    if resolved is None:
        return None
    payload_fn = _legacy_payload_builder(
        invocation_id=invocation_id,
        kind=flow_kind,
        playbook=playbook,
        save_dir=save_dir,
        cwd=cwd,
        started_at=started_at,
    )

    def _argv_fn(argv: tuple[str, ...], envelope: RunTerminalEnvelope) -> list[str]:
        payload_json = json.dumps(payload_fn(envelope))
        status = envelope.terminal_status
        inv_id = invocation_id or ""
        return [
            tok.replace("{payload}", payload_json)
            .replace("{status}", status)
            .replace("{invocation_id}", inv_id)
            for tok in argv
        ]

    def _env_fn(envelope: RunTerminalEnvelope) -> dict[str, str]:
        return {
            _PAYLOAD_ENV: json.dumps(payload_fn(envelope)),
            _STATUS_ENV: envelope.terminal_status,
            _INVOCATION_ID_ENV: invocation_id or "",
        }

    handler = build_handler(resolved, payload_fn=payload_fn, argv_fn=_argv_fn, env_fn=_env_fn)
    if handler is None:
        return None
    name = f"notify.flow.{entity_kind}.{entity_id}"
    registry.register(
        name,
        handler,
        kinds=[entity_kind],
        ids=[entity_id],
        override=True,
    )
    return name


def unregister_flow_notify_scope(
    name: str | None,
    registry: TerminalCallbackRegistry = DEFAULT_TERMINAL_CALLBACKS,
) -> None:
    if name is not None:
        registry.unregister(name)

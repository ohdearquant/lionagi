# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow/play `--notify` compatibility sugar over the terminal-callback
registry.

`--notify` remains scoped compatibility sugar: after the flow/play run's own
entity id is known, it registers the legacy payload shape
(kind/playbook/save_dir/cwd/exit_class/started_at/ended_at/status/
invocation_id) as an exec adapter filtered to that one entity, and
unregisters it once the run's teardown has fired. This is deliberately
different from the settings-level `notify.on_terminal` handler (bootstrapped
once per process, unscoped, delivering the new minimal envelope) -- the
`--notify` flag is a per-run override carrying the old payload shape for
existing consumers, not a second copy of the same delivery. It registers as
an *override* (see `TerminalCallbackRegistry.register`), so it replaces the
settings-resolved handler for this one run's entity only -- other runs still
get the settings-level handler unaffected.

For backward compatibility with the documented `{payload}`/`{status}`/
`{invocation_id}` command-template placeholders and the legacy
`LIONAGI_NOTIFY_PAYLOAD`/`LIONAGI_NOTIFY_STATUS`/`LIONAGI_NOTIFY_INVOCATION_ID`
environment variables, both are still populated for this adapter -- the
placeholders are substituted into each parsed argv token directly (no shell
is ever constructed, so a literal `{payload}` inside a quoted argument is
still exactly one argv element, never re-parsed), and the same three values
are set as environment variables on the child process for consumers that
read them from the environment instead of argv or stdin.

There is no longer a direct teardown call into a notify hook: the terminal
event that used to trigger it now comes from the guarded lifecycle
transition itself (`db.update_status()` on the run's session/invocation),
so registering here and letting the registry's own post-commit push fire it
is what prevents double delivery.
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
    own terminal entity (its invocation if tracked, else its session).

    Returns the registration name (pass to ``unregister_flow_notify_scope``
    in a ``finally`` block), or ``None`` if *override* resolved to the
    disabled state (empty, shell-feature, unparseable, or a malformed
    adapter -- already logged by ``resolve_notify_config``/``build_handler``;
    never raised).
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

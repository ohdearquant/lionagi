# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Flow/play `--notify` compatibility sugar over the terminal-callback
registry: registers the legacy payload shape as a scoped, overriding exec
adapter for this run's entity. See docs/internals/cli.md.
"""

from __future__ import annotations

import json
import logging
import traceback
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio.to_thread

from lionagi.cli.status import _classify
from lionagi.ln.concurrency import is_coro_func, maybe_await
from lionagi.state.lifecycle.callbacks import (
    DEFAULT_TERMINAL_CALLBACKS,
    EntityRef,
    RunTerminalEnvelope,
    TerminalCallbackRegistry,
)
from lionagi.state.lifecycle.notify_settings import (
    build_handler,
    record_notify_outcome_to_run,
    record_notify_rejection_to_run,
    resolve_notify_config,
)

if TYPE_CHECKING:
    from lionagi.cli._runs import RunDir

__all__ = (
    "deliver_flow_notify_now",
    "register_flow_notify_scope",
    "unregister_flow_notify_scope",
)

logger = logging.getLogger(__name__)

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


def _legacy_argv_env_builders(payload_fn, *, invocation_id: str | None):
    """The `{payload}`/`{status}`/`{invocation_id}` argv substitution and the
    matching env vars, shared by every caller that launches the legacy
    `--notify` adapter (registered or delivered directly)."""

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

    return _argv_fn, _env_fn


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
    on_rejection: Callable[[str], None] | None = None,
) -> str | None:
    """Register the `--notify` legacy-payload adapter scoped to this run's
    own terminal entity. Returns the registration name (pass to
    ``unregister_flow_notify_scope`` in a ``finally`` block), or ``None`` if
    *override* resolved to disabled (never raised).

    *on_rejection*, if given, is called with a stable reason when an override
    was asked for and refused, whether the spec itself was rejected or the
    handler could not be built from it. A caller bound to a run passes this to
    record the refusal; without it, asking for a notifier and being refused
    looks exactly like never having asked, since both return ``None``.
    """

    def _report(reason: str) -> None:
        # Bookkeeping about a refusal must never turn into a second failure:
        # the caller's notifier is already not going to fire, and aborting
        # registration here would lose the reason as well.
        if on_rejection is None:
            return
        try:
            on_rejection(reason)
        except Exception:  # noqa: BLE001 -- bookkeeping must never affect the run
            logger.debug("failed to record notify override rejection", exc_info=True)

    resolution = resolve_notify_config(override=override)
    if resolution.reason is not None:
        _report(resolution.reason)
        return None
    resolved = resolution.handler
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

    _argv_fn, _env_fn = _legacy_argv_env_builders(payload_fn, invocation_id=invocation_id)

    handler = build_handler(
        resolved,
        payload_fn=payload_fn,
        argv_fn=_argv_fn,
        env_fn=_env_fn,
        on_build_failure=_report,
    )
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


async def deliver_flow_notify_now(
    *,
    override: str,
    run: RunDir,
    entity_kind: str,
    entity_id: str,
    invocation_id: str | None,
    flow_kind: str,
    playbook: str | None,
    save_dir: str | None,
    cwd: str,
    started_at: float,
    terminal_status: str,
    reason_code: str,
    occurred_at: float,
) -> None:
    """Deliver this run's `--notify` adapter directly, at process end, for a
    run that never got a session entity to fire the registered callback path
    on (``setup_agent_persist`` failed -- see docs/internals/cli.md). Same
    resolution, same legacy payload/argv/env shape as
    ``register_flow_notify_scope``, invoked immediately against a
    process-local envelope instead of registered for a transition that can
    never happen.

    Idempotent: a run that already has a recorded notify outcome (an earlier
    rejection, or an earlier direct-path attempt) is skipped rather than
    notified twice -- see docs/internals/cli.md for why a file check is
    sufficient here rather than the per-run lock the MCP job record uses.

    A notifier that was never asked for is not this function's job to notice;
    callers only reach here once they know an override was actually given.
    """
    if run.notify_outcome_path.exists():
        return

    def _report(reason: str) -> None:
        try:
            record_notify_rejection_to_run(run, reason)
        except Exception:  # noqa: BLE001 -- bookkeeping must never affect the run
            logger.debug("failed to record notify direct-delivery rejection", exc_info=True)

    resolution = resolve_notify_config(override=override)
    if resolution.reason is not None:
        _report(resolution.reason)
        return
    resolved = resolution.handler
    if resolved is None:
        return

    payload_fn = _legacy_payload_builder(
        invocation_id=invocation_id,
        kind=flow_kind,
        playbook=playbook,
        save_dir=save_dir,
        cwd=cwd,
        started_at=started_at,
    )
    _argv_fn, _env_fn = _legacy_argv_env_builders(payload_fn, invocation_id=invocation_id)

    def _outcome_fn(*, ok: bool, exit_code: int | None, stderr_text: str | None) -> str | None:
        return record_notify_outcome_to_run(
            run, ok=ok, exit_code=exit_code, stderr_text=stderr_text
        )

    handler = build_handler(
        resolved,
        payload_fn=payload_fn,
        argv_fn=_argv_fn,
        env_fn=_env_fn,
        outcome_fn=_outcome_fn,
        on_build_failure=_report,
    )
    if handler is None:
        return

    envelope = RunTerminalEnvelope(
        event_id=str(uuid.uuid4()),
        entity=EntityRef(kind=entity_kind, id=entity_id),
        previous_status=None,
        terminal_status=terminal_status,
        reason_code=reason_code,
        occurred_at=occurred_at,
    )
    try:
        if is_coro_func(handler):
            await maybe_await(handler(envelope))
        else:
            # offloaded so a sync handler body never blocks the loop; see
            # TerminalCallbackRegistry.emit, which this mirrors.
            await maybe_await(
                await anyio.to_thread.run_sync(handler, envelope, abandon_on_cancel=True)
            )
    except Exception:  # noqa: BLE001 -- a notifier failure must never affect the run
        logger.warning(
            "direct-path notify.on_terminal delivery failed for run %s", run.run_id, exc_info=True
        )
        # A python adapter's raise otherwise reaches only the log line above,
        # leaving nothing queryable — unlike the exec adapter, which records
        # every failure mode as part of outcome recording. Recorded here too
        # so durability doesn't depend on adapter type. Traceback goes to the
        # same owner-only file the exec adapter's stderr does (free text can
        # carry a credential), referenced by path, never inlined.
        record_notify_outcome_to_run(
            run, ok=False, exit_code=None, stderr_text=traceback.format_exc()
        )

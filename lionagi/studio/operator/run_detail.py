# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``run_detail`` Operator read tool: the full projection of one run.

A bounded, single-run counterpart to ``run_progress``/``run_findings`` --
unlike those two, this tool takes a bare run/session id rather than a
resolvable reference (no ``resolve_run`` indirection), and projects the
carrier's own detail fields directly rather than deriving a progress or
findings summary from them.

Carrier: ``lionagi.studio.services.runs.get_run``, called directly. That
function already resolves its own store access correctly (through
``sessions.get_session`` -> ``_open_db``, never through ``StateDB``), so this
module never constructs a ``StateDB`` itself.

Availability is reported as a ``known``/``source`` pair rather than a bare
``None``, because ``get_run`` collapses two different situations into the
same ``None`` return: the store could not be read at all, and the store was
read fine but no run matched. Those need different answers, so this module
runs its own preflight (mirroring the ``state_db_known_absent()``/
``read_only_open_supported()`` pairing every other bounded-read path in this
package already checks before opening a connection) both before calling the
carrier and again after a ``None``, so a store that disappears between the
two calls is reported as unavailable rather than as an empty result.

Redaction reuses the existing helpers in ``redact.py`` exactly as
``run_progress``/``run_findings`` do -- free-text identifier fields
(``name``, ``playbook_name``, ``agent_name``, ``model``, ``worker_name``) are
passed through ``scrub_text``, matching ``run_progress.py``'s own treatment
of the same fields; ``project`` goes through ``public_project``;
``status_reason_summary`` goes through ``redact_scalar`` with a manually
derived truncation flag, since ``redact_scalar`` reports no clipping signal
of its own.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lionagi.state.db import read_only_open_supported, state_db_known_absent
from lionagi.studio.services._db import StoreNotAddressableError
from lionagi.studio.services.runs import get_run

from .redact import (
    ARTIFACT_BYTE_CAP,
    PER_ITEM_TEXT_CAP,
    cap_payload_by_bytes,
    public_project,
    redact_arguments,
    redact_scalar,
    scrub_text,
)

__all__ = ("RunDetailInput", "run_detail")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunDetailInput(_StrictModel):
    run_id: str = Field(min_length=1, max_length=200)


def _store_unavailable() -> bool:
    """Whether the configured store cannot honestly be opened read-only.

    Same predicate pairing used as both the preflight and the post-``None``
    re-check below, so the re-check reliably diagnoses "the store vanished
    or degraded between the two calls" rather than merely guessing --
    ``state_db_known_absent()``/``read_only_open_supported()`` both
    re-resolve the store fresh on every call, they are never cached.
    """
    return state_db_known_absent() or not read_only_open_supported()


def _scrub(value: Any) -> Any:
    return scrub_text(value) if isinstance(value, str) else value


def _summary(raw: Any) -> tuple[Any, bool]:
    """Redact ``status_reason_summary`` and say truthfully whether it clipped.

    ``redact_scalar`` applies the cap to ``scrub_text(value)``, not to the raw
    value, and scrubbing moves the length in both directions -- an absolute
    path collapses to its leaf, a secret header expands to a marker. Deriving
    the flag from the raw length therefore describes a different string than
    the one that was actually capped, and the two disagree whenever scrubbing
    carries the length across the cap.

    The cap clipped exactly when the output sits at the cap while the scrubbed
    input ran past it. Testing that, rather than testing the raw length, also
    keeps the secret-value path honest: there ``redact_scalar`` substitutes a
    short marker instead of slicing, so nothing was truncated.
    """
    redacted = redact_scalar("status_reason_summary", raw)
    if not isinstance(raw, str) or not isinstance(redacted, str):
        return redacted, False
    return redacted, len(redacted) == PER_ITEM_TEXT_CAP < len(scrub_text(raw))


def _project(run: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Redact and rename ``get_run``'s detail fields for the Operator surface.

    Returns ``(fields, truncated)``, where ``truncated`` is the aggregate over
    every field bounded here: the capped ``status_reason_summary`` and the
    byte-capped ``manifest``.

    ``cwd``, ``state_root``, ``artifact_root``, ``manifest``, ``task`` and
    ``error`` are shown by no other Operator tool, and they are the fields
    shaped to carry filesystem layout and arbitrary payloads. On the carrier
    this module actually calls they are not yet dangerous: ``get_run``'s
    StateDB path constant-fills ``task``/``error``/``cwd``/``manifest`` with
    ``""``/``None``/``None``/``{}`` and already runs both roots through
    ``public_path``. They are redacted here anyway because that is a property
    of the carrier, not of this projection -- ``runs.py``'s own manifest-backed
    builder fills the same field names from raw manifest text (``task`` from
    ``manifest["prompt"]``, ``error`` from ``manifest["error"]``), so the
    projection has to be safe for the values the field names promise rather
    than for the placeholders one code path happens to supply today. The
    path-shaped and free-text fields go through ``scrub_text`` like every
    other free-text field here; ``manifest`` goes through the recursive
    redactor and a byte cap because it is an unbounded mapping.
    """
    raw_summary = run.get("status_reason_summary")
    redacted_summary, summary_truncated = _summary(raw_summary)

    manifest, manifest_truncated = cap_payload_by_bytes(
        redact_arguments(run.get("manifest")), ARTIFACT_BYTE_CAP
    )

    fields = {
        "runId": run.get("run_id"),
        "id": run.get("id"),
        "name": _scrub(run.get("name")),
        "playbookName": _scrub(run.get("playbook_name")),
        "agentName": _scrub(run.get("agent_name")),
        "invocationKind": run.get("invocation_kind"),
        "showPlayName": run.get("show_play_name"),
        "sourceKind": run.get("source_kind"),
        "invocationId": run.get("invocation_id"),
        "model": _scrub(run.get("model")),
        "provider": run.get("provider"),
        "effort": run.get("effort"),
        "agentHash": run.get("agent_hash"),
        "status": run.get("status"),
        "startedAt": run.get("started_at"),
        "endedAt": run.get("ended_at"),
        "createdAt": run.get("created_at"),
        "updatedAt": run.get("updated_at"),
        "lastMessageAt": run.get("last_message_at"),
        "effectiveHealth": run.get("effective_health"),
        "branchCount": run.get("branch_count"),
        "messageCount": run.get("message_count"),
        "project": public_project(run.get("project")),
        "projectSource": run.get("project_source"),
        "statusReasonCode": run.get("status_reason_code"),
        "statusReasonSummary": redacted_summary,
        "totalCostUsd": run.get("total_cost_usd"),
        "inputTokens": run.get("input_tokens"),
        "outputTokens": run.get("output_tokens"),
        "stateRoot": _scrub(run.get("state_root")),
        "artifactRoot": _scrub(run.get("artifact_root")),
        "workerName": _scrub(run.get("worker_name")),
        "task": _scrub(run.get("task")),
        "stepCount": run.get("step_count"),
        "finishedAt": run.get("finished_at"),
        "error": _scrub(run.get("error")),
        "cwd": _scrub(run.get("cwd")),
        "manifest": manifest,
        "messageLimit": run.get("message_limit"),
        "messageCursor": run.get("message_cursor"),
        "messageNextCursor": run.get("message_next_cursor"),
    }
    return fields, summary_truncated or manifest_truncated


async def run_detail(arguments: dict[str, Any]) -> dict[str, Any]:
    """Project one run's full detail row, or report why it could not be read.

    Returns exactly one of:
      - ``{"known": False, "source": "unavailable"}`` -- the store could not
        be opened read-only, either before the carrier call, on the carrier
        call itself raising, or (having vanished in between) on a ``None``
        the carrier returned.
      - ``{"known": False, "source": "store"}`` -- the store was read fine;
        no run matches ``run_id``.
      - ``{"known": True, "source": "store", "truncated": bool, **fields}``.
    """
    input_ = RunDetailInput.model_validate(arguments)

    if _store_unavailable():
        return {"known": False, "source": "unavailable"}

    try:
        run = await get_run(input_.run_id)
    except (StoreNotAddressableError, OSError):
        # Catches only store/open failures -- a programming error inside
        # get_run is not turned into "unavailable". The preflight above
        # already excludes the ordinary StoreNotAddressableError case; this
        # remains reachable when the store degrades between the two calls.
        return {"known": False, "source": "unavailable"}

    if run is None:
        if _store_unavailable():
            return {"known": False, "source": "unavailable"}
        return {"known": False, "source": "store"}

    fields, truncated = _project(run)
    return {"known": True, "source": "store", "truncated": truncated, **fields}

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``run_detail`` Operator read tool: the full projection of one run.

A bounded, single-run counterpart to ``run_progress``/``run_findings`` --
unlike those two, this tool takes a bare run/session id rather than a
resolvable reference (no ``resolve_run`` indirection), and projects the
carrier's (``lionagi.studio.services.runs.get_run``) own detail fields
directly. See docs/internals/studio.md ("Bounded read projections") for the
``known``/``source`` availability contract and the redaction rules shared
with ``run_progress``/``run_findings``.
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
from .run_progress import _terminal_safe_health

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

    Truncation is detected against the *scrubbed* input, not the raw value:
    scrubbing changes length in both directions (a path collapses to its
    leaf, a header expands to a marker), so the cap clipped exactly when the
    output sits at the cap while the scrubbed input ran past it. This also
    keeps the secret-value path honest, where ``redact_scalar`` substitutes a
    short marker instead of slicing -- nothing was truncated there.
    """
    redacted = redact_scalar("status_reason_summary", raw)
    if not isinstance(raw, str) or not isinstance(redacted, str):
        return redacted, False
    return redacted, len(redacted) == PER_ITEM_TEXT_CAP < len(scrub_text(raw))


def _project(run: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Redact and rename ``get_run``'s detail fields for the Operator surface.

    Returns ``(fields, truncated)``, the aggregate over every field bounded
    here (``status_reason_summary`` and ``manifest``). ``cwd``, ``state_root``,
    ``artifact_root``, ``manifest``, ``task`` and ``error`` carry filesystem
    layout and arbitrary payloads and are shown by no other Operator tool;
    they are redacted for what the field names promise across every backing
    carrier, not just the safe placeholders today's StateDB carrier happens
    to fill -- see docs/internals/studio.md ("Bounded read projections").
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
        "effectiveHealth": _terminal_safe_health(run),
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

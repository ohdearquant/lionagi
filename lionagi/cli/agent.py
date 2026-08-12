# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent` — one-shot or resumed single-agent conversation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from lionagi import Branch
from lionagi._auto import CliDeclaration, auto_register
from lionagi._errors import ConfigurationError
from lionagi._errors import TimeoutError as LionTimeoutError
from lionagi.ln.concurrency import (
    SigtermInterrupt,
    cache_cancelled_exc_class,
    cancelled_exc_classes,
    run_async,
)
from lionagi.mcp.config import JOB_MARKER_ENV_VAR
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.protocols.messages import ActionRequest, AssistantResponse
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import resolve_artifact_contract

from ._agent_depth import stamp_agent_depth
from ._context_from import (
    DEFAULT_CONTEXT_BUDGET_TOKENS,
    ContextFromError,
    resolve_and_build_context_block,
)
from ._logging import hint, log_error
from ._mcp_resolve import McpConfigError, McpResolution, resolve_spawn_mcp_servers
from ._providers import (
    _CLAUDE_PROVIDER_NAMES,
    BACKENDS,
    PROVIDER_BYPASS_KWARGS,
    PROVIDER_EFFORT_KWARG,
    PROVIDER_FAST_KWARGS,
    PROVIDER_YOLO_KWARGS,
    PROVIDERS_EFFORT_VIA_MODEL_NAME,
    _clamp_claude_effort,
    _clamp_codex_effort,
    add_common_cli_args,
    build_chat_model,
    build_deadline_preamble,
    load_agent_profile,
    normalize_effort,
    parse_model_spec,
    resolve_persisted_effort,
)
from ._runs import (
    allocate_run,
    find_branch,
    load_last_branch,
    resolve_run_reason,
    save_last_branch_pointer,
    setup_agent_persist,
    teardown_agent_persist,
)
from ._util import EXIT_CODE_BY_STATUS, classify_exception, validate_cwd_exists

# Preset names supported by --preset.
_PRESET_CHOICES = ("coding",)

_DETACHED_EXECUTION_BOUNDARY = """[DETACHED EXECUTION BOUNDARY]
This process was launched as a detached MCP job. No interactive harness is attached, so this turn cannot receive a background completion notification. Keep every command in the foreground. If a command tool yields a live execution handle, poll that same handle until it reaches a terminal result. Do not arm a monitor or finish a turn waiting for an external notification. Verify the requested outputs before reporting completion."""

# --image extension -> MIME type, matching InstructionContent's data-URI allowlist
# (lionagi/protocols/messages/instruction.py _DATA_IMAGE_RE: png/jpe?g/gif/webp).
_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_KHIVE_INJECTION_COUNTERS = (
    "recall_turns",
    "blocks_injected",
    "failed",
    "writeback_records",
    "writeback_failed",
)


def _apply_detached_execution_boundary(prompt: str) -> str:
    """Tell detached MCP legs which interactive wait channel they do not have."""
    if not os.environ.get(JOB_MARKER_ENV_VAR):
        return prompt
    if prompt.startswith(_DETACHED_EXECUTION_BOUNDARY):
        return prompt
    return f"{_DETACHED_EXECUTION_BOUNDARY}\n\n{prompt}"


def _fold_injection_stats(totals: dict[str, int], stats: object) -> None:
    """Add one provider-stat snapshot into session totals.

    Unexpected values are ignored so telemetry cannot break teardown.
    """
    if not isinstance(stats, dict):
        return
    for counter in _KHIVE_INJECTION_COUNTERS:
        value = stats.get(counter)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            totals[counter] += value


async def _seed_injection_stats(live: dict | None, totals: dict[str, int]) -> None:
    """Seed a top-level resume from its durable session counters.

    Missing or malformed metadata leaves the new invocation at zero.
    """
    if not live or live.get("db") is None:
        return
    try:
        session = await live["db"].get_session(live["session_id"])
        metadata = session.get("node_metadata") if session else None
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if isinstance(metadata, dict):
            _fold_injection_stats(totals, metadata.get("khive_injection"))
    except Exception:  # noqa: BLE001
        return


def _load_image_data_uris(paths: list[str]) -> list[str]:
    """Read each --image path and wrap it as a `data:image/...;base64,...` URI —
    the same shape InstructionContent._format_image_item already accepts, so this
    is a pure CLI-side reader, not a new content representation.

    Raises FileNotFoundError / ValueError (with the offending path named) for a
    missing path, a non-file path, or an unrecognized extension. Fails fast,
    before any LLM call — same contract as --form / --prompt-file.
    """
    import base64

    uris: list[str] = []
    for raw_path in paths:
        p = Path(raw_path)
        if not p.exists():
            raise FileNotFoundError(f"--image path not found: {raw_path!r}")
        if not p.is_file():
            raise ValueError(f"--image path is not a regular file: {raw_path!r}")
        media_type = _IMAGE_MEDIA_TYPES.get(p.suffix.lower())
        if media_type is None:
            allowed = ", ".join(sorted(_IMAGE_MEDIA_TYPES))
            raise ValueError(
                f"--image {raw_path!r}: unrecognized extension {p.suffix!r}; "
                f"supported extensions: {allowed}"
            )
        encoded = base64.b64encode(p.read_bytes()).decode("ascii")
        uris.append(f"data:{media_type};base64,{encoded}")
    return uris


def _make_coding_preset(
    cwd: str | None = None,
    effort: str | None = "high",
    system_prompt: str | None = None,
    role: str = "implementer",
):
    """Construct an AgentSpec.coding() instance; isolated for test monkeypatching."""
    from lionagi.agent.spec import AgentSpec

    return AgentSpec.coding(cwd=cwd, effort=effort, system_prompt=system_prompt, role=role)


# WorkForm loading helpers (for --form).


_FORM_SPEC_ALLOWED_KEYS = frozenset({"title", "fields", "values"})


def _load_form_spec(path: str) -> dict:
    """Load a YAML or JSON work-form spec file; raises ValueError/FileNotFoundError on failure."""
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        raise FileNotFoundError(f"form spec file not found: {path!r}")
    if not p.is_file():
        raise ValueError(f"form spec path is not a regular file: {path!r}")

    with open(path) as fh:
        raw = fh.read()

    # Try YAML first (superset of JSON), then fall back to plain JSON.
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(raw)
    except Exception as yaml_err:
        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError(f"could not parse form spec {path!r}: {yaml_err}") from yaml_err

    if not isinstance(data, dict):
        raise ValueError(
            f"form spec {path!r} must be a YAML/JSON mapping, got {type(data).__name__}"
        )
    return data


def _build_work_form(spec: dict, spec_path: str):
    """Construct a WorkForm from a parsed spec dict (keys: title, fields, values)."""
    from lionagi.work import FieldSpec, WorkForm, fill_form

    # Enforce closed top-level schema.
    unknown_keys = set(spec) - _FORM_SPEC_ALLOWED_KEYS
    if unknown_keys:
        bad = ", ".join(sorted(f"{k!r}" for k in unknown_keys))
        raise ValueError(
            f"form spec {spec_path!r}: unknown top-level key(s) {bad}; "
            f"allowed: {sorted(_FORM_SPEC_ALLOWED_KEYS)}"
        )

    title = spec.get("title", spec_path)
    raw_fields_raw = spec.get("fields")
    raw_values_raw = spec.get("values")

    # Validate types: 'fields' and 'values' must be mappings when present.
    if raw_fields_raw is not None and not isinstance(raw_fields_raw, dict):
        raise ValueError(
            f"form spec {spec_path!r}: 'fields' must be a mapping, "
            f"got {type(raw_fields_raw).__name__!r}"
        )
    if raw_values_raw is not None and not isinstance(raw_values_raw, dict):
        raise ValueError(
            f"form spec {spec_path!r}: 'values' must be a mapping, "
            f"got {type(raw_values_raw).__name__!r}"
        )

    raw_fields: dict = raw_fields_raw or {}
    raw_values: dict = raw_values_raw or {}

    # --form is a validation gate; values without declared fields would be
    # forwarded unvalidated, defeating that purpose.
    if raw_values and not raw_fields:
        raise ValueError(
            f"form spec {spec_path!r}: 'values' are declared but 'fields' is "
            "absent or empty; declare fields to validate values against"
        )

    # When fields are declared, reject undeclared value keys.
    if raw_fields:
        undeclared = set(raw_values) - set(raw_fields)
        if undeclared:
            bad = ", ".join(sorted(f"{k!r}" for k in undeclared))
            raise ValueError(
                f"form spec {spec_path!r}: values contain undeclared key(s) {bad}; "
                f"declared fields: {sorted(raw_fields)}"
            )

    fields: dict[str, FieldSpec] = {}
    for name, fspec in raw_fields.items():
        if not isinstance(fspec, dict):
            raise ValueError(
                f"form spec {spec_path!r}: field {name!r} must be a mapping, "
                f"got {type(fspec).__name__}"
            )
        try:
            fields[name] = FieldSpec(name=name, **fspec)
        except Exception as exc:
            raise ValueError(f"form spec {spec_path!r}: invalid field {name!r}: {exc}") from exc

    form = WorkForm(title=title, fields=fields)
    if raw_values or fields:
        form = fill_form(form, raw_values)
    return form


def _form_to_context_block(form) -> str:
    """Render a validated WorkForm's values as a structured context preamble
    to prepend to the user's prompt."""
    lines = [f"[Work Form: {form.title}]"]
    for key, value in form.values.items():
        lines.append(f"  {key}: {value!r}")
    return "\n".join(lines)


# Heartbeat tick interval. Module-level so a test can shrink it rather than
# waiting out a real 60s tick.
_HEARTBEAT_INTERVAL_S = 60


class _ProgressReport:
    """Heartbeat text that distinguishes a working agent from a merely live one.

    A bare elapsed-seconds timer reports that the event loop is scheduling, which
    a reader takes as evidence that work is happening. Assistant responses and
    action requests are added to the branch as the stream arrives, so counting
    them says whether anything has actually landed since the run started.
    """

    def __init__(self, branch, now: float):
        self._branch = branch
        self._start = now
        # The only read taken before the run starts, so every later count is a
        # delta from it. Do not retry it on failure: a baseline adopted once
        # messages have arrived would take them as the starting point and report
        # work already done as no progress at all.
        self._base = self._counts()
        self._last = self._base
        self._changed_at = now

    def _counts(self) -> tuple[int, int, int] | None:
        """(turns, tool calls, total messages), or None if they cannot be read."""
        try:
            messages = list(self._branch.msgs.messages)
        except Exception:
            return None
        turns = sum(1 for m in messages if isinstance(m, AssistantResponse))
        calls = sum(1 for m in messages if isinstance(m, ActionRequest))
        return turns, calls, len(messages)

    def line(self, now: float) -> str:
        elapsed = int(now - self._start)
        current = self._counts()
        # An unreadable baseline (never retried) means no delta for the rest
        # of this run; a single unreadable snapshot is only this tick's own
        # gap. Neither falls back to the previous reading — that would
        # present a stale count as current.
        if self._base is None:
            return (
                f"[progress] {elapsed}s elapsed — progress is not observable for "
                "this run; this line means alive, not working"
            )
        if current is None:
            return (
                f"[progress] {elapsed}s elapsed — progress could not be read this "
                "tick; this line means alive, not working"
            )
        if current[2] != self._last[2]:
            self._last = current
            self._changed_at = now
        turns = self._last[0] - self._base[0]
        calls = self._last[1] - self._base[1]
        if turns <= 0 and calls <= 0:
            return f"[progress] {elapsed}s elapsed — no completed turn yet ({elapsed}s since start)"
        return (
            f"[progress] {elapsed}s elapsed — {turns} turn{'' if turns == 1 else 's'}, "
            f"{calls} tool call{'' if calls == 1 else 's'}, "
            f"last activity {int(now - self._changed_at)}s ago"
        )


def _report_mcp_resolution(
    resolution: McpResolution, *, provider: str, cwd: str | None, forwarded: bool
) -> None:
    """Say at spawn time what tool surface the leg is actually getting.

    The whole point of resolving here is that a leg starting without the
    servers its instructions assume should be visible when it is launched
    rather than inferred from its output an hour later. Silence is reserved
    for the one case where it is accurate: servers resolved and handed over.

    ``forwarded`` is the caller's answer to "does this spawn hand the resolved
    set to the leg?", read off the request that spawn built. This function must
    not re-derive it from the provider name: a second list here is what let the
    message contradict the spawn.
    """
    from lionagi.cli._logging import warn

    if not forwarded:
        if resolution.servers is not None:
            warn(
                f"MCP servers resolved from {resolution.source} are not carried to "
                f"provider {provider!r} on this spawn path. This leg gets whatever "
                "its own provider resolves for itself."
            )
        return

    if resolution.servers is not None:
        names = ", ".join(sorted(resolution.servers))
        hint(f"[mcp] {len(resolution.servers)} server(s) from {resolution.source}: {names}")
        return

    if resolution.reason is None:
        return  # --no-mcp-config: chosen, not degraded

    target = cwd or os.getcwd()
    warn(
        f"no MCP servers are being handed to this leg ({resolution.reason}; searched "
        f"from {resolution.searched_from}). It will start with only whatever the "
        f"{provider} CLI discovers from {target} itself, which is where a leg pointed at "
        "a checkout silently loses them. Pass --mcp-config PATH, or --no-mcp-config "
        "to state that this is intended."
    )


"""Framing for a steer delivered as a continuation turn.

Deliberately mirrors the flow-side operator-steer vocabulary and, like it,
claims no authority to override: the operator's own words say what to change.
An earlier draft announced that the steer "supersedes conflicting parts of the
original instruction", and a live leg correctly refused it — a banner asserting
override authority reads exactly like injected content trying to redirect the
model away from what its user asked for. The channel already carries the
operator's authority (this is the same instruction slot the original prompt
arrived through), so the framing only has to say who is speaking and when.
"""
_AGENT_STEER_TEMPLATE = """\
[OPERATOR STEER]
The operator who started this run sent this while it was running. It is a live
correction to the task you are already working on, from the same person who
gave you that task — not a message from a third party. Attend to it before
continuing. Most recent last.
{lines}
[/OPERATOR STEER]
"""


async def _drain_pending_steers(
    live: dict | None,
    branch,
    *,
    operate_kwargs: dict,
    deadline: float | None,
    owner: str | None = None,
) -> object | None:
    """Consume queued `message` session controls as warm continuation turns.

    Called after a successful operate() and before the run finalizes. Each
    drain batch joins all pending steers (arrival order) into one continuation
    turn on the same branch; steers enqueued during a continuation are caught
    by the next iteration. Continuations spend the leg's original wall clock:
    past *deadline* the loop stops without consuming (teardown tombstones the
    remainder), so steering can never keep a leg alive past its budget.

    *owner* names this leg on the rows it claims, so a concurrent leg and the
    teardown can both tell this leg's in-flight work from their own.

    Returns the last continuation result, or None if nothing was consumed.
    """
    if not live or not live.get("session_id"):
        return None
    import time as _time

    from lionagi.cli._logging import hint, log_error, warn

    db = live.get("db")
    if db is None:
        # A persistence context carrying a session but no handle to read it
        # with. Nothing can be drained, and returning quietly would make that
        # indistinguishable from "no steers were queued" -- so say which one
        # this is. setup_agent_persist always supplies both.
        log_error("steer: session is persisted but no database handle came with it; not draining")
        return None
    session_id = live["session_id"]
    last_res = None
    while True:
        if deadline is not None and _time.monotonic() >= deadline:
            break
        pending = await db.list_pending_session_controls(session_id)
        steers = [row for row in pending if row.get("verb") == "message"]
        if not steers:
            break
        if str(steers[0].get("result") or "").startswith("applying"):
            # A consumer stamped this row and did not finish. Re-applying it
            # could deliver the same operator message a second time, so it is
            # left untouched, and the rows behind it wait rather than jumping
            # it. This is the rule the flow poller already follows. It holds
            # whoever the claimant is, including this leg on a later pass:
            # a claim that outlived its apply is exactly the case where nobody
            # can say whether the message landed.
            break
        # The deadline was checked before the queue read above, and that read is
        # I/O that can cross it. Rechecking here, before anything is claimed or
        # sent, is what keeps the run inside the timeout the caller gave it.
        remaining = None
        if deadline is not None:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
        texts = []
        claimed = []
        for row in steers:
            # Carry the claim the database actually wrote rather than rebuilding
            # it here: the finalize below is only guarded if the two agree, and
            # two copies of the same expression are what stop agreeing.
            claim_token = await db.mark_session_control_applying(row["id"], owner=owner)
            if claim_token is None:
                # Another consumer claimed it between the read and here.
                continue
            claimed.append((row, claim_token))
            payload = row.get("payload") or {}
            texts.append(str(payload.get("text") or ""))
        if not claimed:
            break
        joined = "\n".join(f"- {t}" for t in texts if t.strip())
        hint(f"[steer] applying {len(claimed)} queued operator message(s) as a continuation turn")
        kwargs = dict(operate_kwargs)
        if remaining is not None:
            # What is actually left, never a floor. Flooring the budget would
            # hand the continuation a fresh second that the caller's deadline
            # has already spent.
            kwargs["timeout"] = remaining
        last_res = await branch.operate(
            instruction=_AGENT_STEER_TEMPLATE.format(lines=joined),
            **kwargs,
        )
        for row, claim_token in claimed:
            # Unconditional on the clock, deliberately. The deadline gates when
            # new provider work may start, which the recheck above enforces;
            # recording the outcome of work already performed is exempt, because
            # a skipped finalize would leave a delivered message on record as
            # undelivered. The claim token is the guard here instead: this write
            # lands only while the row still carries this leg's claim.
            stamped = await db.finalize_session_control(
                row["id"], result="applied", expect_claim=claim_token
            )
            if not stamped:
                # Somebody resolved the row while the continuation was running.
                # Their outcome stands, which is the point of the guard, but the
                # message was already delivered to the branch by then, so the
                # record now disagrees with what happened. Say so: the delivery
                # cannot be taken back, and a silent refusal here is the same
                # defect as the overwrite, one level up.
                warn(
                    f"operator message {row['id']} was delivered, but the control row "
                    f"was resolved by someone else first and now records their outcome "
                    f"instead of 'applied'. The message reached the agent."
                )
    return last_res


async def _tombstone_pending_steers(live: dict | None) -> None:
    """Finalize never-claimed session controls as rejected at run teardown.

    A steer enqueued while the run was live but never drained must not sit
    pending forever. Best-effort: a failure here logs and leaves the row
    visibly pending, since the status surface independently renders a pending
    control on a terminal run as never-landed.

    Only rows no consumer ever claimed are tombstoned, enforced at the write
    rather than read off the snapshot -- a claimed row belongs to whichever
    leg took it (still in its provider call, or dead between claim and apply),
    and rejecting it would assert non-delivery that nothing here actually
    knows, so it stays visible as claimed for an operator to resolve. Called
    after teardown, so the terminal check at the top is what makes this hold
    even when teardown failed to persist the transition it was asked for.
    """
    if not live or not live.get("session_id"):
        return
    try:
        from lionagi.state.db import SESSION_TERMINAL_STATUSES

        session = await live["db"].get_session(live["session_id"])
        if (session or {}).get("status") not in SESSION_TERMINAL_STATUSES:
            # The precondition this sweep's correctness rests on, asserted
            # rather than assumed. Rejecting a control on a session that is
            # still running would destroy a steer a live consumer was about to
            # take, and the sweep only closes the terminal race at all because
            # the transition it follows is what stops new controls being
            # admitted. Refusing here rather than sweeping keeps a call-site
            # ordering mistake from turning into a deleted operator message.
            log_error(
                "steer tombstone skipped: session "
                f"{str(live['session_id'])[:8]} is not terminal, so a pending "
                "control may still have a consumer"
            )
            return
        stale = await live["db"].list_pending_session_controls(live["session_id"])
        for row in stale:
            if row.get("result") is not None:
                continue
            # The snapshot above says the row was unclaimed when it was read,
            # which is not the same as unclaimed when this writes. Another leg
            # that read the row at its own turn boundary can claim it and hand
            # the steer to the model in between, so the guard has to travel with
            # the write: only_if_unclaimed makes the database re-check, and a
            # row that got claimed in the window is left alone rather than
            # recorded as never delivered.
            await live["db"].finalize_session_control(
                row["id"],
                result=(
                    "rejected: run reached terminal status before "
                    "the steer could land — use `li agent -r`"
                ),
                only_if_unclaimed=True,
            )
    except Exception as exc:  # noqa: BLE001 — teardown must not raise
        log_error(f"steer tombstone write failed: {exc!r}")


async def _run_agent(
    model_str: str | None,
    prompt: str,
    yolo: bool = False,
    verbose: bool = False,
    theme: str | None = None,
    resume: str | None = None,
    continue_last: bool = False,
    effort: str | None = None,
    agent_name: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    fast: bool = False,
    invocation_id: str | None = None,
    project: str | None = None,
    bypass: bool = False,
    preset: str | None = None,
    resume_on_timeout: bool = False,
    context_from: list[str] | None = None,
    context_budget: int | None = None,
    notify: str | None = None,
    images: list[str] | None = None,
    mcp_config: str | None = None,
    no_mcp_config: bool = False,
    _auto_resumed: bool = False,
    _injection_totals: dict[str, int] | None = None,
) -> tuple[str, str, str, str, str | None]:
    """Execute one agent turn; returns (result, provider, branch_id, terminal_status, session_id).

    session_id is None whenever live persistence never started.
    """
    prompt = _apply_detached_execution_boundary(prompt)
    seed_injection_totals = _injection_totals is None
    _injection_totals = (
        dict.fromkeys(_KHIVE_INJECTION_COUNTERS, 0)
        if _injection_totals is None
        else dict(_injection_totals)
    )
    effort = normalize_effort(effort)
    # Fail fast before any run is allocated: a nonexistent --cwd must not spawn
    # into a provider-created dir. Forward the tilde-expanded path — providers
    # never expand `~`.
    cwd = validate_cwd_exists(cwd)
    # Read from *this* process's directory, before --cwd is applied to anything.
    # The child's tool surface is meant to be a property of the submission, and
    # this is the only point at which the submitting directory is still known.
    mcp_resolution = resolve_spawn_mcp_servers(
        mcp_config,
        launch_dir=os.getcwd(),
        disabled=no_mcp_config,
    )
    if resume and continue_last:
        raise ConfigurationError("--resume / -r and --continue-last / -c are mutually exclusive.")
    if preset and (resume or continue_last):
        raise ConfigurationError(
            "--preset only applies to new branches; cannot combine with --resume / --continue-last."
        )
    if context_from and (resume or continue_last):
        raise ContextFromError(
            "--context-from cannot be combined with --resume / -r or --continue-last / -c "
            "(resume already carries the source context)."
        )
    if context_from:
        effective_context_budget = (
            context_budget if context_budget is not None else DEFAULT_CONTEXT_BUDGET_TOKENS
        )
        context_block = await resolve_and_build_context_block(
            context_from, effective_context_budget
        )
        if context_block:
            prompt = f"{context_block}\n\n{prompt}"

    # Cache cancellation exception class while event loop is running;
    # cancelled_exc_classes() in the error path needs it after loop exit.
    try:
        cache_cancelled_exc_class()
    except Exception as _cache_err:
        import logging as _logging

        _logging.getLogger("lionagi.cli").debug(
            "cache_cancelled_exc_class() failed (non-fatal): %s", _cache_err
        )

    profile = None
    if agent_name:
        profile = load_agent_profile(agent_name)
        if profile.model and model_str is None:
            model_str = profile.model
        if profile.effort and effort is None:
            effort = normalize_effort(profile.effort)
        if profile.yolo and not yolo:
            yolo = True
        if profile.bypass and not bypass:
            bypass = True
        if profile.fast_mode and not fast:
            fast = True
        if profile.timeout and timeout is None:
            timeout = profile.timeout
        if profile.resume_on_timeout and not resume_on_timeout:
            resume_on_timeout = True
        if (getattr(profile, "extra", None) or {}).get("hooks"):
            # A saved guard the leg silently does not run is worse than no
            # guard: say so at launch until CLI runs consume the assembly.
            from lionagi.cli._logging import warn

            warn(
                f"agent profile {agent_name!r} declares a hooks assembly; "
                "CLI-spawned runs do not apply profile hook assemblies yet "
                "(Studio Operator turns do), so those hooks are NOT active "
                "for this run"
            )

    # Validate a declared profile `role:` key up front: a falsy-but-present
    # value must fail loudly here, not silently fall back to "implementer".
    profile_role_extra = (getattr(profile, "extra", None) or {}) if profile else {}
    has_role_key = "role" in profile_role_extra
    profile_role = profile_role_extra.get("role") if has_role_key else None
    if has_role_key and (not isinstance(profile_role, str) or not profile_role.strip()):
        raise ConfigurationError(
            f"agent profile {getattr(profile, 'name', '<unknown>')!r} declares a "
            f"`role` key but its value {profile_role!r} is not a non-empty "
            "string; set it to a valid role name, or remove the key to keep "
            "the plain profile path (no role/policy composition)."
        )

    stamp_agent_depth(agent_name)

    # True only when a NEW branch took the create_agent path (--preset coding
    # or an opted-in profile `role:` key) — see the add_message guard below.
    took_create_agent_path = False

    branch: Branch | None = None
    if continue_last:
        _, branch_id = load_last_branch()
        _, branch_path = find_branch(branch_id)
        branch = Branch.from_dict(json.loads(branch_path.read_text()))
    elif resume:
        _, branch_path = find_branch(resume)
        resolved_branch_id = branch_path.stem
        if resolved_branch_id != resume:
            hint(f"[resume] prefix-matched {resume} → {resolved_branch_id}")
        branch = Branch.from_dict(json.loads(branch_path.read_text()))

    # Capture before the new-branch block below reassigns `branch`: the only
    # reliable "reopened existing" vs "minting new" signal once that block runs.
    is_resumed_branch = branch is not None

    if model_str is not None:
        ms = parse_model_spec(model_str)
        if branch is not None and "/" not in ms.model and ms.model not in BACKENDS:
            # A bare token that isn't a known backend name is almost always a
            # mangled command (e.g. a --resume id split across two argv tokens).
            log_error(
                f"resume model override {model_str!r} does not look like a "
                "model spec (expected 'provider/model', or a known name "
                "like 'claude', 'codex', 'gemini-code'). Positionals are "
                "[MODEL] PROMPT — this looks like a mangled command, e.g. "
                "a --resume id accidentally split across two arguments."
            )
            return "", "", str(branch.id), "failed", None
        if "/" in ms.model:
            provider, model = ms.model.split("/", 1)
        else:
            provider, model = ms.model, ms.model
        if ms.effort and not effort:
            effort = ms.effort
    elif branch is not None:
        ep_cfg = branch.chat_model.endpoint.config
        provider = ep_cfg.provider
        model = ep_cfg.kwargs.get("model")
    else:
        raise ValueError(
            "Provide a model spec (e.g. 'claude') for a new branch, "
            "or use --resume / --continue-last to reopen an existing one."
        )

    from lionagi.agent.factory import _reject_unforwardable_explicit_mcp

    _reject_unforwardable_explicit_mcp(
        provider,
        named_explicitly=mcp_resolution.explicit,
        asked_for_servers=bool(mcp_resolution.servers),
    )

    if branch is None:
        # Codex blocks tool calls until file access is enabled. Surface this
        # even without verbose output; CLI or profile approval flags suppress it.
        if provider == "codex" and not yolo and not bypass:
            from lionagi.cli._logging import warn

            warn(
                "codex models need file access enabled or the agent may hang "
                "silently on its first tool call. Re-run with --yolo for the "
                "sandboxed default, or use an agent profile (-a). --bypass also "
                "works but disables the sandbox."
            )
        chat_model = build_chat_model(
            provider,
            model,
            yolo,
            verbose,
            theme,
            effort,
            fast,
            bypass,
            mcp_servers=mcp_resolution.servers,
        )
        effort = resolve_persisted_effort(provider, chat_model, effort)
        # Two spawn shapes hand the set over in two different places, so the
        # message has to read the one that applies. A plain leg gets only what
        # build_chat_model already put on the request, which is knowable here;
        # a create_agent leg is handed the set inside create_agent, so its
        # message waits for the request that call produces (below).
        takes_create_agent_path = preset == "coding" or has_role_key
        if not takes_create_agent_path:
            from lionagi.agent.factory import request_kwargs_carry_forwarded_mcp

            # Read the request build_chat_model produced: the two transports
            # put the set in different places, and a provider name is what got
            # this wrong before.
            built_config = getattr(getattr(chat_model, "endpoint", None), "config", None)
            forwarded = request_kwargs_carry_forwarded_mcp(getattr(built_config, "kwargs", None))
            _report_mcp_resolution(mcp_resolution, provider=provider, cwd=cwd, forwarded=forwarded)

        # Opt-in profile `role:` key switches a plain `-a <profile>` leg onto
        # the same create_agent path as --preset coding, parameterized by role.
        if takes_create_agent_path:
            took_create_agent_path = True
            # Use create_agent so CodingToolkit tools and path-guards are wired;
            # compose the profile extension into the spec before calling it.
            from lionagi.agent.factory import (
                create_agent,
                request_carries_forwarded_mcp,
            )

            # Use profile.raw_body, not profile.system_prompt, to avoid
            # duplicating LION_SYSTEM_MESSAGE (see docs/internals/cli.md).
            profile_extra = (getattr(profile, "raw_body", None) or "") if profile else ""
            spec = _make_coding_preset(
                cwd=cwd,
                effort=effort or "high",
                system_prompt=profile_extra or None,
                role=profile_role if has_role_key else "implementer",
            )
            if profile is not None:
                spec.khive_injection = getattr(profile, "khive_injection", None)
            # AgentSpec.coding()/compose() default lion_system=True regardless
            # of the profile's frontmatter — propagate an explicit opt-out.
            if profile is not None and not profile.lion_system:
                spec.lion_system = False
            # Hand over the set resolved from the submitting directory. Without
            # it the factory looks for a config of its own, and a leg pointed
            # at a checkout gets whatever is found near the target instead of
            # what this command resolved and reported.
            branch = await create_agent(
                spec,
                chat_model=chat_model,
                log_config=DataLoggerConfig(auto_save_on_exit=False),
                load_settings=False,
                resolved_mcp_servers=mcp_resolution.servers,
                resolved_mcp_explicit=mcp_resolution.explicit,
            )
            # The hand-over happened inside create_agent, so the request it
            # produced is the only honest source for what this leg is getting.
            _report_mcp_resolution(
                mcp_resolution,
                provider=provider,
                cwd=cwd,
                forwarded=request_carries_forwarded_mcp(branch),
            )
        else:
            branch = Branch(
                chat_model=chat_model,
                log_config=DataLoggerConfig(auto_save_on_exit=False),
            )
            # A bare `-a <profile>` leg still honors the profile's khive_injection
            # opt-in (a context-provider concern, independent of the coding preset),
            # keyed on `{agent_name}-recall-v1` to match the orchestrate path. Pass
            # agent_name (guaranteed non-empty here), NOT profile.name — the latter's
            # eager attribute access fires before the helper's guard can early-return.
            if profile is not None:
                from lionagi.agent.factory import register_profile_injection

                register_profile_injection(branch, agent_name, profile)

        # Fail fast, before allocate_run/setup_agent_persist: a bad provider
        # prefix (e.g. 'gpt-5.3-codex-spark' vs 'codex/gpt-5.3-codex-spark')
        # must not persist a run/session recorded as a failed reliability event.
        # New-branch only — a resume model override never swaps the provider.
        if not branch.chat_model.is_cli:
            cli_provider = getattr(branch.chat_model.endpoint.config, "provider", provider)
            raise ConfigurationError(
                f"run operation only supports CLI endpoints, but got provider={cli_provider!r}. "
                "Use one of the CLI endpoint prefixes: claude_code, codex, gemini-cli, pi. "
                "Did you mean 'gemini-cli/<model>' instead of 'gemini/<model>'? "
                "The 'gemini' prefix routes to the REST API, not the local Gemini CLI."
            )
    else:
        cfg = branch.chat_model.endpoint.config.kwargs
        if model_str is not None:
            old_model = cfg.get("model")
            if model != old_model:
                from lionagi.cli._logging import warn

                warn(f"resume model override: {old_model} → {model}")
            cfg["model"] = model
        if verbose:
            cfg["verbose_output"] = True
        if theme is not None:
            cfg["cli_display_theme"] = theme
        if effort is not None:
            kwarg = PROVIDER_EFFORT_KWARG.get(provider)
            if kwarg:
                if provider == "codex":
                    effort = _clamp_codex_effort(effort, cfg.get("model"))
                elif provider in _CLAUDE_PROVIDER_NAMES:
                    effort = _clamp_claude_effort(effort, cfg.get("model") or "")
                cfg[kwarg] = effort
            elif provider in PROVIDERS_EFFORT_VIA_MODEL_NAME:
                # agy (Antigravity CLI) has no effort kwarg — fold effort into
                # the resolved --model name instead (see resolve_agy_model).
                from lionagi.providers.google.gemini_code import resolve_agy_model

                cfg["model"] = resolve_agy_model(
                    cfg.get("model"),
                    effort=effort,
                    reapply_effort=model_str is None,
                )
        if bypass:
            cfg.update(PROVIDER_BYPASS_KWARGS.get(provider, {}))
        elif yolo:
            cfg.update(PROVIDER_YOLO_KWARGS.get(provider, {}))
        if fast:
            cfg.update(PROVIDER_FAST_KWARGS.get(provider, {}))
        # A resumed leg re-spawns a CLI child, so it needs the server set handed
        # to it just as a new one does; the persisted branch carries the model,
        # not the caller's directory. Which providers can be given a set, and
        # over which transport, is not this call site's question to answer.
        from lionagi.agent.factory import (
            apply_forwarded_mcp_servers,
            request_kwargs_carry_forwarded_mcp,
        )

        apply_forwarded_mcp_servers(
            cfg,
            mcp_resolution.servers,
            provider=provider,
            exclusive=not mcp_resolution.servers,
        )
        # A resumed leg re-spawns from the persisted request, which is the only
        # thing that decides what it carries — read the answer off it.
        _report_mcp_resolution(
            mcp_resolution,
            provider=provider,
            cwd=cwd,
            forwarded=request_kwargs_carry_forwarded_mcp(cfg),
        )

    # Skip the profile system prompt for a branch that carries (or would carry)
    # a create_agent-composed system message — full rationale in
    # docs/internals/cli.md. Brand-new branch: `took_create_agent_path` is
    # authoritative. Resumed branch: only the immutable
    # CREATE_AGENT_BRANCH_ORIGIN_KEY marker counts as provenance — never
    # re-derived from persisted content (a markerless branch with an
    # explicitly requested role must get that role's
    # system prompt rather than have it silently dropped).
    if is_resumed_branch:
        from lionagi.agent.factory import CREATE_AGENT_BRANCH_ORIGIN_KEY

        composed_via_create_agent = bool(branch.metadata.get(CREATE_AGENT_BRANCH_ORIGIN_KEY))
    else:
        composed_via_create_agent = took_create_agent_path
    if profile and profile.system_prompt and not composed_via_create_agent:
        branch.msgs.add_message(system=profile.system_prompt)

    if timeout is not None:
        preamble = build_deadline_preamble(timeout)
        prompt = preamble + prompt

    run = allocate_run()
    branch_id = str(branch.id)
    resolved_model_spec = _provenance.resolve_model_spec(provider, model)
    run_manifest = {
        "branch_id": branch_id,
        "agent_name": agent_name,
        "provider": provider,
        "model": resolved_model_spec,
        "status": "running",
        "started_at": time.time(),
        "ended_at": None,
    }
    if context_from:
        run_manifest["context_from"] = list(context_from)
    _write_run_manifest = getattr(run, "write_manifest", None)
    if _write_run_manifest is not None:
        _write_run_manifest(run_manifest)

    artifact_contract = resolve_artifact_contract(
        playbook_artifacts=None,
        agent_defaults=profile.artifact_defaults if profile else None,
    )
    live = await setup_agent_persist(
        branch,
        agent_name=agent_name,
        artifacts_path=str(run.artifact_root),
        artifact_contract=artifact_contract,
        invocation_id=invocation_id,
        model=resolved_model_spec,
        provider=provider,
        effort=effort,
        project=project,
        run_id=run.run_id,
        run_manifest=run_manifest,
        # This runner drains queued operator messages at turn end and
        # tombstones whatever it did not take, so controls aimed at it have a
        # consumer.
        drains_controls=True,
    )
    if seed_injection_totals:
        await _seed_injection_stats(live, _injection_totals)

    # Session-scoped: teardown_agent_persist terminalizes only the session;
    # invocation records are finalized externally and would never fire. Deferred
    # auto-resume legs unregister without firing; the recursed leg registers anew.
    #
    # If persistence setup failed there's no session entity to fire a terminal
    # transition on, so register_flow_notify_scope can't be used -- this run
    # instead delivers the notice itself once its own terminal status is known,
    # in the `finally` block below (see `deliver_flow_notify_now`): same
    # resolution and payload, run directly instead of registered for a
    # transition that will never happen. The refusal record this run can still
    # write applies only when delivery is actually attempted and genuinely
    # can't complete, never merely because persistence failed.
    _notify_scope_name: str | None = None
    _notify_session_id = live.get("session_id") if live else None
    if notify and _notify_session_id is not None:
        from lionagi.cli.orchestrate._notify import (
            register_flow_notify_scope,
            unregister_flow_notify_scope,
        )
        from lionagi.state.lifecycle.notify_settings import (
            record_notify_rejection_to_run,
        )

        def _notify_override_refused(reason: str) -> None:
            # This run explicitly asked for a notifier and will not get one.
            # Recording it here is what keeps a refusal distinguishable from
            # never having configured one; both otherwise register nothing.
            record_notify_rejection_to_run(run, reason)

        _notify_scope_name = register_flow_notify_scope(
            override=notify,
            entity_kind="session",
            entity_id=_notify_session_id,
            invocation_id=invocation_id,
            flow_kind="agent",
            playbook=None,
            save_dir=str(run.artifact_root),
            cwd=cwd or os.getcwd(),
            started_at=run_manifest["started_at"],
            on_rejection=_notify_override_refused,
        )

    # Bind this run into the notify.on_terminal handler at registration time so
    # a late outcome lands here or nowhere, never on a later run. Skipped when
    # --notify already owns this entity (a second override would double-fire).
    _notify_outcome_scope_name: str | None = None
    if not notify and _notify_session_id is not None:
        from lionagi.state.lifecycle.notify_settings import (
            register_run_notify_outcome_scope,
            unregister_run_notify_outcome_scope,
        )

        _notify_outcome_scope_name = register_run_notify_outcome_scope(
            run,
            entity_kind="session",
            entity_id=_notify_session_id,
            project_dir=cwd,
        )

    _terminal_status = "completed"
    _terminal_exc: BaseException | None = None
    _propagating = False
    _direct_notice_sent = False

    async def _deliver_direct_notice() -> None:
        """Send this run's one terminal notice on the no-persistence route.

        No session entity ever existed for this run, so the registered path
        was never reached and nothing else will ever deliver for it. *When*
        this is called is the whole question: it reads ``_terminal_status`` at
        call time, so it must run only after every line that can still change
        that status has run.

        Shielded, since a cancellation is exactly what's expected from the
        teardown path that calls this, and the guarantee this exists to
        provide is that a terminal notice arrives regardless. Idempotent by
        flag (reached from both a ``finally`` and the ordinary tail) rather
        than by relying on those two call sites never overlapping.
        """
        nonlocal _direct_notice_sent

        if not notify or _notify_session_id is not None or _direct_notice_sent:
            return
        _direct_notice_sent = True
        import anyio as _anyio

        with _anyio.CancelScope(shield=True):
            try:
                from lionagi.cli.orchestrate._notify import deliver_flow_notify_now

                _reason_code, _, _ = resolve_run_reason(
                    status=_terminal_status, exception=_terminal_exc
                )
                await deliver_flow_notify_now(
                    override=notify,
                    run=run,
                    entity_kind="session",
                    entity_id=branch_id,
                    invocation_id=invocation_id,
                    flow_kind="agent",
                    playbook=None,
                    save_dir=str(run.artifact_root),
                    cwd=cwd or os.getcwd(),
                    started_at=run_manifest["started_at"],
                    terminal_status=_terminal_status,
                    reason_code=_reason_code,
                    occurred_at=time.time(),
                )
            except Exception as _notify_exc:  # noqa: BLE001 — a notifier failure must never affect the run
                log_error(f"direct-path notify.on_terminal delivery failed: {_notify_exc!r}")

    # Armed unconditionally, not only when --timeout is set: the steer receipt
    # ack below is the operator's only signal that a queued message was
    # received (vs. lost) before the turn ends, and a leg spawned without a
    # timeout is exactly the case where a turn can run long enough for that
    # distinction to matter. The extra task is one sleeping coroutine per leg,
    # negligible next to the provider call it watches.
    _heartbeat_task = None
    import asyncio as _asyncio
    import time as _hb_time

    _hb_report = _ProgressReport(branch, _hb_time.monotonic())

    async def _heartbeat_loop():
        while True:
            await _asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            line = _hb_report.line(_hb_time.monotonic())
            # Steer receipt ack: a queued operator message cannot land
            # mid-turn, so tell the operator it was received and when it
            # will apply. Fail-safe — the heartbeat never crashes the leg.
            if live and live.get("session_id"):
                try:
                    _pending = await live["db"].list_pending_session_controls(live["session_id"])
                    _n = sum(1 for c in _pending if c.get("verb") == "message")
                    if _n:
                        line += f"  [steer queued x{_n} — lands at end of current turn]"
                except Exception:  # noqa: BLE001, S110 — ack is best-effort color
                    pass
            hint(line)

    try:
        _heartbeat_task = _asyncio.ensure_future(_heartbeat_loop())
    except RuntimeError:
        _heartbeat_task = None

    _leg_deadline = (time.monotonic() + timeout) if timeout is not None else None
    try:
        res = await branch.operate(
            instruction=prompt,
            stream_persist=True,
            persist_dir=str(run.stream_dir),
            snapshot_dir=str(run.branches_dir),
            timeout=timeout,
            **({"images": images} if images else {}),
            **({"repo": cwd} if cwd else {}),
        )
        steer_res = await _drain_pending_steers(
            live,
            branch,
            operate_kwargs={
                "stream_persist": True,
                "persist_dir": str(run.stream_dir),
                "snapshot_dir": str(run.branches_dir),
                "timeout": timeout,
                **({"repo": cwd} if cwd else {}),
            },
            deadline=_leg_deadline,
            # The run id, because it is what identifies THIS leg. The branch is
            # shared with every leg that resumes it, so a branch-keyed claim
            # could not tell two legs apart, which is the whole point of naming
            # the claimant.
            owner=run.run_id,
        )
        if steer_res is not None:
            res = steer_res
    except (TimeoutError, LionTimeoutError) as exc:
        _terminal_status = "timed_out"
        _terminal_exc = exc
        from lionagi.mcp._terminal_cause import write_terminal_cause

        # Written on the timeout path too, even though a timeout is never one of
        # the typed provider errors. A recorded `unknown` says the cause was
        # looked at and was not a provider error; no record at all says nobody
        # looked, and a reader cannot tell those apart after the fact.
        write_terminal_cause(exc)
        from lionagi.cli._logging import warn

        warn(f"agent timed out after {timeout}s")
        last = branch.msgs.last_response
        res = (last.response if last else "") or None
    except BaseException as exc:
        _terminal_status = classify_exception(exc)
        _terminal_exc = exc
        from lionagi.mcp._terminal_cause import write_terminal_cause

        # Before the re-raise: this is the last point that still holds the
        # exception object, and the hook that records the run's end runs in a
        # different process where only its own class name would survive.
        write_terminal_cause(exc)
        # Nothing after this try block runs, so the teardown below is this
        # run's last chance to notify. Every other path falls through to the
        # tail, where the status is still being decided.
        _propagating = True
        if _terminal_status == "failed":
            # Default traceback printing is unreliable under SIGTERM/process
            # death — leave a one-line diagnostic before it propagates.
            log_error(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # See docs/internals/cli.md for why an about-to-auto-resume leg must
        # not stamp a terminal status here (ADR-0035 terminal guard).
        will_auto_resume = (
            _terminal_status == "timed_out" and resume_on_timeout and not _auto_resumed
        )
        if _heartbeat_task is not None:
            _heartbeat_task.cancel()
            import asyncio as _asyncio2
            import contextlib as _contextlib

            with _contextlib.suppress(_asyncio2.CancelledError, Exception):
                await _heartbeat_task

        # Shield teardown so iModel shutdown always runs (avoids leaked
        # rate-limit replenisher tasks that hang anyio.run forever).
        import anyio

        with anyio.CancelScope(shield=True):
            # Engine session id, used by teardown to tell a genuine failure
            # from a wrapper exception racing a still-live engine session.
            _engine_session_uid = getattr(branch.chat_model.endpoint, "session_id", None)
            registry = getattr(branch, "_context_providers", None)
            injection_stats = getattr(registry, "stats", None) if registry is not None else None
            _fold_injection_stats(_injection_totals, injection_stats)
            telemetry_extras = (
                {"khive_injection": dict(_injection_totals)}
                if not will_auto_resume and any(_injection_totals.values())
                else None
            )
            telemetry_kw = {"extras": telemetry_extras} if telemetry_extras else {}
            effective_status = await teardown_agent_persist(
                live,
                status=_terminal_status,
                exception=_terminal_exc,
                cwd=cwd,
                engine_session_uid=_engine_session_uid,
                defer_terminal=will_auto_resume,
                **telemetry_kw,
            )
            # Terminal-race tombstone, ordered after the teardown above (skipped
            # when auto-resume keeps the run alive -- the resumed leg's drain
            # consumes the steer instead). This ordering leaves no gap for a
            # control to slip through: the writer admits one only while the
            # session reads 'running', so anything admitted lands before the
            # transition and is visible to the sweep below, while anything
            # arriving later is refused at the writer. Teardown can also fail
            # and return the requested status without writing it, so the sweep
            # re-reads the stored session and declines a non-terminal one
            # rather than trusting call order.
            #
            # The `live` handle is closed by teardown's own `finally` by this
            # point, so the sweep gets a fresh connection rather than the
            # corpse -- a sweep that fails on a closed engine would otherwise
            # turn this whole tombstone path into one log line while the rows
            # it exists to close stay pending forever. Opened here (not inside
            # the sweep) so callers supplying their own handle, including
            # tests, keep doing so; it's still inside the sweep's must-not-raise
            # boundary, since StateDB re-raises out of __aenter__ and an
            # unguarded `async with` here would turn a completed run into a
            # reported infrastructure exception.
            if not will_auto_resume and live:
                from lionagi.state.db import StateDB

                try:
                    async with StateDB() as _sweep_db:
                        await _tombstone_pending_steers({**live, "db": _sweep_db})
                except Exception as _sweep_exc:  # noqa: BLE001 — teardown must not raise
                    log_error(f"steer tombstone write failed: {_sweep_exc!r}")
            if effective_status != _terminal_status:
                _terminal_status = effective_status
            from lionagi.state.db import SESSION_TERMINAL_STATUSES

            run_manifest["status"] = _terminal_status
            if _terminal_status in SESSION_TERMINAL_STATUSES:
                run_manifest["ended_at"] = time.time()
            if _write_run_manifest is not None:
                _write_run_manifest(run_manifest)
            # Only when an exception is on its way out, because then this is
            # the last code this run executes. On every other path the status
            # is not settled yet: an empty resumed stream becomes `failed`
            # below, and a leg about to auto-resume has no terminal answer of
            # its own at all — the resumed leg carries the notice. Sending
            # from here regardless is how a notifier was told `timed_out`
            # about a run that went on to complete.
            if _propagating:
                await _deliver_direct_notice()
            # Unregister after teardown fires the terminal transition.
            if _notify_scope_name is not None:
                unregister_flow_notify_scope(_notify_scope_name)
            if _notify_outcome_scope_name is not None:
                from lionagi.state.lifecycle.notify_settings import (
                    unregister_run_notify_outcome_scope,
                )

                unregister_run_notify_outcome_scope(_notify_outcome_scope_name)
            await branch.mdls.shutdown()

    is_resume = bool(resume or continue_last)
    if is_resume and _terminal_status == "completed" and not (res or "").strip():
        log_error(
            f"resume produced empty stream — session may be expired; "
            f"re-run without -r (resume target: {resume or 'last'})"
        )
        _terminal_status = "failed"
        run_manifest["status"] = _terminal_status
        run_manifest["ended_at"] = time.time()
        if _write_run_manifest is not None:
            _write_run_manifest(run_manifest)

    # Bookkeeping, and the terminal notice below has not gone out yet. This call
    # reports its own failures rather than raising for exactly that reason: a
    # run that finished still owes its answer to whoever asked to be told, and
    # an unwritable convenience pointer is not a reason to withhold it.
    save_last_branch_pointer(run.run_id, branch_id)

    session_id = live.get("session_id") if live else None

    if _terminal_status == "timed_out" and resume_on_timeout and not _auto_resumed:
        from lionagi.cli._logging import warn

        # Deliberately keyed on this branch rather than on the `will_auto_resume`
        # the teardown computed: that one is read before the effective status is
        # applied, so the two can disagree, and a notice suppressed on a
        # recursion that then does not happen is a leg that never reports at all.
        # The recursive call owns the notice, which is what makes it the final
        # status rather than an interim one.
        warn(
            f"[auto-resume] session {session_id or branch_id} timed out after "
            f"{timeout}s — resuming once with 'continue and conclude the task'"
        )
        # Carry the model forward explicitly — None would let the profile's
        # model silently re-apply on the resumed leg, switching models.
        _effective_cfg = branch.chat_model.endpoint.config
        _effective_model_str = f"{_effective_cfg.provider}/{_effective_cfg.kwargs.get('model')}"
        return await _run_agent(
            _effective_model_str,
            "continue and conclude the task",
            yolo=yolo,
            verbose=verbose,
            theme=theme,
            resume=branch_id,
            effort=effort,
            agent_name=agent_name,
            cwd=cwd,
            timeout=timeout,
            fast=fast,
            invocation_id=invocation_id,
            project=project,
            bypass=bypass,
            resume_on_timeout=resume_on_timeout,
            notify=notify,
            mcp_config=mcp_config,
            no_mcp_config=no_mcp_config,
            _auto_resumed=True,
            _injection_totals=_injection_totals,
        )

    # Past every line that can still move the status, and past the recursion
    # that would have made this leg an interim one, so this is the run's answer.
    await _deliver_direct_notice()

    return res or "", provider, branch_id, _terminal_status, session_id


def add_agent_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    agent = subparsers.add_parser(
        "agent",
        help="Spawn one-shot subagent (blocking); prints final response.",
        description=(
            "Spawn a single subagent and wait for its final response. "
            "Flags may appear anywhere relative to the positionals. "
            "Use -r / -c to continue a previous conversation. "
            "Use -a to load a profile from .lionagi/agents/. "
            "Use --preset to apply a built-in agent configuration. "
            "Use --form to load and validate structured inputs before invoking. "
            "Use --context-from to hand a new agent distilled context from a "
            "prior session/branch/run/file."
        ),
    )
    agent.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Optional model spec followed by the prompt. Model is one of "
            "'claude', 'codex', 'gemini-code' (defaults), or a full spec like "
            "'claude/opus'; omit it when -a / --resume / -c provides one. "
            "The prompt may instead be passed via --prompt or --prompt-file."
        ),
    )
    agent.add_argument(
        "--prompt",
        dest="prompt_flag",
        metavar="TEXT",
        default=None,
        help=(
            "The instruction, as a flag instead of the positional PROMPT. Give it one way or "
            "the other, never both."
        ),
    )
    agent.add_argument(
        "--prompt-file",
        metavar="PATH",
        default=None,
        help=(
            "Read the instruction from a file; '-' reads stdin, which is heredoc-friendly. The "
            "file is read once at spawn, so editing it afterwards cannot change the run."
        ),
    )
    agent.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load agent profile by name. Resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md, "
            "then a trusted+enabled plugin's declared profile "
            "('<plugin>/<NAME>', or a bare NAME when only one plugin declares it). "
            "Profile provides system prompt, default model, effort, yolo, "
            "timeout, resume_on_timeout. CLI flags override profile settings."
        ),
    )
    agent.add_argument(
        "--list-profiles",
        action="store_true",
        help=(
            "Print every agent profile -a would resolve, as JSON, and exit without running "
            "anything. Use it to find out what names are available here."
        ),
    )
    agent.add_argument(
        "-r",
        "--resume",
        metavar="BRANCH_ID",
        default=None,
        help=(
            "Continue a previous run by its branch id, keeping that conversation's history. "
            "Cannot be combined with --context-from, which is for starting fresh."
        ),
    )
    agent.add_argument(
        "-c",
        "--continue-last",
        action="store_true",
        help=(
            "Continue the most recently used branch, keeping its history, without having to "
            "look up its id."
        ),
    )
    agent.add_argument(
        "--preset",
        choices=_PRESET_CHOICES,
        default=None,
        metavar="NAME",
        help=(
            "Apply a built-in agent configuration preset. "
            f"Supported values: {', '.join(_PRESET_CHOICES)}. "
            "'coding' wires CodingToolkit with path-guard hooks "
            "and a coding system prompt; cwd defaults to the invocation directory."
        ),
    )
    agent.add_argument(
        "--form",
        metavar="SPEC",
        default=None,
        help=(
            "Path to a YAML or JSON work-form spec file. "
            "The spec declares typed fields and values; validation runs "
            "BEFORE any LLM call. Exits rc=1 on validation error. "
            "Validated values are injected into the prompt as structured context."
        ),
    )

    agent.add_argument(
        "--image",
        dest="image",
        metavar="PATH",
        action="append",
        default=None,
        help=(
            "Attach an image file to the prompt (repeatable, e.g. "
            "--image a.png --image b.jpg). Supported extensions: "
            f"{', '.join(sorted(_IMAGE_MEDIA_TYPES))}. Each file is read, "
            "base64-encoded, and attached as an image content part on the "
            "user message."
        ),
    )
    agent.add_argument(
        "--context-from",
        dest="context_from",
        metavar="REF",
        action="append",
        default=None,
        help=(
            "Inject distilled context from a prior session id, branch id, run id, "
            "or file path into this new branch's first instruction, above the "
            "prompt. Repeatable (concatenated in argv order); the total budget "
            "(see --context-budget) is shared across all refs. Rejected in "
            "combination with -r / --resume or -c / --continue-last."
        ),
    )
    agent.add_argument(
        "--context-budget",
        dest="context_budget",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Total token budget for --context-from content "
            f"(default {DEFAULT_CONTEXT_BUDGET_TOKENS}; ~4 chars/token)."
        ),
    )

    agent.add_argument(
        "--mcp-config",
        dest="mcp_config",
        metavar="PATH",
        default=None,
        help=(
            "Read this MCP config and hand its servers to the leg explicitly. "
            "By default the nearest .mcp.json at or above the directory this "
            "command was run in is used, so the leg's tools come from the "
            "submission rather than from --cwd. The file is read once at spawn."
        ),
    )
    agent.add_argument(
        "--no-mcp-config",
        dest="no_mcp_config",
        action="store_true",
        help=(
            "Hand the leg no MCP servers, and say so deliberately instead of "
            "arriving there by an empty search."
        ),
    )
    add_common_cli_args(agent)
    return agent


def _resolve_model_and_prompt(args: argparse.Namespace) -> tuple[str | None, str] | None:
    """Assign the positional bucket + --prompt/--prompt-file to (model, prompt).

    Returns None after logging a clear error."""
    query: list[str] = getattr(args, "query", None) or []
    flag_prompt = args.prompt_flag
    if args.prompt_file:
        if flag_prompt is not None:
            log_error("pass --prompt or --prompt-file, not both")
            return None
        if args.prompt_file == "-":
            flag_prompt = sys.stdin.read()
        else:
            try:
                flag_prompt = Path(args.prompt_file).read_text()
            except OSError as exc:
                log_error(f"could not read --prompt-file: {exc}")
                return None
        if not flag_prompt.strip():
            log_error(f"--prompt-file {args.prompt_file!r} is empty")
            return None
    if len(query) > 2:
        log_error(
            "too many positional arguments — expected [MODEL] PROMPT. "
            "Did you forget to quote the prompt?"
        )
        return None
    if flag_prompt is not None:
        if len(query) == 2:
            log_error("prompt given twice (positionally and via --prompt/--prompt-file)")
            return None
        return (query[0] if query else None), flag_prompt
    if len(query) == 2:
        return query[0], query[1]
    if len(query) == 1:
        return None, query[0]
    log_error("no prompt given — pass it positionally, or via --prompt / --prompt-file")
    return None


@auto_register(area="agent", cli=CliDeclaration(seed="agent", parser_factory=add_agent_subparser))
def run_agent(args: argparse.Namespace) -> int:
    """Dispatch agent command."""
    if getattr(args, "list_profiles", False):
        from lionagi.cli._providers import build_agent_profile_catalog

        print(json.dumps(build_agent_profile_catalog(), indent=2, sort_keys=True))
        return 0
    resolved = _resolve_model_and_prompt(args)
    if resolved is None:
        return 1
    model, prompt_text = resolved
    # --form: load, build, and validate BEFORE any LLM call.
    form_prompt_prefix: str = ""
    if getattr(args, "form", None):
        try:
            spec = _load_form_spec(args.form)
        except FileNotFoundError as exc:
            log_error(str(exc))
            return 1
        except ValueError as exc:
            log_error(str(exc))
            return 1

        try:
            work_form = _build_work_form(spec, args.form)
        except ValueError as exc:
            log_error(str(exc))
            return 1

        if work_form.status == "error":
            errs = "; ".join(work_form.validation_errors)
            log_error(f"form validation failed ({args.form}): {errs}")
            return 1

        # Validated — build a context block to prepend to the prompt.
        if work_form.values:
            form_prompt_prefix = _form_to_context_block(work_form) + "\n\n"

    prompt = form_prompt_prefix + prompt_text

    # --image: load and validate BEFORE any LLM call, same contract as --form.
    image_uris: list[str] | None = None
    if getattr(args, "image", None):
        try:
            image_uris = _load_image_data_uris(args.image)
        except (FileNotFoundError, ValueError) as exc:
            log_error(str(exc))
            return 1

    has_model = model is not None or args.agent is not None
    if not has_model and not (args.resume or args.continue_last):
        log_error(
            "model or --agent is required unless --resume / -r or --continue-last / -c is set"
        )
        return 1

    try:
        result, provider, branch_id, terminal_status, session_id = run_async(
            _run_agent(
                model,
                prompt,
                yolo=args.yolo,
                verbose=args.verbose,
                theme=args.theme,
                resume=args.resume,
                continue_last=args.continue_last,
                effort=args.effort,
                agent_name=args.agent,
                cwd=args.cwd,
                timeout=args.timeout,
                fast=args.fast,
                invocation_id=getattr(args, "invocation", None),
                project=getattr(args, "project", None),
                bypass=getattr(args, "bypass", False),
                preset=getattr(args, "preset", None),
                resume_on_timeout=getattr(args, "resume_on_timeout", False),
                context_from=getattr(args, "context_from", None),
                context_budget=getattr(args, "context_budget", None),
                notify=getattr(args, "notify", None),
                images=image_uris,
                mcp_config=getattr(args, "mcp_config", None),
                no_mcp_config=getattr(args, "no_mcp_config", False),
            )
        )
    except McpConfigError as exc:
        # An explicitly named --mcp-config that cannot be used: refuse at spawn,
        # rather than starting a leg whose tool surface is not what was asked for.
        log_error(str(exc))
        return 2
    except ContextFromError as exc:
        log_error(str(exc))
        return 2
    except KeyboardInterrupt:
        return EXIT_CODE_BY_STATUS["aborted"]
    except SigtermInterrupt as exc:
        from lionagi.cli._logging import warn

        warn(f"agent terminated by SIGTERM: {exc}")
        return EXIT_CODE_BY_STATUS["cancelled"]
    except BaseException as exc:
        if isinstance(exc, cancelled_exc_classes()):
            return EXIT_CODE_BY_STATUS["cancelled"]
        log_error(f"{type(exc).__name__}: {exc}")
        raise

    if not args.verbose:
        print(f"\n{result}" if result is not None else "", flush=True)

    hint(f'\n[to resume] li agent -r {branch_id} "..."')
    if session_id:
        hint(f"[status]    li agent status {session_id}")
    return EXIT_CODE_BY_STATUS.get(terminal_status, 1)

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped file layout: authoritative state in LIONAGI_HOME/runs/{run_id}/, artifacts in --save dir or state_root/artifacts/."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lionagi._paths import RUNS_ROOT
from lionagi.libs.path_safety import validate_path_component
from lionagi.ln._utils import now_utc
from lionagi.providers._provider_errors import ProviderError
from lionagi.utils import LIONAGI_HOME

if TYPE_CHECKING:
    from lionagi import Branch
    from lionagi.state.db import StateDB

__all__ = (
    "LIONAGI_HOME",
    "RUNS_ROOT",
    "RunDir",
    "allocate_run",
    "find_branch",
    "load_last_branch",
    "save_last_branch_pointer",
    "list_runs",
    "current_run_id",
    "resolve_run_reason",
    "setup_agent_persist",
    "teardown_persist",
    "teardown_agent_persist",
    "teardown_orchestration_persist",
)
_LEGACY_AGENTS_ROOT = LIONAGI_HOME / "logs" / "agents"
_LAST_BRANCH_POINTER = LIONAGI_HOME / "last_branch.json"
_RUN_ID_ENV_VAR = "LIONAGI_RUN_ID"


def _new_run_id() -> str:
    ts = now_utc().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


def current_run_id() -> str | None:
    """Return the run_id inherited from the environment (subprocess case)."""
    return os.environ.get(_RUN_ID_ENV_VAR) or None


@dataclass(frozen=True, slots=True)
class RunDir:
    """Resolved state and artifact paths for one CLI run."""

    run_id: str
    state_root: Path
    artifact_root: Path

    # ── Path helpers ────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.state_root / "run.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.state_root / "checkpoint.json"

    @property
    def branches_dir(self) -> Path:
        return self.state_root / "branches"

    @property
    def stream_dir(self) -> Path:
        return self.state_root / "stream"

    def branch_path(self, branch_id: str) -> Path:
        return self.branches_dir / f"{branch_id}.json"

    def stream_buffer_path(self, branch_id: str) -> Path:
        return self.stream_dir / f"{branch_id}.buffer.jsonl"

    def agent_artifact_dir(self, agent_id: str) -> Path:
        """Return artifact dir for agent_id, rejecting any id that resolves outside artifact_root (path-traversal guard)."""
        try:
            validate_path_component(agent_id, label="agent_id")
        except ValueError as exc:
            raise ValueError(f"agent_id {agent_id!r} is not a safe path component") from exc
        candidate = (self.artifact_root / agent_id).resolve()
        root = self.artifact_root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"agent_id {agent_id!r} resolves outside artifact_root {root}"
            ) from exc
        return self.artifact_root / agent_id

    @property
    def synthesis_path(self) -> Path:
        return self.artifact_root / "synthesis.md"

    @property
    def flow_log_path(self) -> Path:
        return self.artifact_root / "flow.log"

    @property
    def dag_image_path(self) -> Path:
        return self.artifact_root / "flow_dag.png"

    # ── Manifest I/O ────────────────────────────────────────────────

    def write_manifest(self, data: dict) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "state_root": str(self.state_root),
            "artifact_root": str(self.artifact_root),
            **data,
        }
        self.manifest_path.write_text(json.dumps(payload, indent=2))

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text())

    # ── Notify-outcome I/O (separate from the manifest; see notify_settings.py) ──

    @property
    def notify_outcome_path(self) -> Path:
        return self.state_root / "notify_outcome.json"

    def write_notify_outcome(self, data: dict) -> None:
        """Atomically replace notify_outcome.json (tmp + os.replace); never
        merges with a prior outcome and never touches the manifest."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        tmp_path = self.notify_outcome_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2))
        os.replace(tmp_path, self.notify_outcome_path)

    # ── Directory setup ─────────────────────────────────────────────

    def ensure_state_dirs(self) -> None:
        self.branches_dir.mkdir(parents=True, exist_ok=True)
        self.stream_dir.mkdir(parents=True, exist_ok=True)

    def ensure_artifact_root(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)


def allocate_run(
    save_dir: str | os.PathLike | None = None,
    run_id: str | None = None,
) -> RunDir:
    """Allocate a run dir, inheriting run_id from LIONAGI_RUN_ID env var if set (subprocess handoff)."""
    rid = run_id or current_run_id() or _new_run_id()
    state_root = RUNS_ROOT / rid

    if save_dir is not None:
        artifact_root = Path(save_dir).expanduser().resolve()
    else:
        artifact_root = state_root / "artifacts"

    run = RunDir(run_id=rid, state_root=state_root, artifact_root=artifact_root)
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    run.write_manifest(
        {
            "status": "running",
            "started_at": time.time(),
            "ended_at": None,
        }
    )
    return run


# ── Branch lookup (canonical + legacy fallback) ─────────────────────────


def find_branch(branch_id: str) -> tuple[str | None, Path]:
    """Locate a branch JSON; returns (run_id, path), run_id=None for legacy logs/agents/ storage."""
    if RUNS_ROOT.exists():
        # Prefer an exact hit, fall back to prefix match (branch UUIDs may
        # have been truncated by the user when resuming).
        for run_dir in sorted(
            RUNS_ROOT.iterdir(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            if not run_dir.is_dir():
                continue
            branches = run_dir / "branches"
            if not branches.exists():
                continue
            exact = branches / f"{branch_id}.json"
            if exact.exists():
                return run_dir.name, exact
            for match in branches.glob(f"{branch_id}*.json"):
                return run_dir.name, match

    if _LEGACY_AGENTS_ROOT.exists():
        for provider_dir in sorted(_LEGACY_AGENTS_ROOT.iterdir()):
            if not provider_dir.is_dir():
                continue
            exact = provider_dir / branch_id
            if exact.exists():
                return None, exact
            for match in provider_dir.glob(f"{branch_id}*"):
                return None, match

    raise FileNotFoundError(f"No branch log found for id {branch_id!r}")


# ── Last-branch pointer (with legacy schema compat) ─────────────────────


def load_last_branch() -> tuple[str | None, str]:
    """Read the last-branch pointer; returns (run_id, branch_id), run_id=None for pre-run-scoped schema."""
    if not _LAST_BRANCH_POINTER.exists():
        raise FileNotFoundError(
            f"No last-branch pointer at {_LAST_BRANCH_POINTER}. "
            "Run `li agent <model> <prompt>` at least once before using -c."
        )
    data = json.loads(_LAST_BRANCH_POINTER.read_text())
    branch_id = data["branch_id"]
    run_id = data.get("run_id")  # None for legacy pointers
    return run_id, branch_id


def save_last_branch_pointer(run_id: str, branch_id: str) -> None:
    LIONAGI_HOME.mkdir(parents=True, exist_ok=True)
    _LAST_BRANCH_POINTER.write_text(json.dumps({"run_id": run_id, "branch_id": branch_id}))


# ── Introspection ───────────────────────────────────────────────────────


def list_runs(limit: int | None = None) -> list[RunDir]:
    """Return all runs under RUNS_ROOT, newest first (by mtime)."""
    if not RUNS_ROOT.exists():
        return []
    dirs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        dirs = dirs[:limit]
    out: list[RunDir] = []
    for d in dirs:
        manifest_path = d / "run.json"
        artifact_root = d / "artifacts"
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text())
                art = m.get("artifact_root")
                if art:
                    artifact_root = Path(art)
            except (OSError, json.JSONDecodeError):
                pass
        out.append(RunDir(run_id=d.name, state_root=d, artifact_root=artifact_root))
    return out


# ── Live-persist lifecycle (absorbed from _persist.py) ──────────────────────

_log = logging.getLogger("lionagi.cli")


def resolve_run_reason(
    *,
    status: str,
    exception: BaseException | None,
) -> tuple[str, str, list[dict] | None]:
    from lionagi.state.reasons import RunReasons

    if status == "completed":
        return RunReasons.COMPLETED_OK, "Run completed successfully.", None
    if status == "completed_empty":
        return (
            RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
            "Run exited clean but produced no commits ahead of base and no artifacts.",
            None,
        )
    if status == "timed_out":
        return RunReasons.TIMED_OUT_DEADLINE, "Run exceeded the configured timeout.", None
    if status == "aborted":
        return RunReasons.CANCELLED_SIGINT, "User pressed Ctrl-C (SIGINT).", None
    if status == "cancelled":
        from lionagi.ln.concurrency.utils import (
            SigtermInterrupt,
            consume_sigterm_received,
        )

        # At teardown the surfaced exception is usually a plain CancelledError
        # even when an external SIGTERM caused it — SigtermInterrupt is only
        # raised after the worker thread joins, after this record is stamped.
        # The handler's process-wide latch is the reliable signal. Consume it
        # unconditionally (before the branch) so a later, unrelated run/test
        # can't inherit a latch left set on the explicit-SigtermInterrupt path.
        sigterm_latched = consume_sigterm_received()
        if isinstance(exception, SigtermInterrupt) or sigterm_latched:
            return (
                RunReasons.CANCELLED_SIGTERM,
                "sigterm_external: process received an external SIGTERM mid-run.",
                None,
            )
        return (
            RunReasons.CANCELLED_SYSTEM,
            "Task cancelled by the runtime (anyio CancelledError).",
            None,
        )
    if exception is not None:
        if isinstance(exception, ProviderError):
            code = (
                RunReasons.FAILED_PROVIDER_RETRYABLE
                if exception.retryable
                else RunReasons.FAILED_PROVIDER_NONRETRYABLE
            )
            return code, f"{type(exception).__name__}: {exception}", None
        return RunReasons.FAILED_EXCEPTION, f"{type(exception).__name__}: {exception}", None
    return RunReasons.FAILED_EXCEPTION, "Run failed.", None


async def _linked_engine_session(
    db: StateDB,
    engine_session_uid: str | None,
    *,
    retries: int = 3,
    retry_interval: float = 0.1,
) -> dict[str, Any] | None:
    """The claude/codex-mirror session row for a CLI provider's real engine session.

    Retries a bounded number of times, since the mirror row may not be written yet at teardown.
    """
    if not engine_session_uid:
        return None
    import anyio

    from lionagi.state.claude_mirror import session_db_id

    db_id = session_db_id(engine_session_uid)
    linked = await db.get_session(db_id)
    if linked is not None:
        return linked
    for _ in range(retries):
        await anyio.sleep(retry_interval)
        linked = await db.get_session(db_id)
        if linked is not None:
            return linked
    return None


async def _teardown_common(
    db: StateDB,
    *,
    session_id: str,
    session_prog_id: str,
    status: str,
    exception: BaseException | None,
    artifacts_path: str | None,
    artifact_contract: dict | None,
    extras: dict | None = None,
    identity_markers: dict | None = None,
    escalated_evidence: list[dict] | None = None,
    cwd: str | None = None,
    engine_session_uid: str | None = None,
    defer_terminal: bool = False,
) -> str:
    from lionagi.state.artifact_verifier import (
        missing_artifact_evidence,
        missing_artifact_summary,
        verify_artifact_contract,
    )

    if defer_terminal:
        # A resumed leg on this same session owns the real terminal write (ADR-0035);
        # skip the DB mutation here and let the caller's non-status bookkeeping run.
        return status

    all_msgs = await db.get_progression(session_prog_id)
    completion_evidence_msgs = list(all_msgs)
    update_kwargs: dict[str, Any] = {"ended_at": time.time()}
    if all_msgs:
        update_kwargs["first_msg_id"] = all_msgs[0]
        update_kwargs["last_msg_id"] = all_msgs[-1]

    if extras:
        markers = identity_markers or {}
        update_kwargs["node_metadata"] = json.dumps({**extras, **markers})

    await db.update_session(session_id, **update_kwargs)

    reason_code, reason_summary, evidence_refs = resolve_run_reason(
        status=status, exception=exception
    )
    metadata: dict | None = None
    if exception is not None:
        metadata = {"exception_class": type(exception).__name__}

    session_row = await db.get_session(session_id) or {}
    contract = artifact_contract or session_row.get("artifact_contract_json")
    artifacts_root = artifacts_path or session_row.get("artifacts_path")
    verification = verify_artifact_contract(contract, artifacts_root=artifacts_root)
    await db.update_artifact_verification(session_id, verification)

    final_status = status
    final_reason_code = reason_code
    final_reason_summary = reason_summary
    final_evidence_refs = evidence_refs

    # Suppress a phantom "failed" only for this exact unclassified ProviderError class
    # when the linked engine session is still alive/completed; exact-type (not isinstance)
    # so genuine ProviderQuotaError/AuthError/ContextError subclasses still fail loud.
    if final_status == "failed" and type(exception) is ProviderError and engine_session_uid:
        from lionagi.state.claude_mirror import session_db_id
        from lionagi.state.db import SESSION_TERMINAL_STATUSES
        from lionagi.state.reasons import RunReasons

        linked_id = session_db_id(engine_session_uid)
        linked = await _linked_engine_session(db, engine_session_uid)

        # Record the link durably (id is deterministic) so `li monitor run <id>` can
        # resolve status later, even if this teardown's bounded wait ran out first.
        metadata = dict(metadata or {})
        metadata["linked_engine_session_id"] = linked_id
        existing_node_meta = session_row.get("node_metadata") or {}
        if isinstance(existing_node_meta, str):
            existing_node_meta = json.loads(existing_node_meta)
        await db.update_session(
            session_id,
            node_metadata=json.dumps({**existing_node_meta, "linked_engine_session_id": linked_id}),
        )

        if linked is not None and linked["status"] in SESSION_TERMINAL_STATUSES:
            reason_by_status = {
                "completed": RunReasons.COMPLETED_OK,
                "completed_empty": RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
                "failed": RunReasons.FAILED_EXCEPTION,
                "timed_out": RunReasons.TIMED_OUT_DEADLINE,
                "aborted": RunReasons.CANCELLED_SIGINT,
                "cancelled": RunReasons.CANCELLED_SYSTEM,
            }
            final_status = linked["status"]
            final_reason_code = reason_by_status.get(linked["status"], RunReasons.FAILED_EXCEPTION)
            final_reason_summary = (
                f"reconciled to linked engine session {linked_id} terminal status "
                f"{linked['status']!r}"
            )
            final_evidence_refs = [{"kind": "session", "id": linked_id, "label": linked["status"]}]
            linked_prog_id = linked.get("progression_id")
            if linked_prog_id:
                completion_evidence_msgs.extend(await db.get_progression(linked_prog_id))
        elif linked is not None and linked["status"] == "running":
            final_status = "running"
            final_reason_code = RunReasons.STARTED_OK
            final_reason_summary = (
                f"suppressed phantom 'failed': linked engine session {linked_id} is still running"
            )
            final_evidence_refs = [{"kind": "session", "id": linked_id, "label": "running"}]
        # else: engine uid was captured but no mirror row landed within the
        # bounded wait — can't confirm the engine is alive, so `failed` stands.

    if verification and verification["status"] == "failed":
        from lionagi.state.reasons import RunReasons

        missing = verification["missing_required"]
        if final_status == "completed":
            final_status = "failed"
            final_reason_code = RunReasons.FAILED_MISSING_ARTIFACT
            final_reason_summary = missing_artifact_summary(missing)
            final_evidence_refs = missing_artifact_evidence(missing)
        else:
            metadata = dict(metadata or {})
            metadata["artifact_verification_status"] = verification["status"]
            metadata["missing_required_artifact_ids"] = [
                str(entry.get("id", "")) for entry in missing
            ]

    # Completion-trust gate: don't accept "completed" on faith. Require a git trace
    # (commits ahead/dirty tree) or a durable assistant response as real evidence.
    if final_status == "completed" and not (verification and verification.get("produced")):
        from lionagi.state.completion_evidence import (
            check_completion_evidence,
            has_completion_evidence,
        )
        from lionagi.state.reasons import RunReasons

        evidence = check_completion_evidence(cwd)
        if evidence["checked"]:
            has_output = await _has_assistant_output_evidence(db, completion_evidence_msgs)
            metadata = dict(metadata or {})
            metadata["completion_evidence"] = evidence
            metadata["has_assistant_output"] = has_output
            if not has_completion_evidence(evidence) and not has_output:
                final_status = "completed_empty"
                final_reason_code = RunReasons.COMPLETED_EMPTY_NO_EVIDENCE
                base_label = evidence.get("base_ref") or "base"
                final_reason_summary = (
                    f"No commits ahead of {base_label}, no artifacts produced, and no "
                    "assistant response recorded; working tree clean."
                )
                final_evidence_refs = [
                    {
                        "kind": "git_evidence",
                        "id": "completion_check",
                        "label": (
                            f"base={base_label} "
                            f"commits_ahead={evidence.get('commits_ahead')} "
                            f"dirty={evidence.get('dirty')}"
                        ),
                    }
                ]

    # Escalation backstop: a leg that gave up mid-run via EscalationRequest without
    # an artifact contract must not read as a clean completion.
    if escalated_evidence and final_status == "completed":
        from lionagi.state.reasons import RunReasons

        final_status = "failed"
        final_reason_code = RunReasons.FAILED_ESCALATED
        ids = [str(e.get("id", "")) for e in escalated_evidence]
        final_reason_summary = (
            f"{len(escalated_evidence)} operation(s) escalated without producing "
            f"required output: {', '.join(ids)}."
        )
        final_evidence_refs = escalated_evidence

    from lionagi.state.db import SESSION_TERMINAL_STATUSES, TransitionRejectedError

    # Snapshot of status observed at the start of this teardown; used only as the
    # CAS guard below (not updated_at, which this function may itself have touched).
    pre_write_status = session_row.get("status")

    if pre_write_status in SESSION_TERMINAL_STATUSES:
        # Already terminal before this teardown attempted anything (e.g. reattached
        # to a session an earlier run already finalized) -- skip the redundant write
        # and report this invocation's own outcome (ADR-0035 protects the earlier record).
        if pre_write_status != final_status:
            _log.warning(
                "session %s already terminal at %r; this invocation's %r "
                "outcome was not persisted (ADR-0094 protects the earlier "
                "terminal record)",
                session_id,
                pre_write_status,
                final_status,
            )
        else:
            _log.debug(
                "session %s already terminal at %r; skipping duplicate status write",
                session_id,
                pre_write_status,
            )
    else:
        try:
            written = await db.update_status(
                "session",
                session_id,
                new_status=final_status,
                reason_code=final_reason_code,
                reason_summary=final_reason_summary,
                evidence_refs=final_evidence_refs,
                source="executor",
                actor=session_id,
                metadata=metadata,
                expected_statuses={pre_write_status},
            )
            if not written:
                # CAS miss: a concurrent teardown of the same session won the race.
                # Read back the persisted status rather than raising past callers.
                persisted = await db.get_session(session_id) or {}
                final_status = persisted.get("status", final_status)
                _log.debug(
                    "session %s status changed under this teardown; using persisted status %s",
                    session_id,
                    final_status,
                )
        except TransitionRejectedError:
            # Defensive fallback: the row became terminal between this
            # teardown's snapshot and the write despite the CAS guard above.
            persisted = await db.get_session(session_id) or {}
            final_status = persisted.get("status", final_status)
            _log.debug(
                "session %s already terminal (%s); skipped duplicate status write",
                session_id,
                final_status,
            )
    return final_status


async def _has_assistant_output_evidence(db: StateDB, message_ids: list[str]) -> bool:
    """Walk the progression newest-first; a non-empty assistant message counts as
    durable completion evidence even when there's no commit, dirty tree, or artifact."""
    for message_id in reversed(message_ids):
        msg = await db.get_message(message_id)
        if not msg or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                content = {"assistant_response": content}
        text_val = ""
        if isinstance(content, dict):
            text_val = str(content.get("assistant_response") or content.get("content") or "")
        elif content:
            text_val = str(content)
        if text_val.strip():
            return True
    return False


def _resolve_project(project: str | None) -> tuple[str | None, str | None]:
    if project:
        return project, "explicit"
    from lionagi.cli._project import detect_project

    return detect_project()


async def teardown_persist(
    ctx: dict | None,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
    extras: dict | None = None,
    escalated_evidence: list[dict] | None = None,
    cwd: str | None = None,
    engine_session_uid: str | None = None,
    defer_terminal: bool = False,
) -> str:
    if ctx is None:
        return status

    db = ctx["db"]
    try:
        await _flush_pending_message_events(ctx)
        final_status = await _teardown_common(
            db,
            session_id=ctx["session_id"],
            session_prog_id=ctx["session_prog_id"],
            status=status,
            exception=exception,
            artifacts_path=ctx.get("artifacts_path"),
            artifact_contract=ctx.get("artifact_contract"),
            extras=extras,
            identity_markers=ctx.get("identity_markers"),
            escalated_evidence=escalated_evidence,
            cwd=cwd,
            engine_session_uid=engine_session_uid,
            defer_terminal=defer_terminal,
        )

        from lionagi.hooks import unroute_message_persistence
        from lionagi.hooks.bus import HookPoint

        hook = ctx.get("hook")
        if hook is not None:
            unroute_message_persistence(ctx["branch"], hook)
        for branch, h in ctx.get("hooks", []):
            unroute_message_persistence(branch, h)

        session_obj = ctx.get("session")
        # Skip SESSION_END here when deferred: the resumed leg's own (non-deferred)
        # teardown emits it once, carrying cumulative usage for both legs.
        if session_obj is not None and not defer_terminal:
            err_str = str(exception) if exception is not None else None
            _usage: dict = {}
            _branch = ctx.get("branch")
            # Orchestrator/DAG sessions never set a singular ctx["branch"];
            # every leg (including the orchestrator branch itself) is
            # tracked in ctx["hooks"] as (branch, handler) pairs instead.
            _hook_branches = [b for b, _h in ctx.get("hooks", [])]
            try:
                if _branch is not None:
                    from lionagi.session.signal import _collect_branch_usage

                    _usage = _collect_branch_usage(_branch)
                elif _hook_branches:
                    from lionagi.session.signal import _collect_multi_branch_usage

                    _usage = _collect_multi_branch_usage(_hook_branches)
            except Exception:  # noqa: BLE001, S110
                pass

            # BRANCH_END: finalize terminal status/ended_at for every branch
            # this teardown owns. The single-branch agent path (ctx["branch"])
            # never gets branches.status written anywhere else, so this is its
            # only finalize. The multi-leg DAG path (ctx["hooks"]) already gets
            # per-op status from flow.py's NodeCompleted/NodeFailed handlers;
            # this is the safety net for legs that never reached a terminal
            # signal (queued-but-never-started, or still "running" when the
            # DAG itself raised) -- persist_branch_end()/finalize_branch()'s
            # own guard skips any branch a per-op writer already finalized.
            #
            # final_status is only ever a genuine terminal outcome
            # (SESSION_TERMINAL_STATUSES) EXCEPT for one case: the
            # linked-engine reconciliation above suppresses a phantom
            # "failed" back to "running" when the real engine session is
            # still alive -- this teardown's own view of the branch is not
            # actually done. Never emit BRANCH_END for that case; a branch
            # must never be stamped "ended" with a non-terminal status.
            # finalize_branch() also rejects a non-terminal status outright,
            # so this is belt-and-suspenders, not the only guard.
            from lionagi.state.db import SESSION_TERMINAL_STATUSES

            if final_status in SESSION_TERMINAL_STATUSES:
                _end_at = time.time()
                for _b in [_branch] if _branch is not None else _hook_branches:
                    await session_obj.hooks.emit(
                        HookPoint.BRANCH_END,
                        branch_id=str(_b.id),
                        status=final_status,
                        ended_at=_end_at,
                    )

            await session_obj.hooks.emit(
                HookPoint.SESSION_END,
                session_id=ctx["session_id"],
                status=final_status,
                error=err_str,
                **_usage,
            )

        # Detach signal persistence so the observer handler cannot fire after
        # teardown (the db handle is about to be closed in the finally block).
        if session_obj is not None:
            try:
                session_obj.observer.unbind_db_persistence()
            except Exception as _exc:  # noqa: BLE001
                _log.debug("signal persist unbind failed: %s", _exc)

        return final_status
    except Exception as exc:
        _log.warning("live persist teardown failed: %s", exc, exc_info=True)
        return status
    finally:
        # Release branch ownership even when the bookkeeping above failed -- a
        # stranded owner marker would make the long-lived branch unresumable.
        _session_obj = ctx.get("session")
        if _session_obj is not None:
            for _b in [ctx.get("branch"), *(b for b, _h in ctx.get("hooks", []))]:
                if _b is None:
                    continue
                try:
                    _session_obj.remove_branch(_b)
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("branch ownership release failed: %s", _exc)
        try:
            await db.close()
        except Exception as exc:
            _log.warning("live persist db.close failed: %s", exc, exc_info=True)
        # Sweep the shared-db registry (our connection plus any stray a hook
        # opened) so no non-daemon aiosqlite worker thread blocks process exit.
        from lionagi.state.db import close_shared_db

        await close_shared_db()


# Keep old names as aliases so callers don't break.
teardown_agent_persist = teardown_persist


async def teardown_orchestration_persist(*args, **kwargs) -> str:
    """Deprecated alias for :func:`teardown_persist`; delegates unchanged."""
    warnings.warn(
        "lionagi.cli._runs.teardown_orchestration_persist is deprecated; "
        "use lionagi.cli._runs.teardown_persist instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return await teardown_persist(*args, **kwargs)


async def _open_shared_db():
    """Open a StateDB and register it as the process-wide shared connection."""
    from lionagi.state.db import StateDB, register_shared_db, unregister_shared_db

    db = StateDB()
    try:
        await db.open()
        # Lifecycle hooks reach a db via get_shared_db(); register ours so they reuse
        # this connection rather than opening a second one whose worker thread leaks.
        await register_shared_db(db)
    except Exception:
        try:
            await db.close()
        except Exception as close_exc:
            _log.warning("fallback db.close after open failure also failed: %s", close_exc)
        unregister_shared_db(db)
        raise
    return db


def _make_message_handler(
    db,
    branch_id: str,
    session_id: str,
    branch_prog_id: str,
    session_prog_id: str,
    *,
    dedup_set: set | None = None,
    new_msg_ids_list: list | None = None,
    on_first_msg=None,
    message_retry_queues: list | None = None,
):
    """Return an async _on_message handler for live DB persistence."""
    from copy import deepcopy

    from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent

    retry_queue = MessagePersistRetryQueue(
        db,
        logger=_log,
        owner=f"branch {branch_id}",
    )
    if message_retry_queues is not None:
        message_retry_queues.append(retry_queue)

    async def _on_message(msg):
        try:
            if on_first_msg is not None:
                await on_first_msg()
            msg_dict = msg.to_dict(mode="db")
            msg_id = msg_dict["id"]
            append_to_progressions = dedup_set is None or msg_id not in dedup_set
            on_persisted = None
            if append_to_progressions and new_msg_ids_list is not None:

                def _record_persisted() -> None:
                    new_msg_ids_list.append(msg_id)

                on_persisted = _record_persisted
            await retry_queue.submit(
                PendingMessageEvent(
                    message=deepcopy(msg_dict),
                    session_id=session_id,
                    branch_progression_id=(branch_prog_id if append_to_progressions else None),
                    session_progression_id=(session_prog_id if append_to_progressions else None),
                    system_branch_id=branch_id if msg_dict.get("role") == "system" else None,
                    activity_at=msg_dict.get("created_at"),
                    on_persisted=on_persisted,
                )
            )
        except Exception as exc:
            _log.warning(
                "live persist write failed for branch %s: %s",
                branch_id,
                exc,
                exc_info=True,
            )

    return _on_message


async def _flush_pending_message_events(ctx: dict) -> None:
    """Retry queued messages before teardown reads completion evidence."""
    for retry_queue in ctx.get("message_retry_queues", []):
        await retry_queue.flush()


async def setup_agent_persist(
    branch: Branch,
    *,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
) -> dict | None:
    from lionagi.session.session import Session
    from lionagi.state import provenance as _provenance

    db = None
    session = None
    try:
        # Claim the branch before touching the shared DB registry: registering a
        # shared DB closes the previous handle, which would break its owner's teardown.
        session = Session(name="agent", default_branch=branch)
        session_id = str(session.id)
        branch_id = str(branch.id)

        db = await _open_shared_db()

        existing_branch = await db.get_branch(branch_id)
        if existing_branch:
            session_id = existing_branch["session_id"]
            existing_session = await db.get_session(session_id)
            session_prog_id = existing_session["progression_id"]
            branch_prog_id = existing_branch["progression_id"]

            if session_prog_id is None:
                candidate = str(uuid.uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_session_progression(session_id, candidate)
                session_prog_id = effective or candidate
            if branch_prog_id is None:
                candidate = str(uuid.uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_branch_progression(branch_id, candidate)
                branch_prog_id = effective or candidate

            existing_msg_ids = set(await db.get_progression(branch_prog_id))
        else:
            session_prog_id = str(uuid.uuid4())
            branch_prog_id = str(uuid.uuid4())
            existing_msg_ids = set()

            await db.create_progression(session_prog_id)
            await db.create_progression(branch_prog_id)

            session_dict = session.to_dict(mode="db")
            _proj, _proj_src = _resolve_project(project)
            from lionagi.cli.kill import current_pid_markers

            _node_meta = {**(session_dict.get("node_metadata") or {}), **current_pid_markers()}
            await db.create_session(
                {
                    "id": session_id,
                    "created_at": session_dict["created_at"],
                    "node_metadata": _node_meta,
                    "name": session_dict.get("name"),
                    "user": session_dict.get("user"),
                    "progression_id": session_prog_id,
                    "first_msg_id": None,
                    "last_msg_id": None,
                    "invocation_kind": "agent",
                    "agent_name": agent_name,
                    "artifacts_path": artifacts_path,
                    "artifact_contract_json": artifact_contract,
                    "status": "running",
                    "started_at": time.time(),
                    "invocation_id": invocation_id,
                    "model": model,
                    "provider": provider,
                    "effort": effort,
                    "agent_hash": _provenance.agent_definition_hash(agent_name),
                    "project": _proj,
                    "project_source": _proj_src,
                }
            )

            system_msg_id = None
            if branch.system:
                sys_dict = branch.system.to_dict(mode="db")
                system_msg_id = sys_dict["id"]
                await db.insert_message(sys_dict)

            branch_dict = branch.to_dict(mode="db")
            node_meta = branch_dict.get("node_metadata") or {}
            if isinstance(node_meta, str):
                node_meta = json.loads(node_meta)
            if "chat_model" in branch_dict:
                node_meta["chat_model"] = branch_dict["chat_model"]

            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": branch_dict["created_at"],
                    "node_metadata": node_meta,
                    "user": branch_dict.get("user"),
                    "name": branch_dict.get("name"),
                    "session_id": session_id,
                    "progression_id": branch_prog_id,
                    "system_msg_id": system_msg_id,
                    "model": model,
                    "provider": provider,
                    "agent_name": agent_name,
                }
            )

            from lionagi.hooks.bus import HookPoint

            await session.hooks.emit(
                HookPoint.SESSION_START,
                session_id=session_id,
                model=model,
                provider=provider,
                effort=effort,
                agent_name=agent_name,
                agent_hash=_provenance.agent_definition_hash(agent_name),
                invocation_id=invocation_id,
            )
            await session.hooks.emit(
                HookPoint.BRANCH_CREATE,
                branch_id=branch_id,
                model=model,
                provider=provider,
                agent_name=agent_name,
            )

        new_msg_ids: list = []
        message_retry_queues: list = []
        ctx = {
            "db": db,
            "session": session,
            "branch": branch,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_id": branch_prog_id,
            "existing_msg_ids": existing_msg_ids,
            "new_msg_ids": new_msg_ids,
            "message_retry_queues": message_retry_queues,
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
        }

        _on_message = _make_message_handler(
            db,
            branch_id,
            session_id,
            branch_prog_id,
            session_prog_id,
            dedup_set=existing_msg_ids,
            new_msg_ids_list=new_msg_ids,
            message_retry_queues=message_retry_queues,
        )

        # Bind through the already-open DB so signals land in session_signals
        # without opening a new connection per signal.
        session.observer.bind_db_persistence(session_id, db=db)

        from lionagi.hooks import route_message_persistence

        ctx["hook"] = route_message_persistence(session, branch, _on_message)
        return ctx
    except Exception as exc:
        _log.warning(
            "live persist setup failed (%s) — disabling persistence for this run",
            exc,
            exc_info=True,
        )
        # If the wrapper session already claimed the branch, release it so a
        # later setup (or retry) can wrap the same branch again.
        if session is not None:
            try:
                session.remove_branch(branch)
            except Exception as release_exc:  # noqa: BLE001
                _log.debug("branch ownership release failed: %s", release_exc)
        if db is not None:
            try:
                await db.close()
            except Exception as close_exc:
                _log.warning("fallback db.close after setup failure also failed: %s", close_exc)
            # Drop the now-closed handle so get_shared_db() can't hand it out.
            from lionagi.state.db import unregister_shared_db

            unregister_shared_db(db)
        return None

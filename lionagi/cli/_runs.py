# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped file layout: authoritative state in LIONAGI_HOME/runs/{run_id}/, artifacts in --save dir or state_root/artifacts/."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid4

from lionagi._paths import RUNS_ROOT, ensure_lionagi_dir
from lionagi.cli._util import AmbiguousIdError, mark_run_allocated
from lionagi.libs.path_safety import validate_path_component
from lionagi.ln._json_dump import raise_if_non_finite
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
    "active_run_id",
    "resolve_run_reason",
    "setup_agent_persist",
    "find_incomplete_session_for_run",
    "teardown_persist",
    "teardown_agent_persist",
    "teardown_orchestration_persist",
)
_LEGACY_AGENTS_ROOT = LIONAGI_HOME / "logs" / "agents"
_LAST_BRANCH_POINTER = LIONAGI_HOME / "last_branch.json"
_RUN_ID_ENV_VAR = "LIONAGI_RUN_ID"
PERSISTENCE_DEGRADED_REASON_FIELD = "persistence_degraded_reason"


def _new_run_id() -> str:
    ts = now_utc().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid4().hex[:6]}"


def current_run_id() -> str | None:
    """Return the run_id inherited from the environment (subprocess case)."""
    return os.environ.get(_RUN_ID_ENV_VAR) or None


# The run this process most recently allocated. Kept here rather than in the
# environment because allocate_run() reads the environment to *inherit* an id,
# so exporting one would make a second allocation in the same process silently
# reuse the first run's directory.
_ALLOCATED_RUN_ID: str | None = None


def active_run_id() -> str | None:
    """The run_id of the run this process is recording under, if any.

    The run this process allocated, falling back to one inherited from a
    parent. None when nothing has allocated a run yet — an embedded caller,
    or the window before ``allocate_run`` runs.
    """
    return _ALLOCATED_RUN_ID or current_run_id()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* to *path* so readers never see a partial file.

    The temp file is uniquely named and lives in the destination directory
    (os.replace is only atomic within a filesystem), so concurrent writers
    of the same target cannot corrupt each other's in-progress write.

    A non-finite float is refused before anything is written: json.dumps would
    emit the tokens ``NaN``/``Infinity``, which this process reads back happily
    and every strict reader rejects.
    """
    raise_if_non_finite(payload)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


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
        """Replace run.json with *data* plus this run's identity fields.

        The write is atomic (a uniquely-named temp file in the same
        directory, then os.replace), so a concurrent reader observes either
        the previous manifest or the new one, never a truncated file. It is
        a whole-file replacement, not a merge: the caller owns the full
        manifest contents, and two writers racing on one run still resolve
        last-writer-wins.
        """
        ensure_lionagi_dir(self.state_root)
        payload = {
            "run_id": self.run_id,
            "state_root": str(self.state_root),
            "artifact_root": str(self.artifact_root),
            **data,
        }
        _atomic_write_json(self.manifest_path, payload)

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text())

    # ── Notify-outcome I/O (separate from the manifest; see notify_settings.py) ──

    @property
    def notify_outcome_path(self) -> Path:
        return self.state_root / "notify_outcome.json"

    def write_notify_outcome(self, data: dict) -> None:
        """Atomically replace notify_outcome.json; never merges with a prior
        outcome and never touches the manifest."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.notify_outcome_path, data)

    @property
    def notify_stderr_path(self) -> Path:
        return self.state_root / "notify_stderr.log"

    def write_notify_stderr(self, text: str) -> Path:
        """Capture a notify adapter's stderr to an owner-only file (0600).

        Adapter output is free text that may carry a credential from any
        source, so it is written once, readable only by the user running
        the process, and referenced by path everywhere else instead of
        being copied into records or log lines.
        """
        self.state_root.mkdir(parents=True, exist_ok=True)
        path = self.notify_stderr_path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    # ── Directory setup ─────────────────────────────────────────────

    def ensure_state_dirs(self) -> None:
        ensure_lionagi_dir(self.branches_dir)
        ensure_lionagi_dir(self.stream_dir)

    def ensure_artifact_root(self) -> None:
        ensure_lionagi_dir(self.artifact_root)


def _record_persistence_degraded(
    exc: BaseException,
    *,
    run: RunDir | None = None,
    run_id: str | None = None,
    run_manifest: dict[str, Any] | None = None,
) -> str:
    """Record why lifecycle persistence was disabled outside the failed store."""
    reason = repr(exc)
    if run_manifest is not None:
        run_manifest[PERSISTENCE_DEGRADED_REASON_FIELD] = reason
    resolved_id = run.run_id if run is not None else run_id
    manifest_path = (
        run.manifest_path
        if run is not None
        else (RUNS_ROOT / resolved_id / "run.json" if resolved_id else None)
    )
    if manifest_path is None:
        _log.warning("could not record persistence degradation: no active run id")
        return reason
    try:
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict):
            raise TypeError("run manifest is not a JSON object")
        if run_manifest is not None:
            manifest.update(run_manifest)
        manifest[PERSISTENCE_DEGRADED_REASON_FIELD] = reason
        _atomic_write_json(manifest_path, manifest)
    except Exception as record_exc:  # noqa: BLE001 — degradation must not become a run failure
        _log.warning(
            "could not record persistence degradation for run %s: %r",
            resolved_id,
            record_exc,
            exc_info=True,
        )
    return reason


def allocate_run(
    save_dir: str | os.PathLike | None = None,
    run_id: str | None = None,
) -> RunDir:
    """Allocate a run dir, consuming an inherited run id as a one-hop handoff."""
    global _ALLOCATED_RUN_ID

    inherited_run_id = current_run_id()
    rid = run_id or inherited_run_id or _new_run_id()
    if inherited_run_id is not None:
        os.environ.pop(_RUN_ID_ENV_VAR, None)
    _ALLOCATED_RUN_ID = rid
    state_root = RUNS_ROOT / rid

    if save_dir is not None:
        artifact_root = Path(save_dir).expanduser().resolve()
    else:
        artifact_root = state_root / "artifacts"

    run = RunDir(run_id=rid, state_root=state_root, artifact_root=artifact_root)
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    # From here on there is durable state on disk under this run id, so a later
    # failure is a failed run and must not be reported as an unusable
    # environment. Marked here rather than at the call sites so every caller,
    # including ones added later, is covered.
    mark_run_allocated()
    run.write_manifest(
        {
            "status": "running",
            "started_at": time.time(),
            "ended_at": None,
        }
    )
    return run


def _branch_dirs() -> list[tuple[str | None, Path, str]]:
    """Every directory that can hold a branch file, with its run id and suffix.

    Newest run first, so an exact hit is found in the order a caller expects.
    """
    places: list[tuple[str | None, Path, str]] = []
    if RUNS_ROOT.exists():
        for run_dir in sorted(RUNS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            branches = run_dir / "branches"
            if run_dir.is_dir() and branches.exists():
                places.append((run_dir.name, branches, ".json"))
    if _LEGACY_AGENTS_ROOT.exists():
        places.extend(
            (None, provider_dir, "")
            for provider_dir in sorted(_LEGACY_AGENTS_ROOT.iterdir())
            if provider_dir.is_dir()
        )
    return places


def find_branch(branch_id: str) -> tuple[str | None, Path]:
    """Locate a branch JSON; returns (run_id, path), run_id=None for legacy logs/agents/ storage.

    Branch ids may be given truncated, so a prefix is accepted. It is resolved
    in two passes over every place a branch can live rather than one pass that
    accepts whatever it finds first: an exact id is a complete answer and must
    win wherever it lives, and a prefix that fits more than one branch has no
    correct winner. Resuming acts, so picking one of several would silently put
    a new leg on a branch the caller did not name.
    """
    places = _branch_dirs()

    for run_id, directory, suffix in places:
        exact = directory / f"{branch_id}{suffix}"
        if exact.exists():
            return run_id, exact

    matches: list[tuple[str | None, Path]] = []
    for run_id, directory, suffix in places:
        # startswith re-checks the glob: a case-insensitive filesystem matches
        # names the id does not actually prefix.
        matches.extend(
            (run_id, match)
            for match in sorted(directory.glob(f"{branch_id}*{suffix}"))
            if match.name.startswith(branch_id)
        )

    if len(matches) > 1:
        raise AmbiguousIdError(
            branch_id, "branch", [m.name.removesuffix(".json") for _, m in matches]
        )
    if matches:
        return matches[0]

    raise FileNotFoundError(f"No branch log found for id {branch_id!r}")


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
    """Record which branch `--continue` should pick up next. Best effort.

    This is a convenience pointer, and it is written late — after a run has
    produced its answer and, in the agent path, before that answer's terminal
    notice goes out. Letting a filesystem error escape from here would cost the
    caller the notice and the return value both, to protect a file whose only
    job is to save someone typing a branch id.

    So the failure is reported and not raised. It is reported rather than
    swallowed because the next `--continue` will silently pick up an older run,
    and that is confusing precisely when nobody remembers this warning.
    """
    from lionagi.cli._logging import warn

    try:
        ensure_lionagi_dir(LIONAGI_HOME)
        _LAST_BRANCH_POINTER.write_text(json.dumps({"run_id": run_id, "branch_id": branch_id}))
    except Exception as exc:  # noqa: BLE001 — a convenience pointer never fails a run
        _log.warning("could not write the last-branch pointer: %r", exc, exc_info=exc)
        warn(
            f"could not record this run as the last branch ({exc}); "
            f"`li agent -c` will resume an earlier run instead of this one"
        )


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
    failed_operation_evidence: list[dict] | None = None,
    spawn_refusal_evidence: list[dict] | None = None,
    finalize_error: dict | None = None,
    artifact_write_error: dict | None = None,
    gate_rejected_evidence: list[dict] | None = None,
    message_loss: MessageLoss | None = None,
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
        # skip the status mutation here and let the caller's non-status bookkeeping run.
        #
        # The loss is the exception, and it is not a status write. This leg is the only
        # one that ever knows about it -- its queue is unrouted straight after this and
        # the resuming leg builds its own -- so leaving it here means the eventual
        # terminal write reports a clean run over a transcript missing part of itself.
        if message_loss:
            from lionagi.state.reasons import MESSAGE_LOSS_KEY_PREFIX

            try:
                # This leg's own loss under its own key. A session can time out twice
                # before anything terminal, and reading the field to rewrite it would
                # drop whichever concurrent leg wrote between the read and the write;
                # the terminal write reads every key, so nothing needs merging here.
                # Serialized, because the merge refuses a nested patch value: sqlite and
                # postgres merge nested objects differently and it will not persist state
                # that depends on the backend.
                await db.merge_session_node_metadata(
                    session_id,
                    {f"{MESSAGE_LOSS_KEY_PREFIX}{uuid4().hex}": json.dumps(message_loss)},
                )
            except Exception:
                # This path's job is to defer, and the caller reads the status it
                # returns to decide whether to resume. Letting a bookkeeping write
                # decide that would trade a run that resumes for one that hangs
                # unresumed, to save an annotation. Loud, because the annotation
                # existed to stop exactly this kind of silent loss.
                _log.exception(
                    "Could not record %d lost message event(s) for deferred session "
                    "%s; the resuming leg's terminal row will not carry them",
                    message_loss["lost"],
                    session_id,
                )
        return status

    all_msgs = await db.get_progression(session_prog_id)
    completion_evidence_msgs = list(all_msgs)

    # Fetched before this call's own write so started_at reflects session
    # creation, not a value this same update is about to touch.
    session_before_teardown = await db.get_session(session_id) or {}

    # A leg that timed out on this session recorded its loss and deferred the terminal
    # write. This is that write, so it reports every leg's loss or the earlier ones are
    # gone: the legs that saw them are finished and their queues no longer exist.
    # This leg's own loss stays separate. If the terminal write below turns out to belong
    # to another leg, this one records only what it saw: the earlier losses are already on
    # the row under their own keys, and writing them again would count them twice.
    own_message_loss = message_loss
    message_loss = _merge_message_loss(
        (session_before_teardown.get("node_metadata") or {}), message_loss
    )

    # ended_at and duration_ms are terminal fields and must land in the same
    # atomic write as the status transition below (see the
    # CAS/TransitionRejectedError branch) -- never written here on their own, or
    # a failed/lost status write leaves a row with status="running" carrying a
    # non-null end time and duration. duration_ms is derived from ended_at, so
    # it inherits that requirement rather than merely resembling it.
    ended_at = time.time()
    duration_ms: float | None = None
    started_at = session_before_teardown.get("started_at")
    if isinstance(started_at, int | float):
        # The prerequisite for telling "never started" / "hung before first
        # request" / "request sent, no response" apart on a terminal row --
        # duration_ms was previously left NULL on every session regardless of
        # outcome, which is loudest on a zero-turn timeout: the record could
        # not even say how long nothing happened for.
        duration_ms = max(0.0, (ended_at - started_at) * 1000)
    update_kwargs: dict[str, Any] = {}
    if all_msgs:
        update_kwargs["first_msg_id"] = all_msgs[0]
        update_kwargs["last_msg_id"] = all_msgs[-1]

    if extras:
        # NOT routed through merge_session_node_metadata(): `extras` here can
        # carry a nested-dict value (finalize_orchestration's
        # "khive_injection" telemetry block, set on env._finalize_extras
        # before this runs -- see _orchestration.py's finalize_orchestration
        # and flow.py/fanout.py's stop_live_persist call in their `finally`),
        # and the atomic merge intentionally rejects a dict-valued patch key
        # (sqlite/postgres would merge it differently -- see
        # StateDB._merge_node_metadata). Switching this call to the atomic
        # merge would turn today's benign-if-racy read-modify-write into a
        # hard ValueError on every run with injection activity. So this stays
        # an open instance of the clobber class that the stale-sweep path in
        # kill.py closes; closing it safely needs a schema decision (flatten
        # khive_injection to scalar keys, or a merge-helper variant that
        # tolerates one level of nested object).
        existing_metadata = session_before_teardown.get("node_metadata") or {}
        if isinstance(existing_metadata, str):
            try:
                existing_metadata = json.loads(existing_metadata)
            except (TypeError, ValueError):
                existing_metadata = {}
        if not isinstance(existing_metadata, dict):
            existing_metadata = {}
        markers = identity_markers or {}
        update_kwargs["node_metadata"] = json.dumps({**existing_metadata, **extras, **markers})

    if update_kwargs:
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
        # Merged atomically -- `linked_id` is a single scalar, so unlike the
        # extras merge above there is no nested-value contract to give up by
        # using merge_session_node_metadata() instead of a read here plus a
        # whole-column update_session() write below.
        metadata = dict(metadata or {})
        metadata["linked_engine_session_id"] = linked_id
        await db.merge_session_node_metadata(session_id, {"linked_engine_session_id": linked_id})

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

    # Node-failure backstop: a DAG operation's invoke() can raise and be
    # recorded EventStatus.FAILED while still folding into
    # completed_operations alongside genuine completions, so a run whose
    # terminal (or any) node died could otherwise read as a clean completion.
    # Runs before the completion-trust gate below, since a fully-failed run
    # usually has no artifacts/commits either and would otherwise be demoted
    # to "completed_empty" before this evidence had a chance to apply.
    if failed_operation_evidence and final_status == "completed":
        from lionagi.state.reasons import RunReasons

        final_status = "failed"
        final_reason_code = RunReasons.FAILED_EXCEPTION
        ids = [str(e.get("id", "")) for e in failed_operation_evidence]
        final_reason_summary = (
            f"{len(failed_operation_evidence)} operation(s) failed: {', '.join(ids)}."
        )
        final_evidence_refs = failed_operation_evidence

    # Escalation backstop: a leg that gave up mid-run via EscalationRequest with
    # no artifact contract must not read as a clean completion. Same ordering
    # reason as the node-failure backstop above.
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

    # Gate-rejection backstop: a gate node rejected mid-DAG and the executor
    # short-circuited its dependent subtree to skipped instead of running those
    # nodes against the rejected baseline. That's a correct, deliberate stop,
    # not a failure -- status stays "completed" -- but the reason code must say
    # so explicitly. Runs before the completion-trust gate for the same reason
    # as the other two backstops, and unlike them leaves final_status at
    # "completed" rather than flipping to "failed", so it must run first to
    # place its evidence before the gate's no-evidence check.
    if gate_rejected_evidence and final_status == "completed":
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["gate_rejections"] = gate_rejected_evidence
        final_reason_code = RunReasons.COMPLETED_GATE_REJECTED
        gate_names = ", ".join(
            str(e.get("label") or e.get("id") or "") for e in gate_rejected_evidence
        )
        final_reason_summary = (
            f"DAG completed successfully; {len(gate_rejected_evidence)} gate(s) rejected "
            f"({gate_names}) and their dependent subtree was short-circuited instead of "
            "running against the rejected baseline."
        )
        final_evidence_refs = gate_rejected_evidence

    # A capacity-refused SpawnRequest is work the live DAG asked to perform
    # but was not allowed to add. The planned graph may still complete, so keep
    # the terminal status at ``completed`` while distinguishing it from a run
    # that never needed to grow. The reason code reaches Studio status and the
    # terminal-callback envelope; the evidence names each requesting node.
    if spawn_refusal_evidence:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["spawn_refusal_count"] = len(spawn_refusal_evidence)
        metadata["spawn_refusals"] = spawn_refusal_evidence
        if final_status == "completed":
            final_reason_code = RunReasons.COMPLETED_SPAWN_REFUSED
            final_reason_summary = (
                f"{len(spawn_refusal_evidence)} reactive spawn request(s) were refused "
                "because the run's spawn capacity was exhausted."
            )
            final_evidence_refs = spawn_refusal_evidence

    # Completion-trust gate: don't accept "completed" on faith. Require a git trace
    # (commits ahead/dirty tree) or a durable assistant response as real evidence.
    # Skipped when gate_rejected_evidence fired above: a gate rejection is itself
    # real evidence of a deliberate stop, and (unlike the node-failure/escalation
    # backstops) it leaves final_status at "completed" rather than "failed", so
    # without this guard the demotion below would still run and overwrite the
    # gate-rejection reason/evidence with a plain no-evidence verdict.
    if (
        final_status == "completed"
        and not gate_rejected_evidence
        and not spawn_refusal_evidence
        and not (verification and verification.get("produced"))
    ):
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
            # "No assistant response recorded" is read off the transcript, and
            # a run that lost messages has a transcript missing exactly the
            # kind of thing this looks for. Concluding emptiness from it states
            # a fact about the run that is really a fact about our record, so
            # the loss below carries the reason instead.
            if not has_completion_evidence(evidence) and not has_output and not message_loss:
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

    # The synthesis artifact IS the run's output. A DAG that completed but
    # whose output write raised has not delivered anything -- that is a real
    # failure of the run, not a best-effort finalize hiccup, so this flips
    # "completed" to "failed" instead of only annotating the reason code the
    # way COMPLETED_FINALIZE_ERROR below does.
    if artifact_write_error:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["artifact_write_error"] = artifact_write_error
        if final_status == "completed":
            final_status = "failed"
            final_reason_code = RunReasons.FAILED_ARTIFACT_WRITE
            final_reason_summary = (
                "DAG completed successfully but writing its output artifact raised "
                f"{artifact_write_error.get('error_class', 'an error')}: "
                f"{artifact_write_error.get('error', '')}"
            )
            final_evidence_refs = [
                {
                    "kind": "artifact_write_error",
                    "id": artifact_write_error.get("error_class", "error"),
                    "label": artifact_write_error.get("error", ""),
                }
            ]

    # A post-completion finalize step (persistence/team-teardown) raised after
    # the DAG itself already produced its result. That failure is real and must
    # not be silently dropped, but it is not a DAG failure either — surface it
    # via reason_code/metadata only, never by overwriting a "completed" status.
    if finalize_error:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["finalize_error"] = finalize_error
        if final_status == "completed":
            final_reason_code = RunReasons.COMPLETED_FINALIZE_ERROR
            final_reason_summary = (
                "DAG completed successfully; a post-completion finalize step raised "
                f"{finalize_error.get('error_class', 'an error')}: "
                f"{finalize_error.get('error', '')}"
            )
            final_evidence_refs = [
                {
                    "kind": "finalize_error",
                    "id": finalize_error.get("error_class", "error"),
                    "label": finalize_error.get("error", ""),
                }
            ]

    # Live-message persistence gave up. The run's own work stands, so this never
    # manufactures a failure and never displaces a reason that is about the work --
    # it goes last, and takes the reason code only if nothing else claimed one. The
    # evidence ref is appended either way: a reader has to be able to find the loss
    # on a run that also failed, or hit a finalize error, or this records it only in
    # the case where nothing else went wrong.
    if message_loss:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["message_persist_loss"] = message_loss
        final_evidence_refs = list(final_evidence_refs or []) + [
            {
                "kind": "message_persist_loss",
                "id": queue["owner"],
                "label": f"{queue['lost']} event(s) lost",
            }
            for queue in message_loss["queues"]
        ]
        if final_status in ("completed", "completed_empty") and (
            final_reason_code in (None, "", RunReasons.COMPLETED_OK)
        ):
            final_reason_code = RunReasons.COMPLETED_MESSAGE_LOSS
            final_reason_summary = (
                f"Run completed; {message_loss['lost']} live message event(s) were "
                "never written and are lost, so this session's transcript is "
                "incomplete."
            )

    from lionagi.state.db import SESSION_TERMINAL_STATUSES, TransitionRejectedError

    # Snapshot of status observed at the start of this teardown; used only as the
    # CAS guard below (not updated_at, which this function may itself have touched).
    pre_write_status = session_row.get("status")

    # Whether this teardown's own terminal write landed. Every path where it did not
    # returns someone else's status, and that row knows nothing about this leg's loss.
    wrote_terminal_record = False

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
                extra_fields=(
                    {"ended_at": ended_at}
                    if duration_ms is None
                    else {"ended_at": ended_at, "duration_ms": duration_ms}
                ),
            )
            wrote_terminal_record = bool(written)
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

    if own_message_loss and not wrote_terminal_record:
        from lionagi.state.reasons import MESSAGE_LOSS_KEY_PREFIX

        # The row that won says the run is clean and this leg is the only one that saw
        # the loss. Leave it where the invocation reducers look, without touching the
        # winner's status or reason -- an earlier terminal record stands.
        try:
            await db.merge_session_node_metadata(
                session_id,
                {f"{MESSAGE_LOSS_KEY_PREFIX}{uuid4().hex}": json.dumps(own_message_loss)},
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not mask the outcome
            _log.warning(
                "session %s: could not record %d lost message event(s) after its "
                "terminal row was written by another teardown: %r",
                session_id,
                own_message_loss["lost"],
                exc,
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
    failed_operation_evidence: list[dict] | None = None,
    spawn_refusal_evidence: list[dict] | None = None,
    finalize_error: dict | None = None,
    artifact_write_error: dict | None = None,
    gate_rejected_evidence: list[dict] | None = None,
    cwd: str | None = None,
    engine_session_uid: str | None = None,
    defer_terminal: bool = False,
) -> str:
    if ctx is None:
        return status

    db = ctx["db"]
    try:
        message_loss = await _flush_pending_message_events(ctx)
        final_status = await _teardown_common(
            db,
            session_id=ctx["session_id"],
            session_prog_id=ctx["session_prog_id"],
            status=status,
            exception=exception,
            message_loss=message_loss,
            artifacts_path=ctx.get("artifacts_path"),
            artifact_contract=ctx.get("artifact_contract"),
            extras=extras,
            identity_markers=ctx.get("identity_markers"),
            escalated_evidence=escalated_evidence,
            failed_operation_evidence=failed_operation_evidence,
            spawn_refusal_evidence=spawn_refusal_evidence,
            finalize_error=finalize_error,
            artifact_write_error=artifact_write_error,
            gate_rejected_evidence=gate_rejected_evidence,
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

            # BRANCH_END safety net for legs that never reached a terminal
            # signal; finalize_branch()'s own guard skips branches a per-op
            # writer already finalized. See docs/internals/cli.md#_runspy-agent-session-setupteardown-adr-0035
            # for why final_status must stay a genuine terminal outcome here.
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
        # A failure anywhere in the block above (any of _teardown_common's
        # separate write transactions, or the bookkeeping around them) must
        # not make this return the *requested* terminal status as if it had
        # landed -- that status may never have reached the database. Read
        # back what is actually durable instead; only fall back to the
        # caller's request if even that read fails.
        try:
            persisted = await db.get_session(ctx["session_id"])
        except Exception:  # noqa: BLE001 -- best-effort; the DB itself may be unreachable
            persisted = None
        if persisted is not None and persisted.get("status"):
            return persisted["status"]
        # The readback itself failed (or found nothing) -- there is no
        # evidence the requested terminal status ever reached the database.
        # Reporting `status` here would be the same false-terminal-status
        # class this teardown path exists to prevent, so surface an
        # explicit unknown outcome instead of a guess.
        return "unknown"
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
        if ctx.get("shared_db_registered", True):
            # Ordinary CLI runs retain the historical sweep. Embedded daemon
            # callers opt out so teardown cannot close unrelated shared state.
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


async def _open_shared_db(*, register: bool = True):
    """Open a StateDB, optionally registering it as the process-wide handle."""
    from lionagi.state.db import StateDB, register_shared_db, unregister_shared_db

    db = StateDB()
    try:
        await db.open()
        if register:
            # Ordinary CLI lifecycle hooks reuse this connection. Embedded
            # callers bind their observer/message handlers directly instead.
            await register_shared_db(db)
    except Exception:
        try:
            await db.close()
        except Exception as close_exc:
            _log.warning("fallback db.close after open failure also failed: %s", close_exc)
        if register:
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


class QueueLoss(TypedDict):
    """One retry queue that gave up, and how much it was holding."""

    owner: str
    lost: int


class MessageLoss(TypedDict):
    """What a teardown could not persist, summed and itemized.

    Written by ``_flush_pending_message_events``, carried across a deferred leg as
    JSON, and read by ``_teardown_common``. Spelled out because those three are in
    different places and a bare dict makes the shape a convention rather than a
    contract.
    """

    lost: int
    queues: list[QueueLoss]


def _as_message_loss(value: Any) -> MessageLoss | None:
    """Coerce a payload parsed from the session row into a ``MessageLoss``.

    The carried value crossed a persistence boundary and may have been written by
    a leg running different code, so its shape is an assumption rather than a
    guarantee. Entries that do not fit are dropped instead of partly trusted: the
    total feeds a lost-event count a reader treats as exact, and an unchecked one
    reaches ``queue["owner"]`` indexing further on. The total is recomputed here
    rather than read, so it agrees with the entries it claims to sum.
    """
    if not isinstance(value, dict):
        return None
    raw = value.get("queues")
    queues: list[QueueLoss] = [
        {"owner": str(q["owner"]), "lost": q["lost"]}
        for q in (raw if isinstance(raw, list) else [])
        if isinstance(q, dict)
        and q.get("owner") is not None
        and isinstance(q.get("lost"), int)
        and not isinstance(q["lost"], bool)
        # A loss record with a zero or negative count is malformed, and summing it
        # produces totals like "-3 event(s) lost".
        and q["lost"] > 0
    ]
    if not queues:
        return None
    return {"lost": sum(q["lost"] for q in queues), "queues": queues}


def _merge_message_loss(node_metadata: Any, current: MessageLoss | None) -> MessageLoss | None:
    """This teardown's loss plus every loss earlier legs left on the session row."""
    from lionagi.state.reasons import carried_message_loss_queues

    carried = carried_message_loss_queues(node_metadata)
    if not carried:
        return current
    return _as_message_loss({"queues": carried + list((current or {}).get("queues") or [])})


async def _flush_pending_message_events(ctx: dict) -> MessageLoss | None:
    """Retry queued messages before teardown reads completion evidence.

    Returns what was lost, or ``None`` when every queue emptied. What follows
    this reads the run's completion evidence, so a queue that gave up here
    means that evidence is being read against a transcript missing messages the
    run produced. Returning the loss rather than a bare failure flag is what
    lets the terminal row say so: a log line lives in one console tail, and the
    tail is not where anyone looks to find out how a run went.
    """
    queues: list[QueueLoss] = []
    for retry_queue in ctx.get("message_retry_queues", []):
        if not await retry_queue.flush_final():
            queues.append({"owner": retry_queue.owner, "lost": retry_queue.pending_count})
    if not queues:
        return None
    return {"lost": sum(q["lost"] for q in queues), "queues": queues}


async def _reopen_session_for_resume(
    db, session_id: str, existing_session: dict | None, *, drains_controls: bool = False
) -> bool:
    """Return a resumed session to ``running`` so its next close is a real change.

    A closing transition only announces itself when the status actually
    changes. A resume adopts a session an earlier leg already took terminal,
    so writing that same terminal status at the end is a no-op: the leg
    finishes silently and the job record never closes. Reopening first
    restores the invariant the rest of the system reads off this column --
    that a session marked terminal is not currently executing.

    Reopening is the only sanctioned exit from a terminal status and carries
    an explicit override rather than a session-policy rule, so each reopening
    stays attributable and terminal exit isn't opened to every writer --
    finality is what the reapers, the teardown guard, and ``li wait`` all rest on.
    """
    from lionagi.cli.kill import current_pid_markers
    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.state.reasons import SessionReasons

    if not existing_session or existing_session.get("status") not in SESSION_TERMINAL_STATUSES:
        # Not terminal: a resume racing a live leg on the same branch. The row
        # already describes the session correctly, so there is nothing to reopen.
        return False

    # Process-liveness markers move in the same transaction as the status
    # update below: a running row must never carry the previous (exited)
    # leg's markers, or a liveness sweep could cancel this live leg.
    # node_metadata is dropped rather than raised on if unreadable — a resume
    # must not fail on its own bookkeeping.
    node_metadata = existing_session.get("node_metadata")
    if isinstance(node_metadata, str):
        try:
            node_metadata = json.loads(node_metadata)
        except ValueError:
            node_metadata = None
    if not isinstance(node_metadata, dict):
        node_metadata = {}

    try:
        applied = await db.update_status(
            "session",
            session_id,
            new_status="running",
            reason_code=SessionReasons.REOPENED_BY_RESUME,
            reason_summary="branch resumed by a new leg",
            source="executor",
            actor=session_id,
            expected_statuses=SESSION_TERMINAL_STATUSES,
            extra_fields={
                "ended_at": None,
                # The control-drain declaration rides this same write. It has to:
                # it describes the leg that is executing now, and so do the
                # process markers beside it. Written separately afterwards, it
                # would be a read-modify-write over the row as it was read
                # *before* this transition, which restores the exited leg's pid
                # and pid_create_time and hands the stale-session doctor grounds
                # to terminalize a live leg. One statement, one set of facts
                # about one leg.
                "node_metadata": json.dumps(
                    {
                        **node_metadata,
                        **current_pid_markers(),
                        "drains_controls": bool(drains_controls),
                    }
                ),
            },
            override=True,
            override_actor="cli.resume",
            override_justification="branch resumed by a new leg; the session is executing again",
        )
    except LookupError:
        # update_status reports a missing row this way. The row was read a
        # moment ago and is gone now: maintenance removes old terminal sessions
        # and holds each candidate for the length of its transaction, so a
        # resume that arrives just before one starts is let through only after
        # it commits, to find nothing there. That is a legitimate outcome
        # rather than a failure of this leg — there is no session to reopen,
        # and the caller falls back to starting one.
        _log.warning(
            "session %s no longer exists and was not reopened for resume; "
            "this leg will record itself under a new session",
            session_id,
        )
        return False

    if not applied:
        # A resumed leg must not fail because its bookkeeping lost a race, but
        # the consequence is worth saying out loud: this leg will finish without
        # announcing itself, and from the outside that is indistinguishable from
        # a leg that is still running.
        _log.warning(
            "session %s was not reopened for resume; another writer moved it "
            "first, so this leg will close without emitting a terminal notice",
            session_id,
        )
        return False

    return True


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
    run_id: str | None = None,
    run_manifest: dict[str, Any] | None = None,
    share_db: bool = True,
    drains_controls: bool = False,
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

        db = await _open_shared_db(register=share_db)

        existing_branch = await db.get_branch(branch_id)
        existing_session = None
        if existing_branch:
            existing_session = await db.get_session(existing_branch["session_id"])
            if existing_session is not None and not await _reopen_session_for_resume(
                db,
                existing_branch["session_id"],
                existing_session,
                drains_controls=drains_controls,
            ):
                # The reopen declines for three reasons and one of them is that
                # the session is no longer there — maintenance removes old
                # terminal sessions, and a resume can arrive as one commits.
                # Confirm the row before reading anything else out of a copy of
                # it that may describe a row that no longer exists.
                existing_session = await db.get_session(existing_branch["session_id"])

            if existing_session is None:
                # Nothing to resume into. Branches are removed with their
                # session, so this leg records itself the way a branch nobody
                # has seen before does, rather than persisting against ids that
                # no longer resolve.
                existing_branch = None

        if existing_branch:
            session_id = existing_branch["session_id"]
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

            if invocation_id and existing_session.get("invocation_id") != invocation_id:
                # A resume reopens this row rather than inserting a new one,
                # so the ON CONFLICT DO NOTHING branch below never runs for
                # it and this leg's invocation_id would otherwise be
                # dropped. Backfill the same linkage a brand-new session
                # gets at insert time.
                await db.attach_session_invocation(session_id, invocation_id)

            # The adopted row's drain declaration is rewritten by the reopen
            # above, in the same statement that installs this leg's process
            # markers. Nothing to do here for a row that was terminal.
            #
            # A resume that did NOT reopen adopts a row still reading running,
            # keeping whatever the earlier leg declared. Three states reach
            # here: explicit True, explicit False, and no declaration (a row
            # written before this field existed) -- the last refuses like
            # False at the admission gate but means "nobody answered", not "no".
            #
            # Only True is risky: if that leg is genuinely alive it's the right
            # answer, but if it died without terminalizing, a stale True admits
            # a control for a drain that's gone -- and a row reading running is
            # the only evidence available either way. The stale-session doctor
            # resolves it after the fact.
            #
            # This path is left alone deliberately, not because it's correct:
            # writing the declaration here would be a read-modify-write against
            # a row a live leg may be updating, the same race that once
            # restored an exited leg's process markers over a live leg's.
            # The narrower cost -- a row reading False or undeclared keeps
            # refusing controls it could actually drain -- is the safer one.
        else:
            session_prog_id = str(uuid.uuid4())
            branch_prog_id = str(uuid.uuid4())
            existing_msg_ids = set()

            await db.create_progression(session_prog_id)
            await db.create_progression(branch_prog_id)

            session_dict = session.to_dict(mode="db")
            _proj, _proj_src = _resolve_project(project)
            from lionagi.cli.kill import current_pid_markers

            # Whether this session's runner consumes operator controls, declared
            # by the caller that starts the run rather than inferred. The
            # control writer refuses a session whose runner has no drain, and it
            # has no other way to tell one agent-kind session from another:
            # every runner that persists through here writes the same kind and
            # a run_id, so run_id presence stopped distinguishing them the
            # moment a second caller began supplying one. Default False so a
            # new runner that forgets to declare it gets a visible refusal
            # rather than a control nobody will ever read.
            _node_meta = {
                **(session_dict.get("node_metadata") or {}),
                **current_pid_markers(),
                "drains_controls": bool(drains_controls),
            }
            await db.create_session(
                {
                    "id": session_id,
                    # Embedded callers may own a RunDir without mutating the
                    # CLI process-global allocation pointer. Ordinary CLI
                    # callers omit this and retain the established behavior.
                    "run_id": run_id if run_id is not None else active_run_id(),
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
            "shared_db_registered": share_db,
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
            "live persist setup failed (%r) — disabling persistence for this run",
            exc,
            exc_info=True,
        )
        _record_persistence_degraded(exc, run_id=run_id, run_manifest=run_manifest)
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
            if share_db:
                # Drop the now-closed handle so get_shared_db() can't hand it out.
                from lionagi.state.db import unregister_shared_db

                unregister_shared_db(db)
        return None


async def find_incomplete_session_for_run(run_id: str) -> dict | None:
    """Recover a session row that ``setup_agent_persist()`` committed before
    failing on a later step, terminalizing it if it is still "running".

    ``setup_agent_persist()`` can call ``create_session()`` and then raise
    inside the same setup (e.g. the following ``create_branch()``); it catches
    that exception and returns None, so its caller sees no context but the
    session row itself is still durable and left running forever. A caller
    whose *run_id* is minted fresh for every attempt (never reused across a
    resume, the way a new operator turn always is) can pass it here after
    setup fails to recover that row -- and close it out -- instead of
    reporting the run as never having existed. Returns None only when no
    session was ever recorded under this run id.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB
    from lionagi.state.reasons import RunReasons

    db = StateDB()
    await db.open()
    try:
        rows = await db.get_sessions_for_run(run_id)
        if not rows:
            return None
        row = rows[-1]
        status = row.get("status")
        if status not in SESSION_TERMINAL_STATUSES:
            await db.update_status(
                "session",
                row["id"],
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary="run setup failed after the session row was committed",
                source="executor",
                actor=row["id"],
                expected_statuses={status},
            )
            row = await db.get_session(row["id"]) or row
        return row
    finally:
        await db.close()

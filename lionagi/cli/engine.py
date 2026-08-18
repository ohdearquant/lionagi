# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li engine run <kind> <spec>` — shell-reachable engine execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from typing import Any

from lionagi._auto import CliDeclaration, auto_register

from ._logging import log_error, progress, warn

# ── Engine kind registry ───────────────────────────────────────────────────
# Maps the CLI kind name to the class import path and the main positional
# argument name and help text.  Adding a new kind means adding one entry here.

_KIND_META: dict[str, dict[str, Any]] = {
    "research": {
        "cls_path": ("lionagi.engines", "ResearchEngine"),
        "pos_arg": "topic",
        "pos_help": "Research topic or question.",
    },
    "review": {
        "cls_path": ("lionagi.engines", "ReviewEngine"),
        "pos_arg": "artifact",
        "pos_help": "Artifact text or path to review.",
    },
    "coding": {
        "cls_path": ("lionagi.engines", "CodingEngine"),
        "pos_arg": "spec",
        "pos_help": "Coding specification (natural-language or JSON string).",
    },
    "hypothesis": {
        "cls_path": ("lionagi.engines", "HypothesisEngine"),
        "pos_arg": "findings",
        "pos_help": "Research findings or background text for hypothesis generation.",
    },
    "planning": {
        "cls_path": ("lionagi.engines", "PlanningEngine"),
        "pos_arg": "prompt",
        "pos_help": "Goal or task description to plan and execute.",
    },
}

ENGINE_OUTCOME_BYTE_CAP = 16 * 1024
_OUTCOME_TEXT_CAP = 512
_OUTCOME_LIST_CAP = 64


def _import_engine_class(module: str, name: str) -> type:
    import importlib

    mod = importlib.import_module(module)
    return getattr(mod, name)


# ── Subparser builder ──────────────────────────────────────────────────────


def add_engine_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li engine` and its `run` sub-subcommand."""
    engine_parser = subparsers.add_parser(
        "engine",
        help="Run domain-specific multi-agent engine pipelines.",
        description=(
            "Run a lionagi engine kind from the command line.\n\n"
            "Each engine kind wraps a multi-agent pipeline specialised for a\n"
            "domain (research, code review, hypothesis generation, …).  The\n"
            "engine's progress events stream to stderr; the final result is\n"
            "emitted as JSON on stdout.\n\n"
            "Examples:\n"
            "  li engine run research 'What are the latest advances in GQA?'\n"
            "  li engine run review 'See artifact.py' --model claude/sonnet\n"
            "  li engine run coding 'Implement a BFS traversal' --test-cmd 'pytest'\n"
            "  li engine run hypothesis 'Finding: X causes Y' --export-dir ./out\n"
            "  li engine run planning 'Build a REST API'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    engine_sub = engine_parser.add_subparsers(dest="engine_command", required=True)

    kinds_str = ", ".join(sorted(_KIND_META))
    run_parser = engine_sub.add_parser(
        "run",
        help=f"Run an engine. Kinds: {kinds_str}.",
        description=(
            f"Run a lionagi engine of a specific kind.\n\n"
            f"Available kinds: {kinds_str}\n\n"
            "Progress events are written to stderr as human-readable lines.\n"
            "The final result is written as JSON to stdout.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_parser.add_argument(
        "kind",
        choices=list(_KIND_META),
        metavar="kind",
        help=f"Engine kind to run. One of: {kinds_str}.",
    )
    run_parser.add_argument(
        "spec",
        help=("Main input for the engine (topic / artifact / spec / findings / prompt)."),
    )

    # ── Coding-specific flags ──────────────────────────────────────────
    run_parser.add_argument(
        "--test-cmd",
        default=None,
        metavar="CMD",
        help=(
            "Test command to validate generated code (required for 'coding' kind). "
            "May be a shell string or a quoted list."
        ),
    )
    run_parser.add_argument(
        "--export-dir",
        default=None,
        metavar="DIR",
        help=(
            "Directory to save engine outputs to (optional; supported by 'coding' "
            "and 'hypothesis' kinds)."
        ),
    )

    # ── Hypothesis-specific flags ──────────────────────────────────────
    run_parser.add_argument(
        "--dedup-repo",
        default=None,
        metavar="OWNER/REPO",
        help=(
            "GitHub repo to check findings against before extraction "
            "('hypothesis' kind only). Findings whose mechanism an existing "
            "open or closed issue already covers are recorded and not expanded; "
            "certified-new findings land in the export's filing queue. "
            "The engine never files issues itself."
        ),
    )
    run_parser.add_argument(
        "--dedup-cwd",
        default=None,
        metavar="DIR",
        help=(
            "Checkout of the dedup repo so the novelty check can source-confirm "
            "a finding's cite ('hypothesis' kind only; optional)."
        ),
    )

    # ── Engine constructor overrides ───────────────────────────────────
    run_parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=(
            "Model to use (provider/name, e.g. claude/sonnet; an effort suffix "
            "like codex/gpt-5.6-luna-high is honored). Overrides per-stage "
            "defaults. Uses engine defaults if omitted."
        ),
    )
    run_parser.add_argument(
        "--effort",
        default=None,
        metavar="LEVEL",
        help=(
            "Reasoning effort for all stages (e.g. low, medium, high, xhigh). "
            "Overrides per-stage defaults; a per-stage default applies otherwise."
        ),
    )
    run_parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        metavar="N",
        help="Maximum recursion/expansion depth for the engine (kind-specific default).",
    )
    run_parser.add_argument(
        "--max-agents",
        type=int,
        default=None,
        metavar="N",
        help="Maximum number of sub-agents the engine may spawn.",
    )
    run_parser.add_argument(
        "--session-id",
        default=None,
        metavar="SESSION_ID",
        help=(
            "Associate this engine run with an existing session in StateDB "
            "(written to engine_runs.session_id)."
        ),
    )
    run_parser.add_argument(
        "--invocation",
        dest="invocation_id",
        default=os.getenv("LIONAGI_INVOCATION_ID") or None,
        metavar="ID",
        help=(
            "Parent Studio invocation id. Defaults to LIONAGI_INVOCATION_ID; "
            "an explicit value takes precedence."
        ),
    )
    run_parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Skip writing the engine run record to StateDB.",
    )


# ── Main dispatch ──────────────────────────────────────────────────────────


@auto_register(
    area="engine", cli=CliDeclaration(seed="engine", parser_factory=add_engine_subparser)
)
def run_engine(args: argparse.Namespace) -> int:
    """Entry point called from main() when args.command == 'engine'."""
    from lionagi.ln.concurrency import run_async

    if args.engine_command == "run":
        return run_async(_do_engine_run(args))

    log_error(f"unknown engine subcommand: {args.engine_command!r}")
    return 1


# ── Core async implementation ──────────────────────────────────────────────


async def _do_engine_run(args: argparse.Namespace) -> int:
    """Resolve args, instantiate engine, run it, persist result."""
    kind = args.kind
    spec = args.spec
    meta = _KIND_META[kind]

    if kind == "coding" and not args.test_cmd:
        log_error("the 'coding' engine requires --test-cmd (e.g. --test-cmd 'pytest tests/')")
        return 1

    engine_kwargs: dict[str, Any] = {}
    if args.model:
        engine_kwargs["model"] = args.model
    if getattr(args, "effort", None):
        engine_kwargs["effort"] = args.effort
    if args.max_depth is not None:
        engine_kwargs["max_depth"] = args.max_depth
    if args.max_agents is not None:
        engine_kwargs["max_agents"] = args.max_agents
    if kind == "hypothesis":
        if getattr(args, "dedup_repo", None):
            engine_kwargs["dedup_repo"] = args.dedup_repo
        if getattr(args, "dedup_cwd", None):
            engine_kwargs["dedup_cwd"] = args.dedup_cwd
    elif getattr(args, "dedup_repo", None) or getattr(args, "dedup_cwd", None):
        warn("--dedup-repo/--dedup-cwd only apply to the 'hypothesis' kind; ignored")

    run_kwargs: dict[str, Any] = {}
    if kind == "coding":
        run_kwargs["test_cmd"] = args.test_cmd
        if args.export_dir:
            run_kwargs["export_dir"] = args.export_dir
    elif kind == "hypothesis":
        if args.export_dir:
            run_kwargs["export_dir"] = args.export_dir

    # Spec JSON stored in DB represents the user-visible call parameters.
    spec_for_db: dict[str, Any] = {meta["pos_arg"]: spec, **run_kwargs}

    run_id = uuid.uuid4().hex
    started_at = time.time()
    invocation_id = getattr(args, "invocation_id", None)

    db = None
    # engine_runs.session_id carries the user's --session-id; signal_session_id
    # is a separate sessions row (run_id) the engine creates for Studio streaming.
    signal_session_id: str | None = None
    if not args.no_persist:
        try:
            from lionagi.state.db import StateDB

            db = StateDB()
            await db.open()
            await db.insert_engine_run(
                run_id=run_id,
                kind=kind,
                spec_json=spec_for_db,
                started_at=started_at,
                session_id=args.session_id,
            )
        except Exception as exc:  # noqa: BLE001
            warn(f"could not open StateDB for persistence: {exc}")
            db = None
    if db is not None:
        # Guarded separately: failure here only disables live signal streaming.
        try:
            # create_session is INSERT OR IGNORE — never bind to a pre-existing
            # row. See docs/internals/cli.md for the silent-reuse risk.
            if await db.get_session(run_id) is not None:
                warn(f"sessions row {run_id} already exists; skipping signal binding")
            else:
                prog_id = f"{run_id}-prog"
                await db.create_progression(prog_id)
                await db.create_session(
                    {
                        "id": run_id,
                        "created_at": started_at,
                        "started_at": started_at,
                        "progression_id": prog_id,
                        "name": f"engine:{kind}",
                        "status": "running",
                        "invocation_kind": "engine",
                        "invocation_id": invocation_id,
                    }
                )
                signal_session_id = run_id
        except Exception as exc:  # noqa: BLE001
            warn(f"could not create signal session for engine run: {exc}")
            signal_session_id = None
        lineage_writer = getattr(db, "set_engine_run_lineage", None)
        if lineage_writer is not None:
            try:
                await lineage_writer(
                    run_id,
                    invocation_id=invocation_id,
                    signal_session_id=signal_session_id,
                    parent_session_id=args.session_id,
                )
            except Exception as exc:  # noqa: BLE001
                warn(f"could not persist engine run lineage: {exc}")

    # Import engine class lazily (no circular import; heavy deps stay unloaded
    # until actually needed).
    try:
        module, cls_name = meta["cls_path"]
        engine_class = _import_engine_class(module, cls_name)
    except Exception as exc:
        log_error(f"failed to import engine class for kind {kind!r}: {exc}")
        await _maybe_update_db(
            db,
            run_id,
            "failed",
            error=str(exc),
            outcome=_engine_outcome(
                status="failed",
                kind=kind,
                started_at=started_at,
                ended_at=time.time(),
                reason_code="import_failure",
            ),
            signal_session_id=signal_session_id,
        )
        if db is not None:
            await db.close()
        return 1

    def on_event(event: dict[str, Any]) -> None:
        event_type = event.get("type", "event")
        # Format: "engine[research] phase: <msg>" or "engine[research] done"
        parts = [f"engine[{kind}] {event_type}"]
        for key, val in event.items():
            if key == "type":
                continue
            if isinstance(val, (dict, list)):
                val_str = json.dumps(val, ensure_ascii=False)
            else:
                val_str = str(val)
            parts.append(f"{key}={val_str}")
        progress("  ".join(parts))

    _session = None
    if signal_session_id is not None:
        try:
            from lionagi.session.session import Session as _Session

            _session = _Session()
            _session.observer.bind_db_persistence(signal_session_id, db=db)
        except Exception as exc:  # noqa: BLE001
            warn(f"could not bind signal persistence for engine run: {exc}")
            _session = None

    progress(f"engine[{kind}] starting  spec={spec!r}")
    result = None
    ended_at: float | None = None
    try:
        engine = engine_class(**engine_kwargs)
        # CodingEngine's .run() signature diverges from the other engines'.
        # See docs/internals/cli.md.
        result = await engine.run(spec, on_event=on_event, session=_session, **run_kwargs)
    except Exception as exc:
        log_error(f"engine[{kind}] failed: {exc}")
        ended_at = time.time()
        await _maybe_update_db(
            db,
            run_id,
            "failed",
            ended_at=ended_at,
            error=str(exc),
            outcome=_engine_outcome(
                status="failed",
                kind=kind,
                started_at=started_at,
                ended_at=ended_at,
                reason_code="exception",
            ),
            signal_session_id=signal_session_id,
        )
        if db is not None:
            await db.close()
        return 1
    except BaseException as exc:
        # CancelledError/KeyboardInterrupt bypass `except Exception` above; mark
        # cancelled before re-raising. See docs/internals/cli.md.
        ended_at = time.time()
        await _maybe_update_db(
            db,
            run_id,
            "cancelled",
            ended_at=ended_at,
            error=f"{type(exc).__name__}: {exc}",
            outcome=_engine_outcome(
                status="cancelled",
                kind=kind,
                started_at=started_at,
                ended_at=ended_at,
                reason_code="cancelled",
            ),
            signal_session_id=signal_session_id,
        )
        if db is not None:
            await db.close()
        raise

    ended_at = time.time()
    progress(f"engine[{kind}] completed  elapsed={ended_at - started_at:.1f}s")

    # Collect emission-missing diagnostics for the structured outcome. They
    # are degradation, not a terminal failure, so completed rows keep error NULL.
    emission_error: str | None = None
    _emission_failures: list[str] = getattr(engine, "_emission_failures", [])
    if _emission_failures:
        emission_error = "emission_missing: " + "; ".join(_emission_failures)

    # A degraded run reached a result without all of its work. The outcome
    # envelope carries it on a completed row; this text is for the terminal and
    # for the error column a total agent failure does write.
    _degraded: bool = bool(getattr(result, "degraded", False))
    _degrade_reason: str = str(getattr(result, "degrade_reason", "") or "")
    _skipped: list[str] = list(getattr(result, "skipped", []) or [])
    if _degraded:
        degraded_text = "degraded: " + (_degrade_reason or "reason not recorded")
        if _skipped:
            degraded_text += f" (skipped: {', '.join(_skipped)})"
        emission_error = f"{emission_error}; {degraded_text}" if emission_error else degraded_text
        warn(degraded_text)

    # Every agent terminally erroring must not report "completed" as green.
    _total_agent_failure: bool = getattr(engine, "_total_agent_failure", False)
    if _total_agent_failure:
        _agent_errors: list[str] = getattr(engine, "_agent_errors", [])
        agent_error_text = "all sub-agents failed: " + "; ".join(_agent_errors)
        emission_error = (
            f"{emission_error}; {agent_error_text}" if emission_error else agent_error_text
        )

    # Serialise result to stdout as JSON. export_dir sourcing: see docs/internals/cli.md.
    export_dir_from_args: str | None = args.export_dir if kind in ("coding", "hypothesis") else None
    export_dir_for_db: str | None = export_dir_from_args
    try:
        if hasattr(result, "model_dump"):
            # Pydantic model (e.g. CodingEngine returns CodeResultRecorded).
            result_data = result.model_dump(mode="json")
            _rd_export = result_data.get("export_dir")
            export_dir_for_db = _rd_export if _rd_export is not None else export_dir_from_args
        elif isinstance(result, str):
            # EngineResult subclasses str to stay back-compatible, so it lands
            # here and the plain-string shape would carry only the text. A
            # consumer reading the JSON could not then tell a run that did all
            # its work from one that skipped a dimension, which is the whole
            # thing the caller needs in order to decide whether to trust it.
            result_data = {"result": str(result)}
            if _degraded:
                result_data["degraded"] = True
                result_data["degrade_reason"] = _degrade_reason
                result_data["skipped"] = _skipped
        else:
            result_data = {"result": str(result)}
        print(json.dumps(result_data, ensure_ascii=False, indent=2))
    except Exception as exc:
        warn(f"could not serialise result to JSON: {exc}")
        print(repr(result))

    await _maybe_update_db(
        db,
        run_id,
        "failed" if _total_agent_failure else "completed",
        ended_at=ended_at,
        export_dir=export_dir_for_db,
        error=emission_error if _total_agent_failure else None,
        outcome=_engine_outcome(
            status="failed" if _total_agent_failure else "completed",
            kind=kind,
            started_at=started_at,
            ended_at=ended_at,
            result=result,
            engine=engine,
            export_dir=export_dir_for_db,
            emission_failures=_emission_failures,
            agent_failure=_total_agent_failure,
        ),
        signal_session_id=signal_session_id,
    )
    if db is not None:
        await db.close()
    # A run where every agent terminally errored is a failure: exit non-zero so
    # shell/CI callers see it, matching the persisted "failed" status above.
    return 1 if _total_agent_failure else 0


async def _maybe_update_db(
    db: Any,
    run_id: str,
    status: str,
    *,
    ended_at: float | None = None,
    export_dir: str | None = None,
    error: str | None = None,
    outcome: dict[str, Any] | None = None,
    signal_session_id: str | None = None,
) -> None:
    """Update the engine run row if a DB handle is open; swallow errors."""
    if db is None:
        return
    try:
        await db.update_engine_run(
            run_id,
            status=status,
            ended_at=ended_at or time.time(),
            export_dir=export_dir,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001
        warn(f"could not update engine run record in StateDB: {exc}")
    if outcome is not None:
        outcome_writer = getattr(db, "record_engine_run_outcome", None)
        if outcome_writer is not None:
            try:
                await outcome_writer(run_id, outcome)
            except Exception as exc:  # noqa: BLE001
                warn(f"could not persist engine run outcome: {exc}")
    # Mirror terminal status to the sessions row so Studio's SSE generator
    # knows the stream is done (same done-detection logic as agent/flow runs).
    if signal_session_id is not None and status in ("completed", "failed", "cancelled"):
        _session_status = "completed" if status == "completed" else status
        try:
            from lionagi.state.reasons import RunReasons

            _reason = (
                RunReasons.COMPLETED_OK
                if status == "completed"
                else RunReasons.FAILED_EXCEPTION
                if status == "failed"
                else RunReasons.CANCELLED_SYSTEM
            )
            await db.update_status(
                "session",
                signal_session_id,
                new_status=_session_status,
                reason_code=_reason,
                extra_fields={"ended_at": ended_at or time.time()},
            )
        except Exception as exc:  # noqa: BLE001
            warn(f"could not update engine session status in StateDB: {exc}")


def _engine_outcome(
    *,
    status: str,
    kind: str,
    started_at: float,
    ended_at: float,
    result: Any = None,
    engine: Any = None,
    export_dir: str | None = None,
    reason_code: str | None = None,
    emission_failures: list[str] | None = None,
    agent_failure: bool = False,
) -> dict[str, Any]:
    """Build a content-free, bounded terminal projection."""
    raw_skipped = list(emission_failures or [])
    for item in getattr(result, "skipped", []) or []:
        if item not in raw_skipped:
            raw_skipped.append(item)
    skipped = [str(item)[:_OUTCOME_TEXT_CAP] for item in raw_skipped]
    skipped = skipped[:_OUTCOME_LIST_CAP]
    degraded = bool(getattr(result, "degraded", False) or skipped)
    degrade_reason = str(getattr(result, "degrade_reason", "") or "")[:_OUTCOME_TEXT_CAP]
    if not degrade_reason and skipped:
        degrade_reason = "emission_failure"
    if agent_failure:
        reason_code = reason_code or "all_agents_failed"

    if isinstance(result, str):
        result_kind = "text"
        result_size = len(result.encode(errors="replace"))
    elif result is None:
        result_kind = "none"
        result_size = 0
    else:
        result_kind = type(result).__name__[:128]
        result_size = None

    effective_model = getattr(engine, "served_model", None) if engine is not None else None
    config_shape = {
        "kind": kind,
        "model_known": bool(effective_model),
        "max_depth": getattr(engine, "max_depth", None) if engine is not None else None,
        "max_agents": getattr(engine, "max_agents", None) if engine is not None else None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(config_shape, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    envelope: dict[str, Any] = {
        "version": 1,
        "status": status,
        "degraded": degraded,
        "degrade_reason": degrade_reason or None,
        "reason_code": reason_code,
        "skipped": skipped,
        "skipped_count": len(raw_skipped),
        "result": {"kind": result_kind, "size_bytes": result_size},
        "output": {"present": bool(export_dir), "kind": "export_dir" if export_dir else None},
        "engine": {
            "kind": kind,
            "config_fingerprint": fingerprint,
            "effective_model": effective_model,
            "effective_model_source": "reported" if effective_model else "unknown",
        },
        "timing": {
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": max(0.0, (ended_at - started_at) * 1000),
        },
    }
    encoded = json.dumps(envelope, separators=(",", ":"), default=str).encode()
    if len(encoded) > ENGINE_OUTCOME_BYTE_CAP:
        envelope["skipped"] = []
        envelope["skipped_truncated"] = True
        encoded = json.dumps(envelope, separators=(",", ":"), default=str).encode()
    if len(encoded) > ENGINE_OUTCOME_BYTE_CAP:
        raise ValueError("engine outcome envelope exceeds byte cap")
    return envelope

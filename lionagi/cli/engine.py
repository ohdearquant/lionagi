# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li engine run <kind> <spec>` — shell-reachable engine execution."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from typing import Any

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

    # ── Engine constructor overrides ───────────────────────────────────
    run_parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Model to use (provider/name, e.g. claude/sonnet). Uses default if omitted.",
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
        "--no-persist",
        action="store_true",
        help="Skip writing the engine run record to StateDB.",
    )


# ── Main dispatch ──────────────────────────────────────────────────────────


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
    if args.max_depth is not None:
        engine_kwargs["max_depth"] = args.max_depth
    if args.max_agents is not None:
        engine_kwargs["max_agents"] = args.max_agents

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

    db = None
    # session_id for signal persistence: the engine run creates its own
    # sessions row (run_id) so Studio can stream signals live.  The
    # engine_runs.session_id column still carries the user-supplied
    # --session-id for cross-linking to an existing session.
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
        # Create a sessions row so session_signals FK is satisfied.  Guarded
        # separately: a failure here only disables live signal streaming,
        # never the engine_runs record itself.
        try:
            # create_session is INSERT OR IGNORE: a pre-existing row with this
            # id would be silently reused, appending our signals to an
            # unrelated session and mirroring terminal status onto it.  run_id
            # is a fresh uuid4 so this should never happen — but never bind to
            # a row this run did not create.
            if await db.get_session(run_id) is not None:
                warn(f"sessions row {run_id} already exists; skipping signal binding")
            else:
                prog_id = f"{run_id}-prog"
                await db.create_progression(prog_id)
                await db.create_session(
                    {
                        "id": run_id,
                        "created_at": started_at,
                        "progression_id": prog_id,
                        "name": f"engine:{kind}",
                        "status": "running",
                        "invocation_kind": None,
                    }
                )
                signal_session_id = run_id
        except Exception as exc:  # noqa: BLE001
            warn(f"could not create signal session for engine run: {exc}")
            signal_session_id = None

    # Import engine class lazily (no circular import; heavy deps stay unloaded
    # until actually needed).
    try:
        module, cls_name = meta["cls_path"]
        engine_class = _import_engine_class(module, cls_name)
    except Exception as exc:
        log_error(f"failed to import engine class for kind {kind!r}: {exc}")
        await _maybe_update_db(
            db, run_id, "failed", error=str(exc), signal_session_id=signal_session_id
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
        # CodingEngine has its own .run() signature (positional spec + keyword
        # test_cmd/workspace/export_dir); other engines use Engine.run() which
        # dispatches to _run(run, <main_arg>, **run_kwargs).
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
            signal_session_id=signal_session_id,
        )
        if db is not None:
            await db.close()
        return 1
    except BaseException as exc:
        # asyncio.CancelledError and KeyboardInterrupt are BaseException paths that
        # bypass the `except Exception` handler above.  Mark the row cancelled before
        # re-raising so Studio doesn't show it as permanently 'running'.
        # run_async() in lionagi/ln/concurrency/utils.py:86 cancels the task on
        # SIGINT and then raises KeyboardInterrupt at :108; we re-raise here to
        # preserve that exit-code behaviour (interpreter default for SIGINT).
        ended_at = time.time()
        await _maybe_update_db(
            db,
            run_id,
            "cancelled",
            ended_at=ended_at,
            error=f"{type(exc).__name__}: {exc}",
            signal_session_id=signal_session_id,
        )
        if db is not None:
            await db.close()
        raise

    ended_at = time.time()
    progress(f"engine[{kind}] completed  elapsed={ended_at - started_at:.1f}s")

    # Collect emission-missing diagnostics from the engine run object so they
    # can be written to engine_runs.error even when status stays "completed".
    emission_error: str | None = None
    _emission_failures: list[str] = getattr(engine, "_emission_failures", [])
    if _emission_failures:
        emission_error = "emission_missing: " + "; ".join(_emission_failures)

    # A run where every agent made terminally errored (e.g. missing API key)
    # must not be reported "completed" — fold the agent errors into the error
    # column and mark the run failed instead of green.
    _total_agent_failure: bool = getattr(engine, "_total_agent_failure", False)
    if _total_agent_failure:
        _agent_errors: list[str] = getattr(engine, "_agent_errors", [])
        agent_error_text = "all sub-agents failed: " + "; ".join(_agent_errors)
        emission_error = (
            f"{emission_error}; {agent_error_text}" if emission_error else agent_error_text
        )

    # Serialise result to stdout as JSON.
    # export_dir: the CLI knows what directory it passed; neither CodeResultRecorded
    # (lionagi/engines/coding.py:153 — fields: passed, measurements, caveats,
    # experiment_ref, verdict_ref) nor the hypothesis string echo it back.  Source
    # export_dir from args directly for kinds that accept the flag, falling back to
    # result_data for any future engine model that does include it.
    export_dir_from_args: str | None = args.export_dir if kind in ("coding", "hypothesis") else None
    export_dir_for_db: str | None = export_dir_from_args
    try:
        if hasattr(result, "model_dump"):
            # Pydantic model (e.g. CodingEngine returns CodeResultRecorded).
            result_data = result.model_dump(mode="json")
            _rd_export = result_data.get("export_dir")
            export_dir_for_db = _rd_export if _rd_export is not None else export_dir_from_args
        elif isinstance(result, str):
            result_data = {"result": result}
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
        error=emission_error,
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
            )
        except Exception as exc:  # noqa: BLE001
            warn(f"could not update engine session status in StateDB: {exc}")

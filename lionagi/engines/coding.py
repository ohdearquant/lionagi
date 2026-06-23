# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coding engine — Gated-Loop shape: plan → implement → test → [fix] → verify → record."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import Field

from lionagi.casts.emission import Verdict
from lionagi.ln.concurrency import run_sync
from lionagi.tools._subprocess import _SHELL_CONTROL, _subprocess_sync

from .engine import ChainEvent, ChainRun, Engine, EngineEvent, EngineRun

logger = logging.getLogger("lionagi.engines")

__all__ = (
    "CodingChainEvent",
    "WorkPlanned",
    "ChangeProposed",
    "TestsRan",
    "VerifyResult",
    "CodeResultRecorded",
    "AutoRepairApplied",
    "WorkAborted",
    "WorkerHeartbeat",
    "WorkerActivity",
    "CodingRun",
    "CodingEngine",
    "_lint_spec",
)


# ---------------------------------------------------------------------------
# Events — the pipeline vocabulary. The engine stamps ``eid`` (W/P/T/V/K, no
# collision with hypothesis's F/Q/E/H/X/R/C/A); refs link a stage to its
# upstream stage so the export is a walkable chain.
# ---------------------------------------------------------------------------


CodingChainEvent = ChainEvent


class WorkPlanned(EngineEvent, CodingChainEvent):
    """The implementation plan for a coding spec — what the implementer executes."""

    approach: str = Field(description="How the change will be made, in concrete steps.")
    files_to_touch: list[str] = Field(
        default_factory=list, description="The files expected to be created or modified."
    )
    test_strategy: str = Field(
        default="", description="How the change will be proven (which tests, what they assert)."
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list, description="The conditions that mean the work is done and correct."
    )


class ChangeProposed(EngineEvent, CodingChainEvent):
    """A code change the implementer made via its tools; re-emitted once per fix round."""

    plan_ref: str = Field(default="", description="Id of the WorkPlanned this implements.")
    summary: str = Field(description="What was changed, stated concretely.")
    files_touched: list[str] = Field(
        default_factory=list, description="The files actually created or modified."
    )
    test_cmd: str = Field(
        default="",
        description="The command the implementer believes proves the change "
        "(advisory; the engine runs the spec's declared test_cmd as ground truth).",
    )


class TestsRan(EngineEvent, CodingChainEvent):
    """Ground truth: subprocess outcome of the declared test command. ``passed`` is the exit code, never a model claim."""

    # Not a pytest test class — the name starts with "Test" but it is an event.
    __test__ = False

    change_ref: str = Field(default="", description="Id of the ChangeProposed this tested.")
    cmd: str = Field(description="The exact command that was run.")
    passed: bool = Field(description="True iff the process exited 0 (and did not time out).")
    returncode: int = Field(
        default=0, description="The process exit code (-1 on timeout/spawn error)."
    )
    timed_out: bool = Field(default=False, description="True if the command exceeded the timeout.")
    round: int = Field(
        default=0, description="Fix-loop round this run belongs to (0 = first pass)."
    )
    output_tail: str = Field(
        default="",
        description="Tail of combined stdout+stderr; the full capture is written to the export dir.",
    )
    output_file: str = Field(
        default="", description="Path to the full captured output, when an export dir is set."
    )


class VerifyResult(Verdict, CodingChainEvent):
    """The critic's call on whether the change meets acceptance criteria — casts ``Verdict`` plus refs and unmet-criteria list."""

    change_ref: str = Field(default="", description="Id of the ChangeProposed being verified.")
    tests_ref: str = Field(default="", description="Id of the TestsRan that produced ground truth.")
    meets_acceptance: bool = Field(
        default=False, description="True only if every acceptance criterion is satisfied."
    )
    unmet: list[str] = Field(
        default_factory=list, description="Acceptance criteria not yet satisfied by the change."
    )


class CodeResultRecorded(EngineEvent, CodingChainEvent):
    """Terminal coding-run result shaped for hypothesis ingestion via ``CodingRun.to_hypothesis_seeds``."""

    passed: bool = Field(description="Whether the declared test command ultimately passed.")
    measurements: dict[str, Any] = Field(
        default_factory=dict, description="Structured outcome — counts, rounds, the final command."
    )
    caveats: list[str] = Field(
        default_factory=list, description="Conditions that limit what this result shows."
    )
    experiment_ref: str = Field(
        default="", description="Originating experiment eid when seeded from a hypothesis run."
    )
    verdict_ref: str = Field(default="", description="Id of the VerifyResult, if one was produced.")


_EVENT_PREFIX: dict[type, str] = {
    WorkPlanned: "W",
    ChangeProposed: "P",
    TestsRan: "T",
    VerifyResult: "V",
    CodeResultRecorded: "K",
}

_REF_ATTRS = (
    "verdict_ref",
    "tests_ref",
    "change_ref",
    "plan_ref",
)


# ---------------------------------------------------------------------------
# Worker observability events (abort + heartbeat + activity)
# ---------------------------------------------------------------------------


class WorkAborted(EngineEvent):
    """Emitted when a stage is aborted by the stage watchdog."""

    stage: str = Field(default="")
    reason: str = Field(default="")
    elapsed_s: float = Field(default=0.0)


class WorkerHeartbeat(EngineEvent):
    """Periodic liveness ping emitted while a stage is running."""

    elapsed_s: float = Field(description="Seconds since the run started.")
    stage: str = Field(default="implement")


class WorkerActivity(EngineEvent):
    """Emitted when a file in the workspace is written during a stage."""

    path: str = Field(description="Workspace-relative path that changed.")
    elapsed_s: float = Field(description="Seconds since the run started.")


class AutoRepairApplied(EngineEvent):
    """Emitted when the harness auto-applies a repair command before a test gate.

    Distinct from a worker fix round: the worker produced the change; the harness
    normalized it mechanically (e.g. ``cargo fmt --all``).  The dataset records
    both so analysis can separate worker-produced vs harness-normalized diffs.
    """

    cmd: str = Field(description="The auto-repair command that was run.")
    files: list[str] = Field(
        default_factory=list,
        description="Workspace-relative paths whose mtime changed after the command.",
    )
    round: int = Field(
        default=0, description="Fix-loop round this repair belongs to (0 = first pass)."
    )


# ---------------------------------------------------------------------------
# Spec lint
# ---------------------------------------------------------------------------

_RE_ACCEPTANCE = re.compile(r"acceptance.{0,20}criteria|acceptance:", re.IGNORECASE)
_RE_TEST_CMD = re.compile(r"test_cmd\s*:", re.IGNORECASE)
_RE_COUNT_ASSERT = re.compile(r"\d+\s*(test|case|item|assert|pass)", re.IGNORECASE)
_RE_FILE_PATH = re.compile(r"(?:^|[\s(\"'])(/[\w/.\-]+\.[\w]+)", re.MULTILINE)


def _lint_spec(
    spec: str | dict[str, Any],
    *,
    workspace: str | Path | None = None,
    strict: bool = False,
) -> list[str]:
    """Return a list of warning strings for common spec deficiencies.

    Pass *strict=True* to raise ValueError on the first warning instead.
    """
    text = spec if isinstance(spec, str) else json.dumps(spec)
    warnings: list[str] = []
    if not _RE_ACCEPTANCE.search(text):
        warnings.append("spec has no acceptance criteria — add an 'acceptance criteria:' section")
    if _RE_TEST_CMD.search(text) and not _RE_COUNT_ASSERT.search(text):
        warnings.append(
            "spec has a test_cmd but no count assertion (e.g. '3 tests pass') — hard to verify"
        )
    if workspace is not None:
        for m in _RE_FILE_PATH.finditer(text):
            candidate = Path(m.group(1))
            if candidate.is_absolute() and not candidate.exists():
                warnings.append(f"referenced path does not exist: {candidate}")
    if strict and warnings:
        raise ValueError(warnings[0])
    return warnings


# ---------------------------------------------------------------------------
# Spec normalization
# ---------------------------------------------------------------------------


def _normalize_spec(spec: str | dict[str, Any]) -> tuple[str, str]:
    """Return (task_text, experiment_ref) for a string spec or a hypothesis-exported experiment dict."""
    if isinstance(spec, str):
        text = spec.strip()
        if not text:
            raise ValueError("coding spec is empty")
        return text, ""
    if isinstance(spec, dict):
        parts = []
        if procedure := str(spec.get("procedure", "")).strip():
            parts.append(f"- procedure: {procedure}")
        if dataset := str(spec.get("dataset", "")).strip():
            parts.append(f"- dataset / fixtures: {dataset}")
        if acceptance := str(spec.get("acceptance", "")).strip():
            parts.append(f"- acceptance: {acceptance}")
        if method := str(spec.get("method", "")).strip():
            parts.append(f"- method: {method}")
        if not parts:
            raise ValueError("coding spec dict has no procedure/acceptance/dataset/method")
        text = "Implement and validate this experiment:\n" + "\n".join(parts)
        return text, str(spec.get("eid", "") or "")
    raise TypeError(f"coding spec must be str or dict, got {type(spec)!r}")


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def _plan_instruction(task_text: str, workspace: str) -> str:
    return (
        f"You are planning a coding task. Workspace: {workspace}\n\n"
        f"# Task\n{task_text}\n\n"
        "Inspect the workspace if needed, then emit a work_planned with: approach "
        "(the concrete steps to make the change), files_to_touch (the files you "
        "expect to create or modify), test_strategy (how the change is proven — "
        "which tests, what they assert), acceptance_criteria (the conditions that "
        "mean the work is done and correct). Do not write code yet — plan only."
    )


def _implement_instruction(plan: WorkPlanned, task_text: str, workspace: str) -> str:
    files = ", ".join(plan.files_to_touch) if plan.files_to_touch else "(decide as you go)"
    accept = "; ".join(plan.acceptance_criteria) if plan.acceptance_criteria else "(meet the task)"
    return (
        f"Implement the plan in the workspace ({workspace}). Use your coding tools — "
        "read files, edit them, run commands — across as many turns as you need.\n\n"
        f"# Task\n{task_text}\n\n"
        f"# Plan ({plan.eid})\n"
        f"- approach: {plan.approach}\n"
        f"- files to touch: {files}\n"
        f"- test strategy: {plan.test_strategy or '(use the task)'}\n"
        f"- acceptance: {accept}\n\n"
        "When the change is in place, emit a change_proposed with: summary (what you "
        "changed), files_touched (the files you actually created or modified), "
        f"test_cmd (the command that proves it), plan_ref='{plan.eid}'. Make the "
        "change real on disk before emitting — the engine runs the test command "
        "itself and trusts only its exit code."
    )


def _fix_instruction(t: TestsRan, plan: WorkPlanned, round_no: int, max_rounds: int) -> str:
    accept = "; ".join(plan.acceptance_criteria) if plan.acceptance_criteria else "(meet the task)"
    return (
        f"The test command failed (fix round {round_no}/{max_rounds}). Diagnose the "
        "failure from the output below, fix the code with your tools, then emit a "
        f"new change_proposed (summary, files_touched, test_cmd, plan_ref='{plan.eid}').\n\n"
        f"# Failing command\n{t.cmd}\n\n"
        f"# Output (exit {t.returncode}{', TIMED OUT' if t.timed_out else ''})\n{t.output_tail}\n\n"
        f"# Acceptance criteria\n{accept}\n\n"
        "Read the actual error before changing anything; do not retry the same edit "
        "blindly. Make the fix real on disk before emitting."
    )


def _verify_instruction(plan: WorkPlanned, change: ChangeProposed, t: TestsRan, diff: str) -> str:
    accept = (
        "\n".join(f"- {c}" for c in plan.acceptance_criteria)
        if plan.acceptance_criteria
        else "(none stated — judge against the task)"
    )
    diff_block = diff.strip() or "(no diff captured — the change may be untracked or empty)"
    return (
        "Review the implemented change against its acceptance criteria. Ground truth "
        f"is the test result, not the diff's appearance.\n\n"
        f"# Acceptance criteria\n{accept}\n\n"
        f"# Test outcome\ncommand: {t.cmd}\npassed: {t.passed} (exit {t.returncode})\n\n"
        f"# Change summary ({change.eid})\n{change.summary}\n\n"
        f"# Diff\n```diff\n{diff_block}\n```\n\n"
        "Emit a verify_result with: verdict (APPROVE | APPROVE-WITH-FIXES | "
        "REQUEST-CHANGES | REJECT), rationale, meets_acceptance (true only if every "
        "criterion is satisfied), unmet (criteria not yet satisfied), "
        f"change_ref='{change.eid}', tests_ref='{t.eid}'. A passing test with unmet "
        "acceptance is still APPROVE-WITH-FIXES at best."
    )


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


class CodingRun(ChainRun):
    """Per-run state for a CodingEngine run: event store, eid counters, workspace, test command, and diff capture."""

    _chain_event_cls = CodingChainEvent
    _event_prefix_map = _EVENT_PREFIX  # filled after class definition below

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self.workspace: str = str(Path.cwd())
        self.test_cmd: str | list[str] = ""
        self.task_text: str = ""
        self.experiment_ref: str = ""
        self.diff: str = ""
        self.export_dir: Path | None = None
        self._test_runs: int = 0
        # Pre-implement workspace snapshot: maps path → porcelain XY status.
        # None means the snapshot could not be taken (non-git or spawn failure).
        self._ws_baseline: dict[str, str] | None = {}
        # Paths newly changed/added since the baseline (populated by _run).
        self._ws_delta: list[str] = []
        self._spec_lint_warnings: list[str] = []
        self._aborted: bool = False

    # -- typed overrides (narrower signatures than the Any base) ---------------

    def collect(self, event: CodingChainEvent) -> CodingChainEvent:
        return super().collect(event)  # type: ignore[return-value]

    def find(self, eid: str) -> CodingChainEvent | None:
        return self._index.get(eid)

    def events_of(self, event_type: type) -> list[Any]:
        return self.store.get(event_type, [])

    def last(self, event_type: type) -> Any | None:
        evs = self.store.get(event_type, [])
        return evs[-1] if evs else None

    def to_hypothesis_seeds(self) -> list[dict[str, Any]]:
        """Render each CodeResultRecorded as a ResultRecorded input dict for hypothesis ingestion."""
        seeds: list[dict[str, Any]] = []
        for k in self.events_of(CodeResultRecorded):
            seeds.append(
                {
                    "experiment_ref": k.experiment_ref,
                    "measurements": json.dumps(k.measurements, default=str, sort_keys=True),
                    "passed": k.passed,
                    "caveats": list(k.caveats),
                }
            )
        return seeds

    def export(self, dir_path: str | Path, *, report: str = "") -> dict[str, str]:
        """Write results.json and report.md to *dir_path*; returns paths dict. Full test output files are written separately during the run."""
        d = Path(dir_path)
        d.mkdir(parents=True, exist_ok=True)
        events = [e for evs in self.store.values() for e in evs]
        result = self.last(CodeResultRecorded)
        payload = {
            "root": self.root,
            "workspace": self.workspace,
            "agents_made": self.agents_made,
            "passed": bool(result.passed) if result else None,
            "events": [{"type": type(e).__name__, **e.model_dump()} for e in events],
            "refs": [[e.eid, _first_ref(e)] for e in events if _first_ref(e)],
            "hypothesis_seeds": self.to_hypothesis_seeds(),
        }
        results_path = d / "results.json"
        results_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        md = _render_report(self, report)
        report_path = d / "report.md"
        report_path.write_text(md, encoding="utf-8")
        return {"results": str(results_path), "report": str(report_path)}


def _first_ref(e: CodingChainEvent) -> str:
    for attr in _REF_ATTRS:
        ref = getattr(e, attr, "")
        if ref:
            return ref
    return ""


def _render_report(run: CodingRun, report: str) -> str:
    plan = run.last(WorkPlanned)
    result = run.last(CodeResultRecorded)
    verdict = run.last(VerifyResult)
    parts = [f"# Coding run\n\n- root: {run.root}\n- workspace: {run.workspace}"]
    if result is not None:
        parts.append(f"\n- passed: {result.passed}")
        if result.experiment_ref:
            parts.append(f"\n- experiment_ref: {result.experiment_ref}")
    if plan is not None:
        parts.append(f"\n\n## Plan ({plan.eid})\n{plan.approach}")
        if plan.acceptance_criteria:
            parts.append(
                "\n\n### Acceptance\n" + "\n".join(f"- {c}" for c in plan.acceptance_criteria)
            )
    parts.append(f"\n\n## Test runs ({len(run.events_of(TestsRan))})")
    for t in run.events_of(TestsRan):
        parts.append(f"\n- round {t.round}: `{t.cmd}` -> passed={t.passed} (exit {t.returncode})")
    if verdict is not None:
        parts.append(f"\n\n## Verdict ({verdict.eid})\n{verdict.verdict} — {verdict.rationale}")
        if verdict.unmet:
            parts.append("\n\n### Unmet criteria\n" + "\n".join(f"- {u}" for u in verdict.unmet))
    if report.strip():
        parts.append(f"\n\n---\n\n{report.strip()}")
    return "".join(parts) + "\n"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CodingEngine(Engine):
    """Gated plan/implement/test/fix/verify engine (stateless config). See docs/reference/engines.md for parameter details."""

    run_context_cls: type[EngineRun] = CodingRun

    def __init__(
        self,
        *,
        plan_role: str = "analyst",
        implement_role: str = "implementer",
        verify_role: str = "critic",
        coding_tools: tuple[str, ...] = ("coding",),
        implement_permissions: str | None = "safe",
        max_fix_rounds: int = 3,
        test_timeout_s: float = 600.0,
        repair_retries: int = 1,
        turn_timeout_s: float | None = 600.0,
        strict_spec: bool = False,
        heartbeat_interval_s: float | None = 30.0,
        stage_timeout_s: float | None = None,
        # per-stage tool grants (worker only; judge/plan/verify excluded)
        worker_extra_tools: tuple[str, ...] = (),
        worker_mcp_servers: list[str] | None = None,
        worker_extra_prompt: str | None = None,
        # mechanical-repair efficiency
        auto_repair_cmds: list[str] | None = None,
        fast_test_cmd: str | list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.plan_role = plan_role
        self.implement_role = implement_role
        self.verify_role = verify_role
        self.coding_tools = tuple(coding_tools)
        self.implement_permissions = implement_permissions
        self.max_fix_rounds = max_fix_rounds
        self.test_timeout_s = test_timeout_s
        self.repair_retries = repair_retries
        self.turn_timeout_s = turn_timeout_s
        self.strict_spec = strict_spec
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stage_timeout_s = stage_timeout_s
        self.worker_extra_tools = tuple(worker_extra_tools)
        self.worker_mcp_servers = list(worker_mcp_servers) if worker_mcp_servers else None
        self.worker_extra_prompt = worker_extra_prompt
        self.auto_repair_cmds = list(auto_repair_cmds) if auto_repair_cmds else []
        self.fast_test_cmd = fast_test_cmd

    # -- lifecycle ------------------------------------------------------------

    async def run(  # type: ignore[override]
        self,
        spec: str | dict[str, Any],
        *,
        test_cmd: str | list[str],
        workspace: str | None = None,
        export_dir: str | Path | None = None,
        session: Any = None,
        on_event: Any = None,
    ) -> CodeResultRecorded:
        """Normalize *spec* exactly once before creating run state; raises ValueError/TypeError for malformed specs before session initialization."""
        task_text, experiment_ref = _normalize_spec(spec)  # raises on bad input
        return await super().run(
            spec,
            test_cmd=test_cmd,
            workspace=workspace,
            export_dir=export_dir,
            _normalized=(task_text, experiment_ref),
            session=session,
            on_event=on_event,
        )

    async def _run(
        self,
        run: CodingRun,
        spec: str | dict[str, Any],
        *,
        test_cmd: str | list[str],
        workspace: str | None = None,
        export_dir: str | Path | None = None,
        _normalized: tuple[str, str] | None = None,
    ) -> CodeResultRecorded:
        """Drive *spec* through plan -> implement -> test -> [fix loop] -> verify -> conclude; returns the terminal CodeResultRecorded."""
        if not test_cmd:
            raise ValueError("test_cmd is required — the engine needs ground truth to gate on")
        if _normalized is not None:
            task_text, experiment_ref = _normalized
        else:
            # Direct _run() call without the run() gate (e.g. tests): normalize here.
            task_text, experiment_ref = _normalize_spec(spec)
        run.task_text = task_text
        run.experiment_ref = experiment_ref
        run.test_cmd = test_cmd
        run.workspace = str(Path(workspace).expanduser()) if workspace else str(Path.cwd())
        run.root = task_text
        run.export_dir = Path(export_dir) if export_dir is not None else None
        if run.export_dir is not None:
            run.export_dir.mkdir(parents=True, exist_ok=True)

        # Collector first: stamps eids before anything reads them. The pipeline
        # is sequential (no reactive spawning), so observers only stamp + store.
        run.observe(CodingChainEvent, lambda e, _c: run.collect(e))

        lint_warnings = _lint_spec(task_text, workspace=run.workspace, strict=self.strict_spec)
        for w in lint_warnings:
            run.notify("spec_lint_warning", warning=w)
        if lint_warnings:
            run._spec_lint_warnings = lint_warnings

        plan = await self._plan(run)
        # Snapshot workspace state before the implementer runs so the post-
        # implement check computes a delta, not an absolute status read.
        run._ws_baseline = await self._capture_ws_baseline(run)
        change = await self._implement(run, plan)
        if run._aborted:
            return await self._conclude(run, plan, passed=False, caveat="stage aborted by watchdog")
        if change is None:
            # Emission is metadata; the workspace delta is ground truth.  Three
            # outcomes after _implement returns None:
            #   (a) delta non-empty  → work detected; proceed to test gate.
            #   (b) delta empty      → no work; preserve no-change verdict.
            #   (c) check failed     → unknown; fail open to test gate.
            delta, check_failed = await self._workspace_changed(run)
            run._ws_delta = delta
            if check_failed:
                # Cannot prove workspace unchanged — treat as unknown, not no-work.
                run.notify("workspace_check_failed")
                change = run.collect(
                    ChangeProposed(
                        summary="(synthesized — workspace check failed; test gate is authoritative)",
                        files_touched=[],
                        plan_ref=plan.eid,
                    )
                )
            elif not delta:
                return await self._conclude(
                    run, plan, passed=False, caveat="implementer emitted no change"
                )
            else:
                # Work detected in workspace despite emission failure.  Synthesize
                # a minimal ChangeProposed and proceed; emission failure is a warning.
                run.notify("metadata_missing", work_detected=True, files=delta)
                change = run.collect(
                    ChangeProposed(
                        summary="(synthesized from workspace — implementer emitted no structured change)",
                        files_touched=delta,
                        plan_ref=plan.eid,
                    )
                )

        tests = await self._test(run, change, round_no=0)
        change, tests = await self._fix_loop(run, plan, change, tests)
        if run._aborted:
            return await self._conclude(run, plan, passed=False, caveat="stage aborted by watchdog")
        await self._verify(run, plan, change, tests)
        return await self._conclude(run, plan, passed=tests.passed)

    # -- stages ---------------------------------------------------------------

    async def _plan(self, run: CodingRun) -> WorkPlanned:
        emits = (WorkPlanned,)
        async with run._sem:
            agent = await run.make_agent(
                self.plan_role,
                name="plan",
                model=self.model_for("plan"),
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _plan_instruction(run.task_text, run.workspace),
                arrived=lambda: bool(run.events_of(WorkPlanned)),
                emits=emits,
                retries=self.repair_retries,
            )
        plan = run.last(WorkPlanned)
        if plan is None:
            # Degrade rather than crash: a planless run still implements against
            # the raw task, so the stage's emission is best-effort.
            plan = run.collect(WorkPlanned(approach=run.task_text))
            run.notify("plan_missing", eid=plan.eid)
        return plan

    async def _implement(self, run: CodingRun, plan: WorkPlanned) -> ChangeProposed | None:
        emits = (ChangeProposed,)
        before = len(run.events_of(ChangeProposed))
        all_worker_tools = self.coding_tools + self.worker_extra_tools
        async with run._sem:
            agent = await run.make_agent(
                self.implement_role,
                name="implement",
                model=self.model_for("implement"),
                tools=all_worker_tools,
                permissions=self.implement_permissions if all_worker_tools else None,
                cwd=run.workspace,
                emits=emits,
                mcp_servers=self.worker_mcp_servers,
                extra_prompt=self.worker_extra_prompt,
            )
            wrapped = self._wrap_turn_timeout(agent, run)
            heartbeat_task = self._start_heartbeat(run, stage="implement")
            try:
                stage_coro = run.operate_with_repair(
                    wrapped,
                    _implement_instruction(plan, run.task_text, run.workspace),
                    arrived=lambda: len(run.events_of(ChangeProposed)) > before,
                    emits=emits,
                    retries=self.repair_retries,
                )
                await self._run_stage_with_watchdog(run, stage_coro, "implement")
            finally:
                if heartbeat_task is not None and not heartbeat_task.done():
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
        run._implementer = wrapped  # reused by the fix loop — same branch, more turns
        return run.last(ChangeProposed)

    async def _apply_auto_repairs(
        self, run: CodingRun, *, round_no: int
    ) -> list[AutoRepairApplied]:
        """Run each auto_repair_cmd in sequence; emit AutoRepairApplied per command.

        Returns the list of events emitted.  Errors are notified but never raise
        — a failed auto-repair command is surfaced as a notification, not a crash.
        """
        if not self.auto_repair_cmds:
            return []
        applied: list[AutoRepairApplied] = []
        ws = Path(run.workspace)
        for raw_cmd in self.auto_repair_cmds:
            # Snapshot mtimes before running so we can report changed files.
            before_mtimes: dict[str, float] = {}
            try:
                for p in ws.rglob("*"):
                    if p.is_file():
                        before_mtimes[str(p)] = p.stat().st_mtime
            except Exception:  # noqa: BLE001
                logger.debug("auto_repair mtime snapshot failed", exc_info=True)
            cmd, shell = _resolve_cmd(raw_cmd)
            result = await run_sync(_subprocess_sync, cmd, shell, 120.0, run.workspace)
            if int(result.get("returncode", -1)) != 0:
                run.notify(
                    "auto_repair_failed",
                    cmd=raw_cmd,
                    returncode=result.get("returncode"),
                    round=round_no,
                )
                continue
            changed: list[str] = []
            try:
                for p in ws.rglob("*"):
                    if p.is_file():
                        key = str(p)
                        if before_mtimes.get(key) != p.stat().st_mtime:
                            rel = str(p.relative_to(ws)) if p.is_relative_to(ws) else key
                            changed.append(rel)
            except Exception:  # noqa: BLE001
                logger.debug("auto_repair changed-file scan failed", exc_info=True)
            ev = AutoRepairApplied(cmd=raw_cmd, files=sorted(changed), round=round_no)
            await run.emit(ev)
            applied.append(ev)
        return applied

    async def _run_subprocess_gate(
        self, run: CodingRun, change: ChangeProposed, cmd_source: str | list[str], *, round_no: int
    ) -> TestsRan:
        """Run cmd_source as a subprocess and emit a TestsRan; shared by _test and _fast_test."""
        cmd, shell = _resolve_cmd(cmd_source)
        cmd_str = cmd_source if isinstance(cmd_source, str) else " ".join(cmd_source)
        result = await run_sync(_subprocess_sync, cmd, shell, self.test_timeout_s, run.workspace)
        combined = (result.get("stdout", "") + result.get("stderr", "")).rstrip()
        returncode = int(result.get("returncode", -1))
        timed_out = bool(result.get("timed_out", False))
        passed = returncode == 0 and not timed_out
        output_file = self._write_output(run, combined)
        tests = TestsRan(
            change_ref=change.eid,
            cmd=cmd_str,
            passed=passed,
            returncode=returncode,
            timed_out=timed_out,
            round=round_no,
            output_tail=_tail(combined),
            output_file=output_file,
        )
        await run.emit(tests)
        return run.last(TestsRan)

    async def _test(self, run: CodingRun, change: ChangeProposed, *, round_no: int) -> TestsRan:
        """Run the declared test command as a subprocess; passed is the process exit code, never a model claim."""
        await self._apply_auto_repairs(run, round_no=round_no)
        cmd_str = run.test_cmd if isinstance(run.test_cmd, str) else " ".join(run.test_cmd)
        run.notify("testing", change=change.eid, round=round_no, cmd=cmd_str)
        return await self._run_subprocess_gate(run, change, run.test_cmd, round_no=round_no)

    async def _fast_test(
        self, run: CodingRun, change: ChangeProposed, *, round_no: int
    ) -> TestsRan | None:
        """Run fast_test_cmd as an incremental gate for intermediate fix rounds.

        Returns None when fast_test_cmd is not configured.  Auto-repair is NOT
        run here — it fires in _test() which is always the authoritative leg.
        """
        if self.fast_test_cmd is None:
            return None
        cmd_str = (
            self.fast_test_cmd
            if isinstance(self.fast_test_cmd, str)
            else " ".join(self.fast_test_cmd)
        )
        run.notify("fast_testing", change=change.eid, round=round_no, cmd=cmd_str)
        return await self._run_subprocess_gate(run, change, self.fast_test_cmd, round_no=round_no)

    async def _fix_loop(
        self, run: CodingRun, plan: WorkPlanned, change: ChangeProposed, tests: TestsRan
    ) -> tuple[ChangeProposed, TestsRan]:
        """Re-prompt the implementer on failure and re-test, bounded by max_fix_rounds.

        Mechanical rounds (failure attributable solely to fmt/lint output, satisfied
        after auto-repair) skip the judge gate.  Substantive rounds pass through the
        judge as before.  When fast_test_cmd is configured, intermediate rounds gate
        on it first; the full test_cmd is always the final ground-truth leg.
        """
        agent = getattr(run, "_implementer", None)
        round_no = 0
        while not tests.passed and round_no < self.max_fix_rounds and agent is not None:
            round_no += 1
            # Classify: mechanical if auto-repair commands are configured and the
            # previous failure looks like a pure fmt/lint failure.
            is_mechanical = bool(self.auto_repair_cmds) and _looks_mechanical(tests)
            if is_mechanical:
                run.notify("fix_mechanical", round=round_no)
            else:
                subject = (
                    f"fix round {round_no}: test `{tests.cmd}` failed (exit {tests.returncode})"
                )
                if not await self.judge(run, f"fix-{round_no}", subject):
                    run.notify("fix_gated", round=round_no)
                    break
            before = len(run.events_of(ChangeProposed))
            if is_mechanical:
                # Mechanical rounds skip re-prompting the worker: auto_repair_cmds
                # already corrected the workspace.  Re-test directly.
                pass
            else:
                async with run._sem:
                    stage_coro = run.operate_with_repair(
                        agent,
                        _fix_instruction(tests, plan, round_no, self.max_fix_rounds),
                        arrived=lambda n=before: len(run.events_of(ChangeProposed)) > n,
                        emits=(ChangeProposed,),
                        retries=self.repair_retries,
                    )
                    await self._run_stage_with_watchdog(run, stage_coro, f"fix-{round_no}")
                if run._aborted:
                    break
                new_change = run.last(ChangeProposed)
                if new_change is change:
                    run.notify("fix_no_change", round=round_no)
                    break
                change = new_change
            # Incremental gate: fast_test_cmd for intermediate substantive rounds;
            # always run the full test_cmd as the final ground-truth leg.
            if (
                not is_mechanical
                and self.fast_test_cmd is not None
                and round_no < self.max_fix_rounds
            ):
                fast = await self._fast_test(run, change, round_no=round_no)
                if fast is not None and not fast.passed:
                    # Fast gate failed — update tests for the fix instruction but
                    # do not count this as the authoritative result yet.
                    tests = fast
                    continue
            tests = await self._test(run, change, round_no=round_no)
        if not tests.passed:
            run.notify("fix_exhausted", rounds=round_no, passed=False)
        return change, tests

    async def _verify(
        self, run: CodingRun, plan: WorkPlanned, change: ChangeProposed, tests: TestsRan
    ) -> VerifyResult | None:
        run.diff = await self._capture_diff(run)
        emits = (VerifyResult,)
        async with run._sem:
            # exempt: the verdict must report even when the expansion budget
            # (fix-round agents) is spent — degrade, don't lose the verdict.
            agent = await run.make_agent(
                self.verify_role,
                name="verify",
                model=self.model_for("verify"),
                emits=emits,
                exempt=True,
            )
            await run.operate_with_repair(
                agent,
                _verify_instruction(plan, change, tests, run.diff),
                arrived=lambda: bool(run.events_of(VerifyResult)),
                emits=emits,
                retries=self.repair_retries,
            )
        return run.last(VerifyResult)

    async def _conclude(
        self,
        run: CodingRun,
        plan: WorkPlanned,
        *,
        passed: bool,
        caveat: str = "",
    ) -> CodeResultRecorded:
        """Emit the terminal CodeResultRecorded shaped for hypothesis ingestion."""
        tests = run.events_of(TestsRan)
        last_test = tests[-1] if tests else None
        verdict = run.last(VerifyResult)
        caveats: list[str] = []
        if caveat:
            caveats.append(caveat)
        if last_test is not None and last_test.timed_out:
            caveats.append(f"test command timed out after {self.test_timeout_s}s")
        if verdict is not None and verdict.unmet:
            caveats.extend(f"unmet: {u}" for u in verdict.unmet)
        measurements = {
            "passed": passed,
            "fix_rounds": last_test.round if last_test is not None else 0,
            "test_cmd": last_test.cmd if last_test is not None else "",
            "returncode": last_test.returncode if last_test is not None else None,
            "test_runs": len(tests),
            "files_touched": sorted(
                {f for c in run.events_of(ChangeProposed) for f in c.files_touched}
            ),
        }
        if run._spec_lint_warnings:
            measurements["spec_lint_warnings"] = run._spec_lint_warnings
        # Record active tool grants so dataset consumers can correlate grant
        # configuration with outcome — worker grants only, judge context excluded.
        if self.worker_extra_tools or self.worker_mcp_servers:
            measurements["worker_grants"] = {
                "extra_tools": list(self.worker_extra_tools),
                "mcp_servers": self.worker_mcp_servers or [],
            }
        auto_repairs = run.by_type(AutoRepairApplied)
        if auto_repairs:
            measurements["auto_repair_rounds"] = len(auto_repairs)
        result = CodeResultRecorded(
            passed=passed,
            measurements=measurements,
            caveats=caveats,
            experiment_ref=run.experiment_ref,
            verdict_ref=verdict.eid if verdict is not None else "",
        )
        await run.emit(result)
        result = run.last(CodeResultRecorded)
        run.notify("concluded", passed=passed, experiment_ref=run.experiment_ref)
        if run.export_dir is not None:
            report = verdict.rationale if verdict is not None else ""
            paths = run.export(run.export_dir, report=report)
            run.notify("exported", **paths)
        return result

    # -- worker helpers -------------------------------------------------------

    def _wrap_turn_timeout(self, agent: Any, run: CodingRun) -> Any:
        """Return a proxy whose operate() is bounded by turn_timeout_s.

        On TimeoutError the proxy emits a turn_timeout notification and returns
        None so operate_with_repair sees arrived()=False and enters the fix path.
        """
        if self.turn_timeout_s is None:
            return agent
        timeout_s = self.turn_timeout_s

        class _Proxy:
            name = getattr(agent, "name", "")
            chat_model = getattr(agent, "chat_model", None)
            capabilities = getattr(agent, "capabilities", None)

            async def operate(self_, *, instruction: str, **kw: Any) -> Any:
                try:
                    return await asyncio.wait_for(
                        agent.operate(instruction=instruction, **kw),
                        timeout=timeout_s,
                    )
                except asyncio.TimeoutError:
                    run.notify("turn_timeout", stage=self_.name, timeout_s=timeout_s)
                    return None  # operate_with_repair sees arrived()=False → fix path

        return _Proxy()

    def _start_heartbeat(self, run: CodingRun, *, stage: str) -> asyncio.Task | None:
        """Start a background task that emits WorkerHeartbeat at the configured interval.

        Also emits WorkerActivity whenever a file in the workspace changes mtime.
        Returns None when heartbeat_interval_s is None (disabled).
        """
        if self.heartbeat_interval_s is None:
            return None
        t0 = run._t0
        ws = Path(run.workspace)
        # Snapshot existing files so only NEW writes trigger WorkerActivity.
        last_mtime: dict[str, float] = {}
        try:
            for p in ws.rglob("*"):
                if p.is_file():
                    last_mtime[str(p)] = p.stat().st_mtime
        except Exception:
            logger.debug("heartbeat baseline snapshot failed", exc_info=True)

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self.heartbeat_interval_s)
                elapsed = round(monotonic() - t0, 1)
                run.notify("WorkerHeartbeat", elapsed_s=elapsed, stage=stage)
                try:
                    for p in ws.rglob("*"):
                        if p.is_file():
                            key = str(p)
                            mt = p.stat().st_mtime
                            prev = last_mtime.get(key)
                            if prev != mt:
                                rel = str(p.relative_to(ws)) if p.is_relative_to(ws) else key
                                run.notify("WorkerActivity", path=rel, elapsed_s=elapsed)
                                last_mtime[key] = mt
                except Exception:
                    logger.debug("heartbeat mtime poll failed", exc_info=True)

        return asyncio.ensure_future(_loop())

    async def _run_stage_with_watchdog(
        self, run: CodingRun, stage_coro: Any, stage_name: str
    ) -> None:
        """Run *stage_coro* bounded by stage_timeout_s; on timeout emit WorkAborted and set run._aborted."""
        if self.stage_timeout_s is None:
            await stage_coro
            return
        t0 = monotonic()
        try:
            await asyncio.wait_for(stage_coro, timeout=self.stage_timeout_s)
        except asyncio.TimeoutError:
            elapsed = round(monotonic() - t0, 1)
            reason = f"stage '{stage_name}' exceeded {self.stage_timeout_s}s wall-clock limit"
            run.notify("WorkAborted", stage=stage_name, reason=reason, elapsed_s=elapsed)
            run._aborted = True

    async def _partial_export(
        self, run: CodingRun, *args: Any, **kwargs: Any
    ) -> CodeResultRecorded:  # type: ignore[override]
        """Write results.json and report.md even when the run is aborted mid-stage."""
        plan = run.last(WorkPlanned) or WorkPlanned(approach="(aborted)")
        return await self._conclude(run, plan, passed=False, caveat="run aborted")

    # -- ground-truth helpers -------------------------------------------------

    async def _capture_ws_baseline(self, run: CodingRun) -> dict[str, str] | None:
        """Snapshot git status --porcelain before the implement stage; returns {path: xy} or None on failure (unknown state)."""
        result = await run_sync(
            _subprocess_sync,
            ["git", "status", "--porcelain"],
            False,
            30.0,
            run.workspace,
        )
        if int(result.get("returncode", -1)) != 0:
            return None
        return _parse_porcelain(result.get("stdout", ""))

    async def _workspace_changed(self, run: CodingRun) -> tuple[list[str], bool]:
        """Return (delta_paths, check_failed) comparing current git status to the pre-implement baseline; check_failed=True when either capture failed."""
        if run._ws_baseline is None:
            # Baseline capture failed before _implement — state is unknown.
            # Do not run the post-status call; return check_failed immediately.
            return [], True
        result = await run_sync(
            _subprocess_sync,
            ["git", "status", "--porcelain"],
            False,
            30.0,
            run.workspace,
        )
        if int(result.get("returncode", -1)) != 0:
            return [], True
        after = _parse_porcelain(result.get("stdout", ""))
        delta = [
            path
            for path, xy in after.items()
            if path not in run._ws_baseline or run._ws_baseline[path] != xy
        ]
        return sorted(delta), False

    async def _capture_diff(self, run: CodingRun) -> str:
        """Combine git diff (tracked) with --no-index diffs for untracked candidates; covers emission-failure writes and fix-round additions."""
        result = await run_sync(_subprocess_sync, ["git", "diff"], False, 30.0, run.workspace)
        tracked = result.get("stdout", "") if int(result.get("returncode", -1)) == 0 else ""

        # Candidate set: union of all files any ChangeProposed claimed to touch
        # plus the initial workspace delta (covers emission-failure rewrites).
        # This is evaluated at verify time so fix-round additions are included.
        #
        # Normalize to workspace-relative POSIX before intersecting: the coding
        # tool schema asks for absolute file_path values, so files_touched often
        # carries absolute paths while git ls-files --others returns repo-relative
        # ones.  Absolute paths under workspace are stripped to relative; relative
        # paths are normalized (resolve ./.. components); absolute paths that
        # escape the workspace are dropped — they cannot be untracked files here.
        raw_candidates: set[str] = set(run._ws_delta)
        final_change = run.last(ChangeProposed)
        if final_change is not None:
            raw_candidates.update(final_change.files_touched)
        ws = Path(run.workspace)
        candidate_paths: set[str] = set()
        for p in raw_candidates:
            try:
                rel = Path(p)
                if rel.is_absolute():
                    rel = rel.relative_to(ws)
                else:
                    rel = Path(os.path.normpath(ws / rel)).relative_to(ws)
                candidate_paths.add(rel.as_posix())
            except ValueError:
                pass  # absolute path outside workspace — drop

        # Intersect with currently-untracked files to avoid double-counting
        # paths that were later staged or committed during the run.
        untracked_result = await run_sync(
            _subprocess_sync,
            ["git", "ls-files", "--others", "--exclude-standard"],
            False,
            30.0,
            run.workspace,
        )
        untracked_set: set[str] = set()
        if int(untracked_result.get("returncode", -1)) == 0:
            untracked_set = set(untracked_result.get("stdout", "").splitlines())

        untracked_candidates = sorted(candidate_paths & untracked_set)
        parts = [tracked] if tracked else []
        for rel_path in untracked_candidates:
            abs_path = str(Path(run.workspace) / rel_path)
            r = await run_sync(
                _subprocess_sync,
                ["git", "diff", "--no-index", "--", "/dev/null", abs_path],
                False,
                30.0,
                run.workspace,
            )
            # --no-index exits 1 when files differ (always true here); that is
            # the normal success case, not an error.
            content = r.get("stdout", "")
            if content:
                parts.append(content)
        return "\n".join(parts)

    def _write_output(self, run: CodingRun, output: str) -> str:
        """Write full captured test output to the export dir and return the path; the event carries only a tail."""
        if run.export_dir is None:
            return ""
        run._test_runs += 1
        path = run.export_dir / f"test_output_{run._test_runs}.txt"
        path.write_text(output, encoding="utf-8")
        return str(path)


# Patterns that suggest a test failure is purely mechanical (fmt/lint).
_RE_MECHANICAL = re.compile(
    r"(?:rustfmt|cargo fmt|black|ruff format|clang-format|gofmt|prettier|"
    r"isort|yapf|autopep8|scalafmt|ktlint)\b.*(?:would reformat|check|error|diff)",
    re.IGNORECASE,
)


def _looks_mechanical(tests: TestsRan) -> bool:
    """Return True when the test failure output looks like a pure fmt/lint failure.

    Heuristic only — used to skip the judge gate for mechanical rounds; the
    authoritative result is always the full test_cmd exit code.
    """
    if tests.passed:
        return False
    tail = tests.output_tail
    if not tail:
        return False
    lines = tail.splitlines()
    # If every non-empty line matches a formatter/linter pattern, call it mechanical.
    non_empty = [ln for ln in lines if ln.strip()]
    if not non_empty:
        return False
    return all(_RE_MECHANICAL.search(ln) for ln in non_empty)


def _parse_porcelain(output: str) -> dict[str, str]:
    """Parse git status --porcelain into {path: xy}; for renames, records the destination path."""
    mapping: dict[str, str] = {}
    for line in output.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        rest = line[3:]
        # Rename lines: "old -> new" — track the destination path.
        path = rest.split(" -> ", 1)[-1].strip()
        if path:
            mapping[path] = xy
    return mapping


def _resolve_cmd(test_cmd: str | list[str]) -> tuple[str | list[str], bool]:
    """Return (cmd, shell): lists run shell=False; strings with shell-control characters run in a shell; plain strings are shlex-split."""
    if isinstance(test_cmd, (list, tuple)):
        return list(test_cmd), False
    if _SHELL_CONTROL.search(test_cmd):
        return test_cmd, True
    return shlex.split(test_cmd), False


def _tail(text: str, *, lines: int = 40, max_chars: int = 4000) -> str:
    """Last *lines* lines of *text*, bounded to *max_chars* — for event display."""
    tail = "\n".join(text.splitlines()[-lines:])
    if len(tail) > max_chars:
        tail = "…" + tail[-max_chars:]
    return tail

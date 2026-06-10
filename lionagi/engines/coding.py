# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coding engine — the gated implement/test/fix-loop shape.

A coding spec (a task description, or a pending experiment exported by a
hypothesis run) is driven through a deterministic pipeline whose ground truth
is a real test command, not a model's claim:

```text
WorkPlanned -> ChangeProposed -> TestsRan -> [fix loop] -> VerifyResult
  -> CodeResultRecorded
```

- ``plan`` (analyst, no tools): the spec becomes a ``WorkPlanned`` — files to
  touch, approach, test strategy, acceptance criteria.
- ``implement`` (implementer WITH coding tools, ``permissions="safe"``, path-
  guarded to the run workspace): executes the plan across its own tool turns
  and emits a ``ChangeProposed``.
- ``test`` (the engine itself, NOT an agent): runs the declared ``test_cmd`` in
  a subprocess with a timeout and captured output, emitting ``TestsRan``. This
  is ground truth — never an LLM claim.
- the fix loop: on failure the implementer is re-prompted with the captured
  output for a new ``ChangeProposed``, then re-tested — the test-failure
  analogue of emission repair, bounded by ``max_fix_rounds``.
- ``verify`` (critic, read-only): reviews the captured ``git diff`` against the
  acceptance criteria and emits a ``VerifyResult`` (the casts ``Verdict``).
- ``conclude``: the engine emits a ``CodeResultRecorded`` shaped so a hypothesis
  run can ingest it (:meth:`CodingRun.to_hypothesis_seeds`).

This is the *Gated-Loop* shape, complementing research's Tree, review's
Dimensional, and hypothesis's Chain. The bus-interop bridge here is by export +
λ ferry (:meth:`CodingRun.to_hypothesis_seeds`); standing-bus consumption of
``ExperimentDesigned`` events is a future seam, not this engine.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from lionagi.casts.emission import Verdict
from lionagi.ln.concurrency import run_sync
from lionagi.tools._subprocess import _SHELL_CONTROL, _subprocess_sync

from .engine import Engine, EngineEvent, EngineRun, _event_dict, _safe_event_dict

logger = logging.getLogger("lionagi.engines")

__all__ = (
    "CodingChainEvent",
    "WorkPlanned",
    "ChangeProposed",
    "TestsRan",
    "VerifyResult",
    "CodeResultRecorded",
    "CodingRun",
    "CodingEngine",
)


# ---------------------------------------------------------------------------
# Events — the pipeline vocabulary. The engine stamps ``eid`` (W/P/T/V/K, no
# collision with hypothesis's F/Q/E/H/X/R/C/A); refs link a stage to its
# upstream stage so the export is a walkable chain.
# ---------------------------------------------------------------------------


class CodingChainEvent(BaseModel):
    """Mixin: engine-assigned chain id for the coding pipeline's audit trail."""

    eid: str = Field(default="", description="Leave empty — the engine assigns this id.")


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
    """A code change the implementer made via its tools — the unit the test
    command then judges. Re-emitted once per fix round."""

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
    """Ground truth: the outcome of the engine running the declared test command
    in a subprocess. ``passed`` is the process exit code, never a model claim."""

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
    """The critic's call on whether the change meets the acceptance criteria —
    the casts ``Verdict`` (verdict, rationale, evidence, reversible_by) plus the
    refs and the unmet-criteria list."""

    change_ref: str = Field(default="", description="Id of the ChangeProposed being verified.")
    tests_ref: str = Field(default="", description="Id of the TestsRan that produced ground truth.")
    meets_acceptance: bool = Field(
        default=False, description="True only if every acceptance criterion is satisfied."
    )
    unmet: list[str] = Field(
        default_factory=list, description="Acceptance criteria not yet satisfied by the change."
    )


class CodeResultRecorded(EngineEvent, CodingChainEvent):
    """The terminal result of the coding run, shaped for hypothesis ingestion.

    ``measurements`` is a dict so a coding run can carry structured outcomes
    (rounds, files touched, returncode); :meth:`CodingRun.to_hypothesis_seeds`
    renders it to the string ``ResultRecorded`` expects. ``experiment_ref``
    carries the originating ``ExperimentDesigned`` eid when the spec came from a
    pending experiment, '' otherwise."""

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
# Spec normalization
# ---------------------------------------------------------------------------


def _normalize_spec(spec: str | dict[str, Any]) -> tuple[str, str]:
    """Return ``(task_text, experiment_ref)`` for a coding spec.

    A string is the task description. A dict is a pending experiment exported by
    a hypothesis run (``method``/``dataset``/``procedure``/``acceptance``/``eid``)
    — its fields are rendered into the task text and its ``eid`` becomes the
    ``experiment_ref`` the result carries back."""
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


class CodingRun(EngineRun):
    """Per-run state: typed event store, eid counters, the workspace + declared
    test command, and the captured diff."""

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self.store: dict[type, list[Any]] = {t: [] for t in _EVENT_PREFIX}
        self.workspace: str = str(Path.cwd())
        self.test_cmd: str | list[str] = ""
        self.task_text: str = ""
        self.experiment_ref: str = ""
        self.diff: str = ""
        self.export_dir: Path | None = None
        self._eid_counts: dict[str, int] = {}
        self._index: dict[str, CodingChainEvent] = {}
        self._test_runs: int = 0
        # Pre-implement workspace snapshot: maps path → porcelain XY status.
        # None means the snapshot could not be taken (non-git or spawn failure).
        self._ws_baseline: dict[str, str] | None = {}
        # Paths newly changed/added since the baseline (populated by _run).
        self._ws_delta: list[str] = []

    def collect(self, event: CodingChainEvent) -> CodingChainEvent:
        """Stamp the engine-assigned eid, store the event, and fan it to on_event.

        Every chain event — whether it arrives via run.emit() or directly from
        an agent on the session bus — is stamped here.  Notifying here (rather
        than relying on EngineRun.emit's trailing notify) is the only path that
        reaches all three agent-emitted kinds: WorkPlanned, ChangeProposed, and
        VerifyResult.  emit() is overridden below to skip its own notify call for
        CodingChainEvent so there is no double-delivery for the two events that
        also pass through run.emit() (TestsRan, CodeResultRecorded)."""
        prefix = _EVENT_PREFIX.get(type(event), "N")
        n = self._eid_counts.get(prefix, 0) + 1
        self._eid_counts[prefix] = n
        event.eid = f"{prefix}-{n}"
        self.store.setdefault(type(event), []).append(event)
        self._index[event.eid] = event
        self.notify(type(event).__name__, **_safe_event_dict(event))
        return event

    async def emit(self, event: Any) -> list[Any]:
        """Emit onto the session bus; skip the base notify for chain events.

        CodingChainEvent instances are already notified by collect() (triggered
        by the observer registered in _run).  The base EngineRun.emit() would
        call notify() a second time — this override suppresses that duplicate."""
        results = await self.session.emit(event)
        if not isinstance(event, CodingChainEvent):
            self.notify(type(event).__name__, **_event_dict(event))
        return results

    def find(self, eid: str) -> CodingChainEvent | None:
        return self._index.get(eid)

    def events_of(self, event_type: type) -> list[Any]:
        return self.store.get(event_type, [])

    def last(self, event_type: type) -> Any | None:
        evs = self.store.get(event_type, [])
        return evs[-1] if evs else None

    def to_hypothesis_seeds(self) -> list[dict[str, Any]]:
        """Render the run's results as ``ResultRecorded`` inputs for a hypothesis
        run — the bus-interop bridge. Each ``CodeResultRecorded`` becomes a dict
        with ``experiment_ref`` (the originating experiment, '' if none),
        ``measurements`` (the structured dict rendered to a string), ``passed``,
        and ``caveats`` — exactly the fields ``HypothesisRun`` ingests."""
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
        """Write the run's record to *dir_path*: ``results.json`` (typed events +
        refs + hypothesis seeds) and ``report.md`` (the verify rationale +
        outcome). Full test outputs are written separately as ``test_output_N.txt``
        during the run."""
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
    """Gated implement/test/fix-loop engine (stateless config).

    Parameters extend :class:`Engine` with one casts role per agent stage —
    ``plan_role`` (plans the change, no tools), ``implement_role`` (implements
    it WITH coding tools, path-guarded), ``verify_role`` (reviews the diff
    against acceptance) — plus:

    coding_tools
        Tool names granted to the implementer (default ``("coding",)`` — the
        full reader/editor/bash/search toolkit). Tool-bearing agents get the
        destructive-command guard and workspace path guards from
        :meth:`EngineRun.make_agent` (``secure=True``).
    implement_permissions
        Permission preset for the implementer (default ``"safe"``).
    max_fix_rounds
        How many times a failed test re-prompts the implementer before the run
        concludes with ``passed=False`` — the test-failure analogue of emission
        repair.
    test_timeout_s
        Wall-clock cap on each test-command subprocess (default 600s). The
        process group is killed on timeout; output is captured up to a hard
        byte cap and the full capture is written to the export dir.
    repair_retries
        Re-prompt turns when a stage's expected emission did not arrive — the
        loop that keeps small/weak workers in the pipeline (ADR-0077 §3).

    Set ``judge_model`` (base class) to gate the fix loop on a quality judge:
    before each extra round it asks whether another attempt is worth the budget.
    Per-stage models route through ``models={"plan": ..., "implement": ...,
    "verify": ...}``.
    """

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

    # -- lifecycle ------------------------------------------------------------

    async def _run(
        self,
        run: CodingRun,
        spec: str | dict[str, Any],
        *,
        test_cmd: str | list[str],
        workspace: str | None = None,
        export_dir: str | Path | None = None,
    ) -> CodeResultRecorded:
        """Drive *spec* through plan -> implement -> test -> [fix] -> verify ->
        conclude. Returns the terminal :class:`CodeResultRecorded`.

        *test_cmd* is the ground-truth command (a list runs ``shell=False``; a
        string with shell-control characters runs in a shell, otherwise it is
        split). *workspace* is the implementer's cwd and path-guard root. When
        *export_dir* is given, ``results.json`` + ``report.md`` (and the full
        per-round test outputs) are written there."""
        if not test_cmd:
            raise ValueError("test_cmd is required — the engine needs ground truth to gate on")
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

        plan = await self._plan(run)
        # Snapshot workspace state before the implementer runs so the post-
        # implement check computes a delta, not an absolute status read.
        run._ws_baseline = await self._capture_ws_baseline(run)
        change = await self._implement(run, plan)
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
        async with run._sem:
            agent = await run.make_agent(
                self.implement_role,
                name="implement",
                model=self.model_for("implement"),
                tools=self.coding_tools,
                permissions=self.implement_permissions if self.coding_tools else None,
                cwd=run.workspace,
                emits=emits,
            )
            await run.operate_with_repair(
                agent,
                _implement_instruction(plan, run.task_text, run.workspace),
                arrived=lambda: len(run.events_of(ChangeProposed)) > before,
                emits=emits,
                retries=self.repair_retries,
            )
        run._implementer = agent  # reused by the fix loop — same branch, more turns
        return run.last(ChangeProposed)

    async def _test(self, run: CodingRun, change: ChangeProposed, *, round_no: int) -> TestsRan:
        """Run the declared test command as a subprocess — the ground-truth
        stage. NOT an agent: ``passed`` is the process exit code."""
        cmd, shell = _resolve_cmd(run.test_cmd)
        cmd_str = run.test_cmd if isinstance(run.test_cmd, str) else " ".join(run.test_cmd)
        run.notify("testing", change=change.eid, round=round_no, cmd=cmd_str)
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

    async def _fix_loop(
        self, run: CodingRun, plan: WorkPlanned, change: ChangeProposed, tests: TestsRan
    ) -> tuple[ChangeProposed, TestsRan]:
        """Re-prompt the implementer with the captured failure for a new change,
        then re-test — bounded by ``max_fix_rounds``. The judge gate (if armed)
        decides whether another round is worth the budget before each attempt."""
        agent = getattr(run, "_implementer", None)
        round_no = 0
        while not tests.passed and round_no < self.max_fix_rounds and agent is not None:
            round_no += 1
            subject = f"fix round {round_no}: test `{tests.cmd}` failed (exit {tests.returncode})"
            if not await self.judge(run, f"fix-{round_no}", subject):
                run.notify("fix_gated", round=round_no)
                break
            before = len(run.events_of(ChangeProposed))
            async with run._sem:
                await run.operate_with_repair(
                    agent,
                    _fix_instruction(tests, plan, round_no, self.max_fix_rounds),
                    arrived=lambda n=before: len(run.events_of(ChangeProposed)) > n,
                    emits=(ChangeProposed,),
                    retries=self.repair_retries,
                )
            new_change = run.last(ChangeProposed)
            if new_change is change:
                run.notify("fix_no_change", round=round_no)
                break
            change = new_change
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
        """Emit the terminal CodeResultRecorded — shaped for hypothesis ingestion."""
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

    # -- ground-truth helpers -------------------------------------------------

    async def _capture_ws_baseline(self, run: CodingRun) -> dict[str, str] | None:
        """Snapshot ``git status --porcelain`` before the implement stage.

        Returns a ``{path: xy_status}`` dict so the post-implement call can
        compute a true delta.  Returns ``None`` when the workspace is not a git
        repo or the subprocess fails — callers treat ``None`` as unknown state."""
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
        """Return ``(delta_paths, check_failed)`` after the implement stage.

        *delta_paths* is the list of paths whose porcelain status is new or
        changed relative to the pre-implement baseline captured in
        ``run._ws_baseline``.  A path counts as changed when it appears in the
        post-implement output and either (a) was absent from the baseline or
        (b) its XY status string differs from the baseline value.

        *check_failed* is ``True`` when the workspace state is unknown — either
        the pre-implement baseline capture failed (``run._ws_baseline is None``)
        or the post-implement status call fails.  The no-change verdict requires
        both captures to succeed and the delta to be empty; anything unknown
        fails open to the test gate."""
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
        """Capture the diff for the verify stage.

        Combines ``git diff`` (tracked changes) with per-file
        ``git diff --no-index /dev/null <file>`` output for any untracked paths
        that appear in either the final ``ChangeProposed.files_touched`` or
        ``run._ws_delta``.  Candidates are computed fresh at capture time by
        intersecting that union with ``git ls-files --others`` so the verify
        diff is complete for: (a) initial emission-failure writes, (b) files
        created during fix rounds, and (c) emission-ok runs that include
        untracked files.  No index mutation — ``--no-index`` reads directly."""
        result = await run_sync(_subprocess_sync, ["git", "diff"], False, 30.0, run.workspace)
        tracked = result.get("stdout", "") if int(result.get("returncode", -1)) == 0 else ""

        # Candidate set: union of all files any ChangeProposed claimed to touch
        # plus the initial workspace delta (covers emission-failure rewrites).
        # This is evaluated at verify time so fix-round additions are included.
        candidate_paths: set[str] = set(run._ws_delta)
        final_change = run.last(ChangeProposed)
        if final_change is not None:
            candidate_paths.update(final_change.files_touched)

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
        """Write the full captured test output to the export dir, returning the
        path. Info preservation: the event carries only a tail, the file carries
        everything the runner captured (which is itself byte-capped + annotated
        by the subprocess runner, never silently dropped)."""
        if run.export_dir is None:
            return ""
        run._test_runs += 1
        path = run.export_dir / f"test_output_{run._test_runs}.txt"
        path.write_text(output, encoding="utf-8")
        return str(path)


def _parse_porcelain(output: str) -> dict[str, str]:
    """Parse ``git status --porcelain`` output into a ``{path: xy}`` map.

    Each line is ``XY path`` or ``XY old -> new`` (rename).  We record the
    rightmost path token (the destination for renames) mapped to its two-
    character XY status.  Used to compute a pre/post delta without relying on
    absolute porcelain counts."""
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
    """Resolve a test command into ``(cmd, shell)`` for the subprocess runner.

    A list runs ``shell=False`` verbatim. A string with shell-control characters
    (pipes, redirects, ``&&``, …) runs in a shell because the caller asked for a
    pipeline; an otherwise-plain string is ``shlex``-split and runs
    ``shell=False`` — the safe default."""
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

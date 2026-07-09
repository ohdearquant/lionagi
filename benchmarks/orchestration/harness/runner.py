"""Runner — execute one OrchestrationConfig on one Task, once.

Maps a (config, task) pair to a RunResult by driving lionagi:
  - ``single``  : one auditor agent does the whole review.
  - ``fanout``  : parallel role workers + optional synthesis.
  - ``flow``    : orchestrator plans a DAG, executes (reactive optional).

The review PROMPT is identical across configs — only the orchestration around
it changes. That isolates "did coordinating agents help?" from prompt effects.
"""

from __future__ import annotations

import logging
import os
import pickle
import shlex
import sys
import time
from pathlib import Path

from lionagi.agent import AgentSpec
from lionagi.casts.emission import SpawnRequest
from lionagi.cli._providers import build_imodel_from_spec
from lionagi.orchestration import (
    build_dag_graph,
    fanout,
    plan,
    role_node_builder,
    spawn_roles,
)
from lionagi.session.branch import Branch
from lionagi.session.session import Session

from .config import OrchestrationConfig
from .cost import collect_usage
from .task import RunResult, Task

logger = logging.getLogger("orchbench.runner")

# The pre-seam in-process path has no ceiling at all (a reactive "flow" trial
# can run many turns across several spawned agents), so the seam's own cap
# must stay generous — this bounds a runaway subprocess, not typical trials.
_CELL_TIMEOUT_S = 1800

# Allowed model set → canonical CLI specs (the bare `claude` alias is
# claude_code; `claude/x` provider isn't the CLI provider, so map to claude-code).
_MODEL_ALIASES = {
    "claude/haiku": "claude-code/haiku",
    "claude/sonnet": "claude-code/sonnet",
    "claude": "claude-code/sonnet",
    "haiku": "claude-code/haiku",
    "sonnet": "claude-code/sonnet",
}

_REVIEW_TEMPLATE = """\
Review the file {file} for correctness defects.

For EACH finding, state:
  - location (function + line)
  - the defect, with code evidence
  - severity: none | low | medium | high | critical

Be precise. Do not invent issues. If the code is correct, say so explicitly.
If a behavior looks suspicious but is intended/by-design, label it as such and
do NOT rate it as a defect.
{grounding}"""


def _model(config: OrchestrationConfig):
    """Build the iModel via lionagi's canonical CLI resolver (handles yolo,
    effort clamping, and provider kwargs for codex AND claude-code)."""
    spec = _MODEL_ALIASES.get(config.model, config.model)
    return build_imodel_from_spec(spec, yolo=True, effort_override=config.effort)


def _prompt(task: Task, config: OrchestrationConfig) -> str:
    grounding = ""
    if config.grounding:
        grounding = f"\nDESIGN INTENT (authoritative):\n{config.grounding}\n"
    return _REVIEW_TEMPLATE.format(file=task.context["file"], grounding=grounding)


def _role_spec(role: str, config: OrchestrationConfig) -> AgentSpec:
    modes = list(config.critic_modes) if role == "critic" else None
    spec = _MODEL_ALIASES.get(config.model, config.model)
    return AgentSpec.compose(role, modes=modes, model=spec, yolo=True)


def _roster_guidance(config: OrchestrationConfig) -> str:
    """Tell the orchestrator the EXACT assignee names it may use.

    Without this, plan() only *validates* against the roster — the orchestrator
    guesses and emits assignee='default', so every assignment is dropped and
    build_dag_graph raises "no assignments mapped to a known role". This is the
    roster the CLI builds via role_roster(); we scope it to this config's roles.
    """
    roles = ", ".join(config.roles)
    return (
        f"Available roles — set each TaskAssignment.assignee to EXACTLY one of: "
        f"{roles}. Use ONLY these names; do not invent an assignee like 'default'. "
        f"Build a DAG: a reviewing role first, then a critic that depends on it, "
        f"then a synthesizer that depends on the critic."
    )


async def run_once(
    task: Task, config: OrchestrationConfig, trial: int, *, backend: str | None = None
) -> RunResult:
    """Run one trial, optionally through the ADR-0089 sandbox-backend seam.

    ``backend=None`` (default) is byte-for-byte the pre-existing in-process
    path — no behavior change for existing callers. ``"local_worktree"`` or
    ``"daytona"`` provisions an isolated workspace and then genuinely routes
    the trial through ``backend.run_cell()`` as a prompt-cell: the model call
    still runs host-side, already authenticated, via a subprocess that
    re-enters this same in-process trial body inside the sandboxed workspace
    (see ``_cell_entry.py``) — not a parallel, unused code path next to the
    handle. A backend that cannot host a prompt-cell host-side
    (``capabilities().hosts_prompt_cell_host_side`` is False, e.g. Daytona)
    fails fast here instead of silently falling back to the in-process path.
    """
    if backend is None:
        return await _run_once_inprocess(task, config, trial)

    from lionagi.tools.sandbox_backend import Cell, ProvisionSpec, get_backend

    sandbox_backend = get_backend(backend)
    caps = sandbox_backend.capabilities()
    if not caps.hosts_prompt_cell_host_side:
        raise ValueError(
            f"backend {backend!r} cannot host this trial: run_once()'s trial is "
            "always a prompt-cell (the model call runs host-side, already "
            "authenticated) and capabilities().hosts_prompt_cell_host_side is "
            "False for this backend (ADR-0089 §3) — pick a backend that can "
            "host prompt-cells; route exec-shaped work to this one instead"
        )

    handle = await sandbox_backend.provision(ProvisionSpec(repo_root=os.getcwd()))
    t0 = time.monotonic()
    try:
        entry_script = str(Path(__file__).resolve().with_name("_cell_entry.py"))
        cell = Cell(
            kind="prompt_cell",
            entrypoint=f"{shlex.quote(sys.executable)} {shlex.quote(entry_script)} in.pkl out.pkl",
            seed_inputs={"in.pkl": pickle.dumps((task, config, trial))},
            artifact_manifest=["out.pkl"],
            timeout_s=_CELL_TIMEOUT_S,
        )
        cell_result = await sandbox_backend.run_cell(handle, cell)
        out_bytes = cell_result.artifacts.get("out.pkl")
        if cell_result.exit_code != 0 or not out_bytes:
            detail = (
                cell_result.stderr or cell_result.stdout or "no output artifact produced"
            ).strip()
            result = RunResult(
                task_id=task.id,
                config_key=config.key(),
                trial=trial,
                outputs=[],
                wall_seconds=time.monotonic() - t0,
                error=f"sandbox cell failed (exit {cell_result.exit_code}): {detail[-2000:]}",
                model=config.model,
            )
        else:
            # Round-tripping our own RunResult, pickled moments ago by _cell_entry.py
            # in this same trial's sandboxed subprocess — not untrusted input (ADR-0089
            # §3: a prompt-cell runs no untrusted code).
            result = pickle.loads(out_bytes)  # noqa: S301
    finally:
        try:
            await sandbox_backend.teardown(handle)
        except Exception:  # noqa: BLE001 — teardown failure must not mask the trial result
            logger.exception("sandbox backend teardown failed: %s", backend)
    result.backend = backend
    return result


async def _run_once_inprocess(task: Task, config: OrchestrationConfig, trial: int) -> RunResult:
    """The pre-ADR-0089 in-process trial body. Catches errors into RunResult.error (never raises)."""
    t0 = time.monotonic()
    try:
        if config.pattern == "single":
            outputs, spawned, branches = await _run_single(task, config)
        elif config.pattern == "fanout":
            outputs, spawned, branches = await _run_fanout(task, config)
        else:
            outputs, spawned, branches = await _run_flow(task, config)
        prompt = _prompt(task, config)
        usage = collect_usage(branches, [(prompt, o) for o in outputs], config.model)
        return RunResult(
            task_id=task.id,
            config_key=config.key(),
            trial=trial,
            outputs=outputs,
            wall_seconds=time.monotonic() - t0,
            spawned=spawned,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            num_turns=usage.num_turns,
            n_calls=usage.n_calls,
            usage_source=usage.source,
            reasoning_disclosed=usage.reasoning_disclosed,
            model=config.model,
        )
    except Exception as e:  # noqa: BLE001 — a failed run is data, not a crash
        logger.exception("run_once failed: %s / %s", task.id, config.name)
        return RunResult(
            task_id=task.id,
            config_key=config.key(),
            trial=trial,
            outputs=[],
            wall_seconds=time.monotonic() - t0,
            error=f"{type(e).__name__}: {e}",
            model=config.model,
        )


async def _run_single(task: Task, config: OrchestrationConfig):
    branch = Branch(chat_model=_model(config))
    # A single agent runs as an auditor (the strongest solo reviewer role).
    spec = AgentSpec.compose("auditor", model=config.model, yolo=True)
    branch.msgs.set_system(branch.msgs.create_system(system=spec.build_system_message()))
    result = await branch.operate(instruction=_prompt(task, config))
    return [str(result)], 0, [branch]


async def _run_fanout(task: Task, config: OrchestrationConfig):
    from lionagi.casts.emission import TaskAssignment

    session = Session()
    orc = Branch(chat_model=_model(config))
    session.include_branches(orc)
    session.default_branch = orc
    roles = await spawn_roles(session, {r: _role_spec(r, config) for r in config.roles})
    # One assignment per non-synthesis role, all reviewing the same file.
    # Pin the target file with the same force as _run_flow (audit F5): an
    # AgentSpec auditor role may otherwise explore neighbouring repo files.
    file = task.context["file"]
    pin = f"\n\nTARGET FILE — review ONLY this exact file, no other repo files:\n{file}"
    workers = [r for r in config.roles if r != config.synthesis_role]
    assignments = [TaskAssignment(task=_prompt(task, config) + pin, assignee=r) for r in workers]
    results = await fanout(
        session,
        assignments,
        roles,
        synthesis_role=config.synthesis_role,
        max_concurrent=config.max_concurrent,
    )
    op = results.get("operation_results", {})
    return (
        [str(v) for v in op.values()],
        results.get("spawned_operations", 0),
        list(session.branches),
    )


async def _run_flow(task: Task, config: OrchestrationConfig):
    session = Session()
    orc = Branch(chat_model=_model(config))
    session.include_branches(orc)
    session.default_branch = orc
    roles = await spawn_roles(
        session,
        {r: _role_spec(r, config) for r in config.roles},
        spawners=set(config.roles) if config.reactive else (),
    )
    assignments = await plan(
        orc,
        _prompt(task, config),
        roles=list(roles.keys()),
        guidance=_roster_guidance(config),
    )
    if not assignments:
        raise RuntimeError("plan produced no valid assignments (orchestrator used unknown roles)")
    # CRITICAL measurement fix: plan() decomposes the prompt and the orchestrator
    # routinely drops the target file path from each TaskAssignment — workers then
    # wander and review unrelated repo files (observed: reviewed casts/pattern.py
    # instead of the mutant). Pin the exact file into every assignment so the flow
    # reviews what we planted, not whatever it stumbles on.
    file = task.context["file"]
    pin = f"\n\nTARGET FILE — review ONLY this exact file, no other repo files:\n{file}"
    assignments = [a.model_copy(update={"task": a.task + pin}) for a in assignments]
    graph, _ = build_dag_graph(session, assignments, roles)
    results = await session.flow(
        graph,
        reactive=config.reactive,
        spawn_type=SpawnRequest if config.reactive else None,
        node_builder=role_node_builder(roles) if config.reactive else None,
        max_spawn=config.max_spawn,
        max_concurrent=config.max_concurrent,
    )
    op = results.get("operation_results", {})
    return (
        [str(v) for v in op.values()],
        results.get("spawned_operations", 0),
        list(session.branches),
    )

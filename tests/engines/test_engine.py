# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine base machinery — stateless config + per-run EngineRun. No LLM."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from lionhub.orchestration.engines import Engine, EngineEvent


class Finding(EngineEvent):
    claim: str
    novelty: float = 0.5


def _run():
    return Engine().new_run()


@pytest.mark.asyncio
async def test_emit_records_and_queries():
    run = _run()
    await run.emit(Finding(claim="x", novelty=0.9))
    await run.emit(Finding(claim="y", novelty=0.2))
    assert len(run.by_type(Finding)) == 2
    # the emission store is queryable via pile[type] (Phase A)
    assert len(run.events[Finding]) == 2


@pytest.mark.asyncio
async def test_observe_reacts_to_type():
    run = _run()
    seen: list[str] = []

    @run.observe(Finding)
    def _on(f, _ctx):
        seen.append(f.claim)

    await run.emit(Finding(claim="hit"))
    assert seen == ["hit"]


@pytest.mark.asyncio
async def test_observe_with_field_filter():
    from lionagi.ln.types import Spec

    run = _run()
    high: list[Finding] = []

    @run.observe(Spec(float, name="novelty").q > 0.7)
    def _on(f, _ctx):
        high.append(f)

    await run.emit(Finding(claim="lo", novelty=0.1))
    await run.emit(Finding(claim="hi", novelty=0.9))
    assert [f.claim for f in high] == ["hi"]


@pytest.mark.asyncio
async def test_spawn_and_quiescence():
    run = _run()
    done: list[int] = []

    async def work(n: int) -> None:
        await asyncio.sleep(0.01)
        done.append(n)

    run.spawn(work(1))
    run.spawn(work(2))
    await run.wait_quiescence()
    assert sorted(done) == [1, 2]


@pytest.mark.asyncio
async def test_observer_spawns_depth_node():
    """The canonical engine loop: an emission triggers a spawned task."""
    run = Engine(max_depth=2).new_run()
    expanded: list[str] = []

    async def deeper(claim: str) -> None:
        await asyncio.sleep(0)
        expanded.append(claim)

    @run.observe(Finding)
    def _on(f, _ctx):
        if f.novelty > 0.7:
            run.spawn(deeper(f.claim))

    await run.emit(Finding(claim="deep", novelty=0.9))
    await run.emit(Finding(claim="shallow", novelty=0.3))
    await run.wait_quiescence()
    assert expanded == ["deep"]


@pytest.mark.asyncio
async def test_seen_dedup():
    run = _run()
    assert run.seen("Quantum Error Correction") is False  # first time → marked
    assert run.seen("quantum error correction") is True  # normalized dup


@pytest.mark.asyncio
async def test_two_runs_are_isolated():
    """A stateless engine: two runs do not share dedup/session state."""
    eng = Engine()
    a, b = eng.new_run(), eng.new_run()
    assert a.seen("topic") is False
    # b has its own _seen — the same key is still fresh
    assert b.seen("topic") is False
    assert a.session is not b.session


@pytest.mark.asyncio
async def test_run_team_sequences_and_carries_output():
    run = _run()
    calls: list[tuple[str, str]] = []

    def fake(name: str, reply: str):
        async def operate(*, instruction: str):
            calls.append((name, instruction))
            return reply

        return SimpleNamespace(name=name, operate=operate)

    team = [fake("a", "AOUT"), fake("b", "BOUT")]
    last = await run.run_team(team, "do the task")
    assert last == "BOUT"
    assert calls[0] == ("a", "do the task")
    assert "AOUT" in calls[1][1]  # b builds on a's output


@pytest.mark.asyncio
async def test_run_team_survives_agent_failure():
    run = _run()

    def boom(name: str):
        async def operate(*, instruction: str):
            raise RuntimeError("kaboom")

        return SimpleNamespace(name=name, operate=operate)

    def ok(name: str):
        async def operate(*, instruction: str):
            return "recovered"

        return SimpleNamespace(name=name, operate=operate)

    last = await run.run_team([boom("x"), ok("y")], "go")
    assert last == "recovered"  # team continued past the failure


@pytest.mark.asyncio
async def test_make_agent_builds_casts_branch_with_emissions():
    run = _run()
    b = await run.make_agent("researcher", name="r1", emits=(Finding,))
    assert b.name == "r1"
    assert b in run.session.branches
    assert b.capabilities is not None  # emissions granted
    assert b.system is not None  # casts role body composed


@pytest.mark.asyncio
async def test_make_agent_grants_emits_exactly_once(monkeypatch):
    """emits is threaded through AgentSpec so grant_capabilities fires exactly once."""
    from lionagi.session.branch import Branch

    calls: list[object] = []
    original = Branch.grant_capabilities

    def spy(self, operable, *, prompt: bool = True):
        calls.append(operable)
        return original(self, operable, prompt=prompt)

    monkeypatch.setattr(Branch, "grant_capabilities", spy)

    run = _run()
    b = await run.make_agent("researcher", name="r1", emits=(Finding,))
    assert len(calls) == 1  # one grant site, not two
    assert b.capabilities.allowed() == {"finding", "escalation_request"}


@pytest.mark.asyncio
async def test_make_agent_no_emits_uses_role_contract():
    """No emits ⇒ the role's declared contract is granted (researcher emits
    Finding + Gap), still via the single create_agent grant site."""
    run = _run()
    b = await run.make_agent("researcher", name="r1")
    role_op = b.capabilities
    assert role_op is not None
    from lionagi.casts import Role

    expected = Role.load("researcher").emission_operable().allowed()
    assert role_op.allowed() == expected


@pytest.mark.asyncio
async def test_make_agent_fires_on_branch_created():
    """make_agent must notify on_branch_created right after include_branches,
    with the branch's final name already set — mirrors the seam flow.py's
    _preallocate_all_branches already fires for flow-cloned branches."""
    created: list[object] = []
    run = Engine().new_run(on_branch_created=created.append)
    b = await run.make_agent("researcher", name="r1")
    assert created == [b]
    assert created[0].name == "r1"  # fired after branch.name was set


@pytest.mark.asyncio
async def test_make_agent_no_on_branch_created_is_a_no_op():
    """Default None must not affect any of the ~18 existing make_agent callers."""
    run = _run()
    b = await run.make_agent("researcher", name="r1")
    assert b.name == "r1"  # no AttributeError / crash from the new hook


@pytest.mark.asyncio
async def test_engine_run_threads_on_branch_created_into_make_agent():
    """Engine.run(..., on_branch_created=...) must reach EngineRun.make_agent
    for sub-agents spawned mid-run, not just at construction time."""

    class _SpawnsOneAgent(Engine):
        async def _run(self, run, *args, **kwargs):
            return await run.make_agent("researcher", name="sub1")

    created: list[object] = []
    result = await _SpawnsOneAgent().run("task", on_branch_created=created.append)
    assert len(created) == 1
    assert created[0] is result
    assert created[0].name == "sub1"


@pytest.mark.asyncio
async def test_run_dag_emits_node_lifecycle_signals():
    """run_dag executes a DAG and tees NodeStarted/NodeCompleted onto the bus."""
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeCompleted, NodeStarted

    async def work(**kw):
        return "ok"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("work", work)

    started: list[str] = []
    completed: list[str] = []
    session.observe(NodeStarted, handler=lambda s, _c: started.append(s.name))
    session.observe(NodeCompleted, handler=lambda s, _c: completed.append(s.op_id))

    builder = OperationGraphBuilder()
    builder.add_operation("work")
    graph = builder.get_graph()

    run = Engine().new_run(session=session)
    result = await run.run_dag(graph)

    assert len(result["completed_operations"]) == 1
    assert started == ["root"]  # NodeStarted reached the observer with the branch name
    assert len(completed) == 1  # NodeCompleted carried the op id


@pytest.mark.asyncio
async def test_run_dag_forwards_on_branch_created_to_session_flow():
    """The DAG entrypoint must preserve the persistence callback supplied by callers."""
    from unittest.mock import AsyncMock, patch

    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.session import Session

    session = Session()
    callback = lambda branch: None

    with patch.object(
        Session,
        "flow",
        new=AsyncMock(return_value={"operation_results": {}}),
    ) as session_flow:
        run = Engine().new_run(session=session)
        await run.run_dag(OperationGraphBuilder().get_graph(), on_branch_created=callback)

    assert session_flow.await_args.kwargs["on_branch_created"] is callback


# -- spawned task failures surface to caller ----------------------------------


@pytest.mark.asyncio
async def test_spawned_task_failure_is_reported():
    """wait_quiescence() must propagate RuntimeError from spawned tasks, not swallow it."""
    run = _run()

    async def boom():
        await asyncio.sleep(0)
        raise RuntimeError("engine node failed")

    run.spawn(boom())
    # On Python 3.11+ an ExceptionGroup is raised; on 3.10 the first exception
    # is re-raised directly.  Either way, the call must NOT return normally.
    raised: BaseException | None = None
    try:
        await run.wait_quiescence()
    except BaseException as exc:
        raised = exc
    assert raised is not None, "wait_quiescence must raise when a spawned task fails"
    # The original error message must be visible somewhere in the exception chain.
    assert "engine node failed" in str(raised)


@pytest.mark.asyncio
async def test_spawned_task_cancellation_is_not_surfaced():
    """CancelledError from a spawned task must be silently discarded —
    the whole run is not cancelled when a single branch is cancelled."""
    run = _run()
    done: list[int] = []

    async def cancel_me():
        raise asyncio.CancelledError

    async def succeed():
        await asyncio.sleep(0)
        done.append(1)

    run.spawn(cancel_me())
    run.spawn(succeed())
    # Must not raise; the CancelledError is discarded.
    await run.wait_quiescence()
    assert done == [1]


@pytest.mark.asyncio
async def test_failed_parent_still_drains_spawned_child():
    """A failing parent must drain its spawned child before surfacing the error."""
    run = _run()
    child_ran = asyncio.Event()

    async def child():
        await asyncio.sleep(0.02)
        child_ran.set()

    async def parent():
        run.spawn(child())  # schedule child, then fail
        await asyncio.sleep(0)
        raise RuntimeError("parent failed after spawning child")

    run.spawn(parent())

    raised: BaseException | None = None
    try:
        await run.wait_quiescence()
    except BaseException as exc:
        raised = exc

    assert raised is not None, "parent failure must be surfaced"
    assert "parent failed after spawning child" in str(raised)
    # The contract: the run is genuinely quiescent — child finished, none left.
    assert child_ran.is_set(), "child spawned by failed parent must still run"
    assert not run._active, "wait_quiescence must leave no background tasks running"


@pytest.mark.asyncio
async def test_successful_tasks_do_not_raise():
    """Successful spawned tasks must complete without raising."""
    run = _run()
    results: list[str] = []

    async def work(val: str) -> None:
        await asyncio.sleep(0)
        results.append(val)

    run.spawn(work("a"))
    run.spawn(work("b"))
    await run.wait_quiescence()
    assert sorted(results) == ["a", "b"]


def _capture_specs(monkeypatch):
    """Record every AgentSpec create_agent receives, without changing what it does."""
    from lionhub.orchestration.engines import engine as engine_mod

    specs = []
    real = engine_mod.create_agent

    async def spy(spec, **kwargs):
        specs.append(spec)
        return await real(spec, **kwargs)

    monkeypatch.setattr(engine_mod, "create_agent", spy)
    return specs


def _mcp_file(tmp_path, name):
    path = tmp_path / name
    path.write_text('{"mcpServers": {}}', encoding="utf-8")
    return str(path)


async def _planning_workers(
    monkeypatch,
    engine,
    *,
    on_branch_created=None,
    spawn_base=None,
    template_setup=None,
    defer_spawn=None,
):
    """Construct real worker templates and DAG clones without executing a provider."""
    from lionagi.casts.emission import TaskAssignment
    from lionhub.orchestration import patterns
    from lionhub.orchestration.engines import engine as engine_mod

    specs = []
    workers = []
    real_create = engine_mod.create_agent

    async def capture(spec, **kwargs):
        specs.append(spec)
        return await real_create(spec, **kwargs)

    def created(branch):
        if on_branch_created is not None:
            on_branch_created(branch)
        # A role template is created before any clone of it exists, so its
        # registry can be configured before cloning carries it forward.
        if template_setup is not None and branch.metadata.get("clone_from") is None:
            template_setup(branch)

    # Observe both construction paths so a regression is a dropped setting,
    # rather than a test coupled to which helper the engine calls.
    monkeypatch.setattr(engine_mod, "create_agent", capture)
    monkeypatch.setattr(patterns, "create_agent", capture)
    run = engine.new_run(on_branch_created=created)

    async def plan(_run, prompt, max_ops):
        return [
            TaskAssignment(task="first", assignee="researcher"),
            TaskAssignment(task="second", assignee="researcher"),
            TaskAssignment(task="third", assignee="analyst"),
        ]

    async def execute(graph, **kwargs):
        workers.extend(run.session.branches[node.branch_id] for node in graph.internal_nodes)
        if defer_spawn is not None:
            # The live order: a dependent child is cloned after the emitter's
            # turn has completed and left its response on the emitter branch.
            # Return what a caller needs to build the child itself, after it
            # has run the emitter.
            defer_spawn.update(
                node_builder=kwargs["node_builder"],
                on_branch_created=kwargs["on_branch_created"],
                emitter=next(iter(graph.internal_nodes)),
                graph=graph,
            )
        if spawn_base is not None:
            from lionagi.casts.emission import SpawnRequest
            from lionagi.operations.flow import ReactiveExecutor

            emitter = next(iter(graph.internal_nodes))
            request = SpawnRequest(
                instruction="Follow up", assignee="researcher" if spawn_base == "role" else None
            )
            child = kwargs["node_builder"](request, emitter)
            executor = ReactiveExecutor(
                run.session, graph, on_branch_created=kwargs["on_branch_created"]
            )
            executor.operation_branches[emitter.id] = workers[0]
            executor._assign_injected_branch(child, emitter.id, independent=False)
            workers.append(run.session.branches[child.branch_id])
        return {"operation_results": {}}

    async def synthesize(*args):
        return "done"

    monkeypatch.setattr(engine, "_plan", plan)
    monkeypatch.setattr(engine, "_synthesize", synthesize)
    monkeypatch.setattr(run, "run_dag", execute)
    assert await engine._run(run, "a task") == "done"
    return run, specs, workers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting,value,spec_field",
    [
        ("model", "codex/gpt-5.5", "model"),
        ("effort", "high", "effort"),
        ("yolo", True, "yolo"),
        ("agent_cwd", "workspace", "cwd"),
        ("agent_extra_prompt", "Keep the worker evidence concise.", "extra_prompt"),
        ("khive_injection", True, "khive_injection"),
    ],
    ids=["model", "effort", "yolo", "cwd", "extra-prompt", "injection"],
)
async def test_planning_worker_settings_reach_real_agents(
    monkeypatch, tmp_path, setting, value, spec_field
):
    from lionhub.orchestration.engines import PlanningEngine

    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    if setting == "agent_cwd":
        workspace = tmp_path / value
        workspace.mkdir()
        value = str(workspace)
    settings = {
        "model": "codex/gpt-5.5",
        "khive_injection": False,
        "agent_mcp_config_path": _mcp_file(tmp_path, "workers.mcp.json"),
        setting: value,
    }
    run, specs, workers = await _planning_workers(monkeypatch, PlanningEngine(**settings))
    expected = (
        {"namespace": f"planningengine:{run.run_id}"} if setting == "khive_injection" else value
    )

    assert len(specs) == 2  # one template per role, not per assignment
    for spec in specs:
        assert getattr(spec, spec_field) == expected
    assert len(workers) == 3
    for worker in workers:
        kwargs = worker.chat_model.endpoint.config.kwargs
        if setting == "model":
            assert worker.chat_model.model_name == "gpt-5.5"
        elif setting == "effort":
            assert kwargs["reasoning_effort"] == value
        elif setting == "yolo":
            assert kwargs["full_auto"] is True
            assert kwargs["skip_git_repo_check"] is True
        elif setting == "agent_cwd":
            assert str(kwargs["repo"]) == value
        elif setting == "agent_extra_prompt":
            assert value in worker._system_text()
        elif setting == "khive_injection":
            # The clone must carry the template's provider registry, not just the
            # spec field that built it: same provider entries, counted under the
            # clone's own budget.
            assert worker._context_providers is not None
            assert worker.providers.names == worker.metadata["clone_from"].providers.names
            assert worker.providers.names


@pytest.mark.asyncio
async def test_engine_wide_mcp_config_path_reaches_the_agent_spec(monkeypatch, tmp_path):
    """An engine can name the .mcp.json its agents resolve.

    Without this the spec field is left unset and every agent falls through to
    the user-level ~/.lionagi/.mcp.json — a machine-global file other tools
    write, so a run depends on a config it never named and an unrelated write
    to that file breaks every engine on the machine at once.
    """
    specs = _capture_specs(monkeypatch)
    declared = _mcp_file(tmp_path, "declared.mcp.json")

    run = Engine(agent_mcp_config_path=declared).new_run()
    await run.make_agent("researcher", name="r1")

    assert specs[-1].mcp_config_path == declared


@pytest.mark.asyncio
async def test_per_call_mcp_config_path_outranks_the_engine_wide_default(monkeypatch, tmp_path):
    """Same precedence as cwd and extra_prompt: explicit call beats engine-wide."""
    specs = _capture_specs(monkeypatch)
    engine_wide = _mcp_file(tmp_path, "engine.mcp.json")
    per_call = _mcp_file(tmp_path, "percall.mcp.json")

    run = Engine(agent_mcp_config_path=engine_wide).new_run()
    await run.make_agent("researcher", name="r1", mcp_config_path=per_call)
    await run.make_agent("researcher", name="r2")

    assert specs[-2].mcp_config_path == per_call
    assert specs[-1].mcp_config_path == engine_wide


@pytest.mark.asyncio
async def test_unset_mcp_config_path_leaves_resolution_untouched(monkeypatch, tmp_path):
    """Default must not change behaviour for the existing make_agent callers.

    Leaving the field None is what preserves the current discovery order;
    writing a value in by default here would silently repoint every existing
    engine at a different config.

    HOME is redirected at a scratch dir on purpose. Unset is exactly the case
    that falls through to the user-level ~/.lionagi/.mcp.json, so without this
    the assertion below would load whatever MCP servers the developer happens
    to have configured, and the outcome would differ between a laptop and CI.
    That machine dependence is the thing this feature exists to remove, so a
    test for it must not itself rely on the ambient file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    specs = _capture_specs(monkeypatch)

    run = Engine().new_run()
    await run.make_agent("researcher", name="r1")

    assert specs[-1].mcp_config_path is None


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False], ids=["explicit", "ambient"])
async def test_planning_workers_preserve_engine_mcp_resolution(monkeypatch, tmp_path, declared):
    """A regression pin: the engine-wide MCP config already reached workers.

    The worker path carried ``agent_mcp_config_path`` before the shared agent
    builder, so this guards existing behaviour rather than a new one.
    """
    from lionhub.orchestration.engines import PlanningEngine

    path = _mcp_file(tmp_path, "declared.mcp.json") if declared else None
    engine = PlanningEngine(model="codex/gpt-5.5", agent_mcp_config_path=path)
    _, specs, _ = await _planning_workers(monkeypatch, engine)
    assert len(specs) == 2
    assert all(spec.mcp_config_path == path for spec in specs)


@pytest.mark.asyncio
async def test_planning_worker_templates_use_agent_accounting(monkeypatch, tmp_path):
    from lionhub.orchestration.engines import PlanningEngine

    created = []
    engine = PlanningEngine(
        model="codex/gpt-5.5", agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json")
    )
    run, specs, workers = await _planning_workers(
        monkeypatch, engine, on_branch_created=created.append
    )
    assert run.agents_made == len(specs) == len(created) == 2
    assert {branch.name for branch in created} == {"researcher", "analyst"}
    assert {str(branch.id) for branch in created}.isdisjoint(str(worker.id) for worker in workers)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_agents", [1, 2], ids=["one", "two"])
async def test_planning_role_templates_stay_within_a_tight_agent_budget(
    monkeypatch, tmp_path, max_agents
):
    """One role template per distinct assignee, exempt from the agent budget.

    A budget at or below the assignee count must not abort the run before any
    planned task executes: the templates are scaffolding the plan already
    fixed, not discretionary agents. The orchestrator the real pipeline builds
    first is what consumes this budget.
    """
    from lionagi.casts.emission import TaskAssignment
    from lionhub.orchestration.engines import PlanningEngine

    engine = PlanningEngine(
        model="codex/gpt-5.5",
        max_agents=max_agents,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    created: list = []
    run = engine.new_run(on_branch_created=created.append)

    async def plan(orchestrator, prompt, *, roles, dag, max_tasks, guidance=""):
        return [
            TaskAssignment(task="first", assignee="researcher"),
            TaskAssignment(task="second", assignee="analyst"),
        ]

    async def execute(graph, **kwargs):
        return {"operation_results": {}}

    async def synthesize(*args):
        return "done"

    monkeypatch.setattr("lionhub.orchestration.engines.planning.plan", plan)
    monkeypatch.setattr(engine, "_synthesize", synthesize)
    monkeypatch.setattr(run, "run_dag", execute)

    assert await engine._run(run, "a task") == "done"
    assert {branch.name for branch in created} == {"orchestrator", "researcher", "analyst"}
    assert run._budget_notified is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spawn_base", [None, "role", "worker"], ids=["planned", "role-child", "worker-child"]
)
async def test_planning_dag_clones_keep_injection(monkeypatch, tmp_path, spawn_base):
    """A clone that starts from a role template recalls on its first turn.

    A planned worker and a role-targeted spawn both start from a role template,
    which carries no conversation, so khive's first-turn cadence injects one
    block for each. A dependent spawn with no assignee runs on a clone of its
    emitter's branch and continues the emitter's conversation, so it is not a
    first turn and the cadence adds no block of its own. The child is cloned
    only after the emitter has answered, as it is in a live run.
    """
    from lionagi.casts.emission import SpawnRequest
    from lionagi.operations.flow import ReactiveExecutor
    from lionagi.testing import scripted_imodel
    from lionagi.tools.khive_injection import KhiveInjectionProvider
    from lionhub.orchestration.engines import PlanningEngine

    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    namespaces = []
    template_budget = 4096

    async def recall(provider, query):
        namespaces.append(provider.policy.namespace)
        return "WORKER-ONLY-REFERENCE"

    def give_templates_a_distinct_budget(branch):
        # A non-default value so a clone that lost the template's budget reads
        # the registry default instead of the value the template carried.
        branch.providers.budget = template_budget

    monkeypatch.setattr(KhiveInjectionProvider, "_recall", recall)
    engine = PlanningEngine(
        model="codex/gpt-5.5",
        khive_injection=True,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    deferred: dict = {}
    run, _, workers = await _planning_workers(
        monkeypatch,
        engine,
        template_setup=give_templates_a_distinct_budget,
        defer_spawn=deferred if spawn_base is not None else None,
    )
    planned = list(workers)

    async def run_turn(worker):
        # Swap only the model transport: real agent construction, cloning,
        # provider registration and operate's pre-turn fold still run.
        model = scripted_imodel([{"type": "text", "content": "done"}])
        worker.chat_model = worker.parse_model = model
        await worker.operate(instruction="Verify the worker evidence")
        return model.endpoint.calls[0].payload["messages"]

    for worker in planned:
        messages = await run_turn(worker)
        assert "WORKER-ONLY-REFERENCE" in messages[0]["content"]
        assert "WORKER-ONLY-REFERENCE" not in str(worker.msgs.messages)
        source = worker.metadata["clone_from"]
        assert worker.providers is not source.providers
        assert worker.providers.budget == source.providers.budget == template_budget
        assert worker.providers.stats["blocks_injected"] == 1

    expected_recalls = len(planned)
    if spawn_base is not None:
        # Live order: the emitter (the first planned clone) has answered above,
        # so its clone now carries a response. Build the child only now.
        emitter = deferred["emitter"]
        request = SpawnRequest(
            instruction="Follow up",
            assignee="researcher" if spawn_base == "role" else None,
        )
        child = deferred["node_builder"](request, emitter)
        executor = ReactiveExecutor(
            run.session, deferred["graph"], on_branch_created=deferred["on_branch_created"]
        )
        executor.operation_branches[emitter.id] = planned[0]
        executor._assign_injected_branch(child, emitter.id, independent=False)
        spawned = run.session.branches[child.branch_id]
        assert spawned.providers.budget == template_budget
        messages = await run_turn(spawned)
        assert "WORKER-ONLY-REFERENCE" not in str(spawned.msgs.messages)
        if spawn_base == "role":
            # The role-targeted spawn starts from the role template: a first turn.
            assert spawned.metadata["clone_from"] is planned[0].metadata["clone_from"]
            assert "WORKER-ONLY-REFERENCE" in messages[0]["content"]
            assert spawned.providers.stats["blocks_injected"] == 1
            expected_recalls += 1
        else:
            # The dependent unassigned spawn continues the emitter's
            # conversation, so the first-turn cadence adds no block.
            assert "WORKER-ONLY-REFERENCE" not in messages[0]["content"]
            assert spawned.providers.stats["blocks_injected"] == 0
            assert spawned.msgs.last_response is not None
            assert spawned.metadata["clone_from"] is planned[0]
            # The provider is still registered on the child, so the zero above
            # comes from the cadence, not from a child with no provider.
            assert planned[0].providers.names
            assert spawned.providers.names == planned[0].providers.names

    assert namespaces == [f"planningengine:{run.run_id}"] * expected_recalls


def _fenced_spawn(assignee, *, independent=False):
    """One assistant reply carrying a fenced ``spawn_request`` capability block."""
    request = {
        "spawn_request": {
            "instruction": "Follow up",
            "reason": "Adjacent work",
            "independent": independent,
            "assignee": assignee,
        }
    }
    return f"Work done.\n```json\n{json.dumps(request)}\n```"


async def _reactive_planning_run(monkeypatch, tmp_path, replies_by_clone):
    """Drive a real reactive planning DAG on scripted worker models.

    ``replies_by_clone`` maps the 1-based ordinal of each managed worker clone
    (the planned clones first, then each reactively spawned one) to the text its
    model answers with, so a test can choose which node kind emits a fenced
    ``spawn_request``. Real construction, cloning, capability inheritance and
    emission extraction all run; only the transport is scripted.
    """
    from lionagi.casts.emission import TaskAssignment
    from lionagi.testing import scripted_imodel
    from lionhub.orchestration.engines import PlanningEngine
    from lionhub.orchestration.engines import planning as planning_mod

    engine = PlanningEngine(
        model="codex/gpt-5.5",
        reactive=True,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    run = engine.new_run()
    dag_result: dict = {}

    async def plan(_run, prompt, max_ops):
        return [TaskAssignment(task="first", assignee="researcher")]

    async def synthesize(_run, prompt, assignments, node_ids, result):
        dag_result.update(result)
        return "done"

    monkeypatch.setattr(engine, "_plan", plan)
    monkeypatch.setattr(engine, "_synthesize", synthesize)

    real_inherit = planning_mod._inherit_worker_providers
    seen = {"n": 0}

    def scripted_inherit(branch):
        real_inherit(branch)
        seen["n"] += 1
        model = scripted_imodel([{"type": "text", "content": replies_by_clone(seen["n"])}])
        branch.chat_model = branch.parse_model = model

    monkeypatch.setattr(planning_mod, "_inherit_worker_providers", scripted_inherit)
    assert await engine._run(run, "a task") == "done"
    return run, dag_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "emit_from,replies",
    [
        ("planned", lambda n: _fenced_spawn("researcher") if n == 1 else "done"),
        (
            "role-child",
            lambda n: _fenced_spawn("researcher") if n <= 2 else "done",
        ),
        (
            "worker-child",
            lambda n: _fenced_spawn(None, independent=True) if n <= 2 else "done",
        ),
    ],
    ids=["planned", "role-child", "worker-child"],
)
async def test_planning_clones_keep_the_fenced_spawn_grant(
    monkeypatch, tmp_path, emit_from, replies
):
    """A worker clone keeps the template's ``SpawnRequest`` grant.

    ``grant_spawn`` sets the grant on the role template; a clone that lost it
    drops a fenced ``spawn_request`` its model emits, so the live DAG silently
    stops expanding. Each of the three node kinds must still emit: a planned
    worker, a role-targeted child, and a child spawned by a reactively spawned
    worker.
    """
    _, result = await _reactive_planning_run(monkeypatch, tmp_path, replies)
    assert result["spawned_operations"] == 1 + (emit_from != "planned"), result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spawn_base", [None, "role", "worker"], ids=["planned", "role-child", "worker-child"]
)
async def test_planning_clones_keep_the_worker_role_for_injection(
    monkeypatch, tmp_path, spawn_base
):
    """A worker clone keeps the template's role name.

    With no name, khive injection falls back to the generic ``agent`` for both
    the recall query and the writeback tags, so a worker's recalls and stored
    lessons are attributed to no role at all.
    """
    from unittest.mock import AsyncMock

    from lionagi.operations.fields import ActionResponseModel
    from lionagi.testing import scripted_imodel
    from lionagi.tools.khive_injection import KhiveInjectionProvider
    from lionhub.orchestration.engines import PlanningEngine

    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    queries: list[str] = []

    async def recall(provider, query):
        queries.append(query)
        return "WORKER-ONLY-REFERENCE"

    monkeypatch.setattr(KhiveInjectionProvider, "_recall", recall)
    engine = PlanningEngine(
        model="codex/gpt-5.5",
        khive_injection=True,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    _, _, workers = await _planning_workers(monkeypatch, engine, spawn_base=spawn_base)

    call_khive = AsyncMock(
        return_value={
            "results": [{"ok": True, "tool": "memory.remember", "result": {"id": "m1"}}],
            "summary": {"total": 1, "succeeded": 1, "failed": 0},
            "status": "success",
        }
    )
    monkeypatch.setattr("lionagi.tools.khive_injection._call_khive", call_khive)

    for worker in workers:
        role = worker.metadata["clone_from"].name
        assert worker.name == role
        model = scripted_imodel([{"type": "text", "content": "done"}])
        worker.chat_model = worker.parse_model = model
        await worker.operate(instruction="Verify the worker evidence")
    assert [q.split(" ", 1)[0] for q in queries] == [
        f"role={w.metadata['clone_from'].name}" for w in workers
    ], queries
    assert "role=agent " not in queries

    # The first planned worker and the last node (the spawned child when there
    # is one) each write back under their own role.
    for worker in (workers[0], workers[-1]):
        provider = worker.providers._entries[0].provider
        written = await provider.writeback(
            worker,
            [
                ActionResponseModel(function="bash", arguments={}, output={"error": "fail"}),
                ActionResponseModel(function="bash", arguments={}, output={"stdout": "ok"}),
            ],
        )
        assert written == 1
        ops = call_khive.await_args.args[0]
        assert f'"agent:{worker.name}"' in ops
        assert '"agent:agent"' not in ops


@pytest.mark.asyncio
@pytest.mark.parametrize("assignee", [None, ""], ids=["none", "empty"])
async def test_planning_independent_unassigned_spawn_keeps_the_engine_settings(
    monkeypatch, tmp_path, assignee
):
    """An independent, unassigned spawn runs on a branch carrying engine settings.

    With no assignee to route to, the executor falls back to the bare default
    branch: the new node loses every engine agent setting, and it must not
    inherit the emitter's conversation either, which is why the base is the
    role template and not the emitter's clone. An empty assignee is unassigned
    too: the role router treats it the same way, so it must not skip the
    template.
    """
    from lionagi.casts.emission import SpawnRequest, TaskAssignment
    from lionagi.operations.flow import ReactiveExecutor
    from lionhub.orchestration.engines import PlanningEngine
    from lionhub.orchestration.engines.planning import (
        _inherit_worker_providers,
        _role_template_node_builder,
    )
    from lionhub.orchestration.patterns import build_dag_graph

    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    engine = PlanningEngine(
        model="codex/gpt-5.5",
        effort="high",
        agent_extra_prompt="EMITTER-SETTINGS-MUST-CARRY",
        khive_injection=True,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    run = engine.new_run()
    template = await run.make_agent("researcher", name="researcher")
    template.providers.budget = 4096
    roles = {"researcher": template}
    assignments = [TaskAssignment(task="first", assignee="researcher")]
    graph, _ = build_dag_graph(run.session, assignments, roles)
    emitter = next(iter(graph.internal_nodes))
    emitter_branch = run.session.branches[emitter.branch_id]
    _inherit_worker_providers(emitter_branch)
    emitter_branch.msgs.add_message(
        instruction="EMITTER-ONLY-CONTEXT",
        sender=str(emitter_branch.user),
        recipient=emitter_branch.id,
    )

    builder = _role_template_node_builder(roles, run.session)
    request = SpawnRequest(instruction="independent follow-up", assignee=assignee, independent=True)
    child = builder(request, emitter)

    executor = ReactiveExecutor(run.session, graph, on_branch_created=_inherit_worker_providers)
    executor.operation_branches[emitter.id] = emitter_branch
    executor._assign_injected_branch(child, emitter.id, independent=True)
    spawned = run.session.branches[child.branch_id]

    assert spawned is not emitter_branch
    assert spawned.metadata["clone_from"] is template
    assert spawned.metadata["clone_from"] is not run.session.default_branch
    assert "EMITTER-SETTINGS-MUST-CARRY" in spawned._system_text()
    assert spawned._context_providers is not None
    assert spawned.providers.budget == 4096
    assert spawned.chat_model.model_name == template.chat_model.model_name
    assert "EMITTER-ONLY-CONTEXT" not in str(spawned.msgs.messages)


@pytest.mark.asyncio
async def test_planning_provider_inheritance_is_idempotent_and_skips_an_empty_source(tmp_path):
    """Inheriting twice registers nothing twice, and an empty source allocates nothing.

    A second pass would duplicate every provider, injecting each block twice;
    and a source with a registry but no providers would allocate an empty
    registry on the clone and claim it had injection.
    """
    from lionagi.protocols.context_providers import ContextProviderRegistry
    from lionhub.orchestration.engines import PlanningEngine
    from lionhub.orchestration.engines.planning import _inherit_worker_providers

    engine = PlanningEngine(
        model="codex/gpt-5.5",
        khive_injection=True,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    run = engine.new_run()
    template = await run.make_agent("researcher", name="researcher")
    template.providers.budget = 4096
    clone = template.clone(sender=run.session.id)
    _inherit_worker_providers(clone)
    _inherit_worker_providers(clone)
    assert len(clone.providers._entries) == 1
    assert clone.providers.budget == 4096

    empty_source = await run.make_agent("analyst", name="analyst")
    empty_source._context_providers = ContextProviderRegistry()
    bare_clone = empty_source.clone(sender=run.session.id)
    _inherit_worker_providers(bare_clone)
    assert bare_clone._context_providers is None

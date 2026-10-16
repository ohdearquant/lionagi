# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine base machinery — stateless config + per-run EngineRun. No LLM."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lionagi.engines import Engine, EngineEvent


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
    from lionagi.engines import engine as engine_mod

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


async def _planning_workers(monkeypatch, engine, *, on_branch_created=None, spawn_base=None):
    """Construct real worker templates and DAG clones without executing a provider."""
    from lionagi.casts.emission import TaskAssignment
    from lionagi.engines import engine as engine_mod
    from lionagi.orchestration import patterns

    specs = []
    workers = []
    real_create = engine_mod.create_agent

    async def capture(spec, **kwargs):
        specs.append(spec)
        return await real_create(spec, **kwargs)

    # Observe both construction paths so a regression is a dropped setting,
    # rather than a test coupled to which helper the engine calls.
    monkeypatch.setattr(engine_mod, "create_agent", capture)
    monkeypatch.setattr(patterns, "create_agent", capture)
    run = engine.new_run(on_branch_created=on_branch_created)

    async def plan(_run, prompt, max_ops):
        return [
            TaskAssignment(task="first", assignee="researcher"),
            TaskAssignment(task="second", assignee="researcher"),
            TaskAssignment(task="third", assignee="analyst"),
        ]

    async def execute(graph, **kwargs):
        workers.extend(run.session.branches[node.branch_id] for node in graph.internal_nodes)
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
    from lionagi.engines import PlanningEngine

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
    from lionagi.engines import PlanningEngine

    path = _mcp_file(tmp_path, "declared.mcp.json") if declared else None
    engine = PlanningEngine(model="codex/gpt-5.5", agent_mcp_config_path=path)
    _, specs, _ = await _planning_workers(monkeypatch, engine)
    assert len(specs) == 2
    assert all(spec.mcp_config_path == path for spec in specs)


@pytest.mark.asyncio
async def test_planning_worker_templates_use_agent_accounting(monkeypatch, tmp_path):
    from lionagi.engines import PlanningEngine

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
async def test_planning_workers_respect_the_agent_budget(monkeypatch, tmp_path):
    from lionagi.engines import PlanningEngine
    from lionagi.engines.engine import EngineBudgetError

    created = []
    engine = PlanningEngine(
        model="codex/gpt-5.5",
        max_agents=1,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    with pytest.raises(EngineBudgetError, match="agent budget exhausted"):
        await _planning_workers(monkeypatch, engine, on_branch_created=created.append)
    assert len(created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spawn_base", [None, "role", "worker"], ids=["planned", "role-child", "worker-child"]
)
async def test_planning_dag_clones_keep_injection(monkeypatch, tmp_path, spawn_base):
    from lionagi.engines import PlanningEngine
    from lionagi.testing import scripted_imodel
    from lionagi.tools.khive_injection import KhiveInjectionProvider

    monkeypatch.delenv("LIONAGI_KHIVE_INJECTION", raising=False)
    namespaces = []

    async def recall(provider, query):
        namespaces.append(provider.policy.namespace)
        return "WORKER-ONLY-REFERENCE"

    monkeypatch.setattr(KhiveInjectionProvider, "_recall", recall)
    engine = PlanningEngine(
        model="codex/gpt-5.5",
        khive_injection=True,
        agent_mcp_config_path=_mcp_file(tmp_path, "workers.mcp.json"),
    )
    run, _, workers = await _planning_workers(monkeypatch, engine, spawn_base=spawn_base)
    for worker in workers:
        # Swap only the model transport: real agent construction, cloning,
        # provider registration and operate's pre-turn fold still run.
        model = scripted_imodel([{"type": "text", "content": "done"}])
        worker.chat_model = worker.parse_model = model
        await worker.operate(instruction="Verify the worker evidence")
        messages = model.endpoint.calls[0].payload["messages"]
        assert "WORKER-ONLY-REFERENCE" in messages[0]["content"]
        assert "WORKER-ONLY-REFERENCE" not in str(worker.msgs.messages)
        source = worker.metadata["clone_from"]
        assert worker.providers is not source.providers
        assert worker.providers.budget == source.providers.budget
        assert worker.providers.stats["blocks_injected"] == 1
    assert namespaces == [f"planningengine:{run.run_id}"] * len(workers)

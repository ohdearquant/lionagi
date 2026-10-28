# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestration ownership boundaries with SQLite and in-process provider work."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def _reached(event, *calls):
    """Surface a failed producer instead of timing out waiting for its next barrier."""
    waiter = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait(
            [waiter, *calls], timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        for call in calls:
            if call in done:
                await call
        assert waiter in done, "producer did not reach the ownership barrier"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest_asyncio.fixture(loop_scope="function")
async def orchestration_workers(tmp_path, monkeypatch):
    from lionagi import Branch, Session
    from lionagi._cli_worker_context import (
        _bind_cli_worker_recorder,
        _get_cli_worker_recorder,
    )
    from lionagi.cli._runs import RunDir, _unroute_message_hooks
    from lionagi.cli.orchestrate import _orchestration as shared
    from lionagi.cli.orchestrate import fanout, flow
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.state import cli_workers
    from lionagi.state import db as db_module
    from lionagi.state.lifecycle import notify_settings

    state = SimpleNamespace(
        shared=shared,
        flow=flow,
        fanout=fanout,
        db_module=db_module,
        workers=cli_workers,
        get_recorder=_get_cli_worker_recorder,
        bind=_bind_cli_worker_recorder,
        db_path=tmp_path / "state.db",
        dbs=[],
        envs=[],
        branches=[],
        contexts=[],
        tasks=[],
        releases=[],
        observations=[],
    )
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", state.db_path)
    monkeypatch.setattr(
        db_module,
        "settings",
        db_module.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    monkeypatch.setenv("LIONAGI_AGENT_DEPTH", "0")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shared, "save_last_branch_pointer", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "lionagi.cli.kill.current_pid_markers",
        lambda: {
            "pid": 4200,
            "pid_create_time": 200.0,
            "pid_host": "synthetic-host",
            "pid_boot_time": 100.0,
            "process_identity_mode": "local",
        },
    )
    monkeypatch.setattr(
        notify_settings, "register_run_notify_outcome_scope", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        notify_settings, "unregister_run_notify_outcome_scope", lambda *args, **kwargs: None
    )

    async def open_store(**kwargs):
        # One real handle per execution; no process-wide shared registration.
        db = db_module.StateDB(state.db_path)
        state.dbs.append(db)
        await db.open()
        return db

    monkeypatch.setattr(shared, "_open_shared_db", open_store)
    real_start = shared.start_live_persist

    async def start(env, **kwargs):
        await real_start(env, **kwargs)
        if env._live_persist is not None:
            env._live_persist["shared_db_registered"] = False
            state.contexts.append(env._live_persist)

    monkeypatch.setattr(flow, "start_live_persist", start)
    monkeypatch.setattr(fanout, "start_live_persist", start)

    def make_env(label):
        branch = Branch(name="orchestrator")
        state.branches.append(branch)
        session = Session(default_branch=branch)
        run = RunDir(
            run_id=f"{label}-run",
            state_root=tmp_path / label / "state",
            artifact_root=tmp_path / label / "artifacts",
        )
        run.ensure_state_dirs()
        run.ensure_artifact_root()
        env = shared.OrchestrationEnv(
            run=run,
            session=session,
            orc_branch=branch,
            builder=OperationGraphBuilder(),
            orc_profile=None,
            orc_profile_name=None,
            default_model_spec="codex/model",
            bare=False,
            effort=None,
            theme=None,
            yolo=False,
            bypass=False,
            verbose=False,
            fast=False,
            cwd=str(tmp_path),
        )
        state.envs.append(env)
        return env

    def task(coro):
        call = asyncio.create_task(coro)
        state.tasks.append(call)
        return call

    async def run(kind, **kwargs):
        if kind == "flow":
            return await flow._run_flow("codex/model", "work", project="test/ownership", **kwargs)
        return await fanout._run_fanout("codex/model", "work", project="test/ownership", **kwargs)

    def owner_at(env, phase):
        owner = (env._live_persist or {}).get("worker_spawn_owner")
        assert owner is not None, f"{phase}: persisted orchestration needs a worker owner"
        assert state.get_recorder() is owner, f"{phase}: bind the owner before provider tasks"
        assert owner.session_id == str(env.session.id)
        state.observations.append((env.run.run_id, phase, owner))
        return owner

    async def record_task(env, phase, number):
        # A provider stand-in: the real token, owner and StateDB commit remain.
        owner = owner_at(env, phase)
        admission = owner.admit()
        admission.retain(asyncio.current_task())
        try:
            await admission.record(
                {
                    "pid": 5000 + number,
                    "pgid": 5000 + number,
                    "pid_create_time": 200.0 + number,
                    "pid_host": "synthetic-host",
                    "pid_boot_time": 100.0,
                    "process_identity_mode": "local",
                }
            )
        finally:
            admission.finish()

    state.make_env = make_env
    state.task = task
    state.run = run
    state.owner_at = owner_at
    state.record_task = record_task
    with state.bind(None):
        try:
            yield state
        finally:
            for release in state.releases:
                release.set()
            for call in state.tasks:
                if not call.done():
                    call.cancel()
            await asyncio.gather(*state.tasks, return_exceptions=True)
            for ctx in state.contexts:
                owner = ctx.get("worker_spawn_owner")
                if owner is not None:
                    await owner.aclose()
                _unroute_message_hooks(ctx)
                ctx["session"].observer.unbind_db_persistence()
            for db in state.dbs:
                await db.close()
            for branch in state.branches:
                await branch.mdls.shutdown()


async def test_generation_is_stable_across_sqlite_admission_retry(
    orchestration_workers, monkeypatch
):
    h = orchestration_workers
    env = h.make_env("retry")
    attempted = []
    hook_owners = []
    retry_delays = []
    real_create = h.db_module.StateDB.create_session
    real_hook = h.shared.register_branch_hook

    async def create_session(db, record):
        attempted.append(dict(record))
        if len(attempted) == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real_create(db, record)

    def register_hook(ctx, branch):
        hook_owners.append(ctx.get("worker_spawn_owner"))
        return real_hook(ctx, branch)

    async def retry_sleep(delay):
        retry_delays.append(delay)

    monkeypatch.setattr(h.db_module.StateDB, "create_session", create_session)
    monkeypatch.setattr(h.shared, "register_branch_hook", register_hook)
    monkeypatch.setattr(h.shared, "_sleep_before_sqlite_admission_retry", retry_sleep)
    ctx = await h.shared.setup_orchestration_persist(
        env.session, run=env.run, invocation_kind="flow", branches=[env.orc_branch]
    )
    assert ctx is not None, "the injected SQLite busy write must be retried"
    ctx["shared_db_registered"] = False
    h.contexts.append(ctx)
    assert len(attempted) == 2 and len(retry_delays) == 1
    generations = [record.get("cli_worker_generation") for record in attempted]
    assert all(generations), "allocate generation before the SQLite admission retry"
    assert len(set(generations)) == 1, "a retry must not allocate another generation"
    assert str(UUID(generations[0])) == generations[0]
    row = await ctx["db"].get_session(str(env.session.id))
    assert row["status"] == "running" and row["cli_worker_generation"] == generations[0]
    owner = ctx.get("worker_spawn_owner")
    assert owner is not None, "admission must return its worker owner"
    assert hook_owners == [owner], "install the owner before registering branch hooks"
    assert owner.session_id == str(env.session.id)
    assert owner.generation_id == generations[0]


async def test_flow_binds_owner_before_planning_worker_and_reactive_tasks(
    orchestration_workers, monkeypatch
):
    from lionagi.casts.emission import TaskAssignment

    h = orchestration_workers
    env = h.make_env("flow-phases")

    async def setup(**kwargs):
        return env

    async def plan(*args, **kwargs):
        await h.task(h.record_task(env, "planning", 1))
        return [TaskAssignment(task="first", assignee="worker")]

    async def build(env, prompt, plan_result, **kwargs):
        await h.task(h.record_task(env, "worker-construction", 2))
        return h.flow._DagState(
            node_ids=["worker"],
            known_nodes={"worker"},
            deps_by_node={"worker": []},
            reactive=True,
            spawn_roles={"worker"},
            role_base={},
            worker_models=["codex/model"],
        )

    async def execute(env, plan_result, dag, **kwargs):
        assert dag.reactive
        await h.task(h.record_task(env, "worker-task", 3))
        await h.task(h.record_task(env, "reactive-task", 4))
        return h.flow._ExecResult(
            agent_results=[{"agent": "worker", "response": "result"}],
            n_spawned=1,
            t_exec_elapsed=0.0,
        )

    async def synthesize(env, *args, **kwargs):
        await h.task(h.record_task(env, "synthesis", 5))
        return {"model": "codex/model", "response": "integrated", "time_ms": 0.0}

    monkeypatch.setattr(h.flow, "setup_orchestration", setup)
    monkeypatch.setattr(h.flow, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(h.flow, "role_roster", lambda model: "worker")
    monkeypatch.setattr(h.flow, "mode_roster", lambda pack: "")
    monkeypatch.setattr(h.flow, "team_guidance", lambda team: "")
    monkeypatch.setattr(h.flow, "plan", plan)
    monkeypatch.setattr(h.flow, "_build_dag", build)
    monkeypatch.setattr(h.flow, "_execute_dag", execute)
    monkeypatch.setattr(h.flow, "_synthesize", synthesize)
    monkeypatch.setattr(h.flow, "_finalize_flow", lambda *args, **kwargs: "integrated")
    output, _ = await h.run("flow", with_synthesis=True)
    assert output == "integrated"
    assert [phase for _, phase, _ in h.observations] == [
        "planning",
        "worker-construction",
        "worker-task",
        "reactive-task",
        "synthesis",
    ]
    assert len({id(owner) for _, _, owner in h.observations}) == 1
    async with h.db_module.StateDB(h.db_path) as db:
        snapshot = await db.snapshot_cli_worker_spawns(str(env.session.id))
    assert len(snapshot["spawns"]) == 5
    assert {row["generation_id"] for row in snapshot["spawns"]} == {
        snapshot["session"]["cli_worker_generation"]
    }


async def test_fanout_binds_owner_through_worker_and_synthesis_passes(
    orchestration_workers, monkeypatch
):
    from lionagi import Branch
    from lionagi.casts.emission import TaskAssignment
    from lionagi.protocols.types import EventStatus
    from lionagi.session.signal import NodeCompleted
    from lionhub.orchestration.engines import PlanningEngine

    h = orchestration_workers
    env = h.make_env("fanout-phases")
    passes = []

    async def setup(**kwargs):
        return env

    async def plan(*args, **kwargs):
        await h.task(h.record_task(env, "planning", 10))
        return [TaskAssignment(task="first", assignee="worker")]

    async def worker(env, *, explicit_name, **kwargs):
        await h.task(h.record_task(env, "worker-construction", 11))
        branch = Branch(name=explicit_name)
        h.branches.append(branch)
        env.session.include_branches(branch)
        return branch, "codex/model", None, False

    async def synthesis(env, name="synthesis"):
        h.owner_at(env, "synthesis-construction")
        branch = Branch(name=name)
        h.branches.append(branch)
        env.session.include_branches(branch)
        return branch

    async def run_dag(graph, **kwargs):
        nodes = list(graph.internal_nodes.values())
        passes.append([str(node.id) for node in nodes])
        phase = "worker-task" if len(passes) == 1 else "synthesis-task"
        await h.task(h.record_task(env, phase, 11 + len(passes)))
        node = nodes[-1]
        node.execution.status = EventStatus.COMPLETED
        node.execution.response = phase + " result"
        await env.session.emit(NodeCompleted(op_id=str(node.id), name=str(node.id), elapsed=0.01))
        return {"operation_results": {node.id: node.execution.response}}

    engine = SimpleNamespace(run_dag=run_dag)
    monkeypatch.setattr(h.fanout, "setup_orchestration", setup)
    monkeypatch.setattr(h.fanout, "plan", plan)
    monkeypatch.setattr(h.fanout, "available_roles", lambda: ["worker"])
    monkeypatch.setattr(h.fanout, "role_roster", lambda model: "worker")
    monkeypatch.setattr(h.fanout, "mode_roster", lambda pack: "")
    monkeypatch.setattr(h.fanout, "build_worker_branch", worker)
    monkeypatch.setattr(h.fanout, "_fresh_synthesis_branch", synthesis)
    monkeypatch.setattr(PlanningEngine, "new_run", lambda self, **kwargs: engine)
    await h.run("fanout", num_workers=1, with_synthesis=True)
    assert [phase for _, phase, _ in h.observations] == [
        "planning",
        "worker-construction",
        "worker-task",
        "synthesis-construction",
        "synthesis-task",
    ]
    assert len({id(owner) for _, _, owner in h.observations}) == 1
    assert len(passes) == 2 and passes[1][:-1] == passes[0]
    assert (env.run.artifact_root / "worker_1.md").read_text() == "worker-task result"
    assert env.run.synthesis_path.read_text() == "synthesis-task result"
    async with h.db_module.StateDB(h.db_path) as db:
        snapshot = await db.snapshot_cli_worker_spawns(str(env.session.id))
    assert len(snapshot["spawns"]) == 4
    assert {row["generation_id"] for row in snapshot["spawns"]} == {
        snapshot["session"]["cli_worker_generation"]
    }


async def test_flow_reactive_executor_keeps_owner_for_the_spawned_provider(
    orchestration_workers, monkeypatch
):
    from lionagi import Branch
    from lionagi.casts.emission import SpawnRequest, TaskAssignment

    h = orchestration_workers
    env = h.make_env("flow-reactive")
    worker = Branch(name="worker")
    h.branches.append(worker)
    env.session.include_branches(worker)
    node_id = env.builder.add_operation(
        "operate", node_id="initial", branch=worker, instruction="spawn one follow-up"
    )
    spawned_ids = []
    execution = []

    async def setup(**kwargs):
        return env

    async def operate(branch, instruction=None, **kwargs):
        if branch.id == worker.id:
            await h.task(h.record_task(env, "planned-provider", 30))
            return SpawnRequest(
                instruction="persist follow-up", assignee="worker", independent=True
            )
        await h.task(h.record_task(env, "reactive-provider", 31))
        spawned_ids.append(str(branch.id))
        h.branches.append(branch)
        await branch.msgs.a_add_message(assistant_response="durable follow-up")
        return "durable follow-up"

    async def inner(*args, **kwargs):
        h.owner_at(env, "before-reactive-executor")
        plan = h.flow._PlanResult(
            assignments=[TaskAssignment(task="spawn one follow-up", assignee="worker")],
            agent_ids=["worker"],
            dep_indices=[[]],
            pool=[],
            budget_preambles={},
        )
        dag = h.flow._DagState(
            node_ids=[node_id],
            known_nodes={node_id},
            deps_by_node={node_id: []},
            reactive=True,
            spawn_roles=None,
            role_base={"worker": worker},
            worker_models=["codex/model"],
        )
        result = await h.flow._execute_dag(env, plan, dag, max_concurrent=1, max_ops=0)
        execution.append(result)
        return "durable follow-up"

    monkeypatch.setattr(h.flow, "setup_orchestration", setup)
    monkeypatch.setattr(h.flow, "_run_flow_inner", inner)
    monkeypatch.setattr(Branch, "operate", operate)
    await h.run("flow")
    assert execution[0].n_spawned == 1
    assert len(spawned_ids) == 1
    assert [phase for _, phase, _ in h.observations] == [
        "before-reactive-executor",
        "planned-provider",
        "reactive-provider",
    ]
    assert len({id(owner) for _, _, owner in h.observations}) == 1
    async with h.db_module.StateDB(h.db_path) as db:
        snapshot = await db.snapshot_cli_worker_spawns(str(env.session.id))
        spawned = await db.get_branch(spawned_ids[0])
    assert spawned["session_id"] == str(env.session.id)
    assert len(snapshot["spawns"]) == 2
    assert {row["generation_id"] for row in snapshot["spawns"]} == {
        snapshot["session"]["cli_worker_generation"]
    }


async def test_concurrent_flow_and_fanout_keep_distinct_owners(orchestration_workers, monkeypatch):
    h = orchestration_workers
    ready, release = asyncio.Event(), asyncio.Event()
    h.releases.append(release)
    envs = {kind: h.make_env(kind) for kind in ("flow", "fanout")}
    owners = {}

    for kind, env in envs.items():

        async def setup(*, _env=env, **kwargs):
            return _env

        async def inner(*args, env, _kind=kind, **kwargs):
            owners[_kind] = h.owner_at(env, _kind)
            if len(owners) == 2:
                ready.set()
            await release.wait()
            await h.task(h.record_task(env, _kind + "-child", 20 + len(_kind)))
            assert h.get_recorder() is owners[_kind]
            return "work"

        module = getattr(h, kind)
        monkeypatch.setattr(module, "setup_orchestration", setup)
        monkeypatch.setattr(module, f"_run_{kind}_inner", inner)

    calls = [h.task(h.run(kind)) for kind in envs]
    try:
        await _reached(ready, *calls)
        assert owners["flow"] is not owners["fanout"]
        assert owners["flow"].session_id != owners["fanout"].session_id
        assert owners["flow"].generation_id != owners["fanout"].generation_id
        assert h.get_recorder() is None
        release.set()
        await asyncio.wait_for(asyncio.gather(*calls), 5)
    finally:
        release.set()
    async with h.db_module.StateDB(h.db_path) as db:
        snapshots = [
            await db.snapshot_cli_worker_spawns(str(env.session.id)) for env in envs.values()
        ]
    assert all(len(snapshot["spawns"]) == 1 for snapshot in snapshots)
    assert all(
        snapshot["spawns"][0]["session_id"] == snapshot["session"]["id"]
        and snapshot["spawns"][0]["generation_id"] == snapshot["session"]["cli_worker_generation"]
        for snapshot in snapshots
    )


@pytest.mark.parametrize("kind", ["flow", "fanout"])
async def test_unpersisted_orchestration_masks_an_outer_owner(
    orchestration_workers, monkeypatch, kind
):
    h = orchestration_workers
    env = h.make_env("unpersisted-" + kind)
    db = h.db_module.StateDB(h.db_path)
    h.dbs.append(db)
    await db.open()
    outer_id, generation = str(uuid4()), str(uuid4())
    progression_id = str(uuid4())
    await db.create_progression(progression_id)
    await db.create_session(
        {
            "id": outer_id,
            "progression_id": progression_id,
            "status": "running",
            "cli_worker_generation": generation,
        }
    )
    outer = h.workers._WorkerSpawnOwner(db, session_id=outer_id, generation_id=generation)
    observations = []

    async def setup(**kwargs):
        observations.append(h.get_recorder())
        assert h.get_recorder() is None, "a new CLI execution must mask the outer recorder"
        return env

    async def no_persistence(*args, **kwargs):
        return None

    async def inner(*args, **kwargs):
        observations.append(h.get_recorder())
        assert h.get_recorder() is None, "unpersisted work must not borrow the outer owner"
        return "unpersisted"

    module = getattr(h, kind)
    monkeypatch.setattr(module, "setup_orchestration", setup)
    monkeypatch.setattr(module, f"_run_{kind}_inner", inner)
    monkeypatch.setattr(h.shared, "setup_orchestration_persist", no_persistence)
    try:
        with outer.bind():
            output, _ = await h.run(kind)
            assert h.get_recorder() is outer
        assert output == "unpersisted" and observations == [None, None]
        assert h.get_recorder() is None
        snapshot = await db.snapshot_cli_worker_spawns(outer_id)
        assert snapshot["spawns"] == []
        assert snapshot["session"]["status"] == "running"
        assert snapshot["session"]["cli_worker_generation"] == generation
    finally:
        await outer.aclose()


@pytest.mark.parametrize("kind", ["flow", "fanout"])
@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "declared"])
async def test_an_ownerless_persisted_orchestration_runs_only_when_its_setup_says_why(
    orchestration_workers, monkeypatch, kind, declared
):
    """ADR-0104 Amendment 4, Limits: "its setup result says so in `worker_spawns_unrecorded`, and
    a persisted setup result that carries no owner and does not say so is refused"."""
    from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

    h = orchestration_workers
    env = h.make_env(f"ownerless-{kind}-{'declared' if declared else 'undeclared'}")
    recorders = []

    async def setup(**kwargs):
        return env

    async def start(env_, **kwargs):
        env_._live_persist = {"session_id": str(env_.session.id), "worker_spawn_owner": None}
        if declared:
            env_._live_persist["worker_spawns_unrecorded"] = True

    async def stop(env_, *, status, **kwargs):
        return status

    async def inner(*args, **kwargs):
        recorders.append(h.get_recorder())
        return "ran"

    module = getattr(h, kind)
    monkeypatch.setattr(module, "setup_orchestration", setup)
    monkeypatch.setattr(module, "start_live_persist", start)
    monkeypatch.setattr(module, "stop_live_persist", stop)
    monkeypatch.setattr(module, f"_run_{kind}_inner", inner)
    if declared:
        output, _ = await h.run(kind)
        assert output == "ran"
        assert recorders == [None], "an unrecorded run's workers launch with no recorder"
    else:
        with pytest.raises(
            _WorkerSpawnAdmissionError, match="persisted orchestration has no worker owner"
        ):
            await h.run(kind)
        assert recorders == [], "the refusal comes before any work"


@pytest.mark.parametrize("kind", ["flow", "fanout"])
async def test_orchestration_drains_registration_before_terminal_and_close(
    orchestration_workers, monkeypatch, kind
):
    h = orchestration_workers
    env = h.make_env("drain-" + kind)
    registration, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    h.releases.append(release)
    observed = []
    ctxs = []
    real_record = h.db_module.StateDB.register_cli_worker_spawn
    real_update = h.db_module.StateDB.update_status
    real_close = h.db_module.StateDB.close
    real_owner_close = h.workers._WorkerSpawnOwner.aclose

    async def register(db, record):
        registration.set()
        await release.wait()
        await real_record(db, record)
        observed.append("registered")

    async def update(db, entity_type, entity_id, **kwargs):
        if ctxs and db is ctxs[0]["db"] and entity_type == "session":
            if kwargs["new_status"] != "running":
                assert release.is_set(), "terminal write must wait for worker registration"
                observed.append("terminal")
        return await real_update(db, entity_type, entity_id, **kwargs)

    async def close(db):
        if ctxs and db is ctxs[0]["db"]:
            assert release.is_set(), "DB close must wait for worker registration"
            observed.append("close")
        return await real_close(db)

    async def owner_close(owner):
        if ctxs and owner is ctxs[0].get("worker_spawn_owner"):
            closing.set()
        await real_owner_close(owner)

    async def setup(**kwargs):
        return env

    async def inner(*args, env, **kwargs):
        h.owner_at(env, "before-teardown")
        ctxs.append(env._live_persist)
        call = h.task(h.record_task(env, "held-registration", 40))
        await _reached(registration, call)
        return "work"

    module = getattr(h, kind)
    monkeypatch.setattr(module, "setup_orchestration", setup)
    monkeypatch.setattr(module, f"_run_{kind}_inner", inner)
    monkeypatch.setattr(h.db_module.StateDB, "register_cli_worker_spawn", register)
    monkeypatch.setattr(h.db_module.StateDB, "update_status", update)
    monkeypatch.setattr(h.db_module.StateDB, "close", close)
    monkeypatch.setattr(h.workers._WorkerSpawnOwner, "aclose", owner_close)
    call = h.task(h.run(kind))
    try:
        await _reached(closing, call)
        assert registration.is_set() and not release.is_set()
        assert not call.done() and observed == []
        row = await ctxs[0]["db"].get_session(str(env.session.id))
        assert row["status"] == "running"
        release.set()
        await asyncio.wait_for(call, 5)
    finally:
        release.set()
    assert observed[:3] == ["registered", "terminal", "close"]
    async with h.db_module.StateDB(h.db_path) as db:
        snapshot = await db.snapshot_cli_worker_spawns(str(env.session.id))
    assert len(snapshot["spawns"]) == 1
    assert snapshot["session"]["status"] in h.db_module.SESSION_TERMINAL_STATUSES
    assert h.get_recorder() is None


@pytest.mark.parametrize(
    ("kind", "terminal_outcome"),
    [
        ("flow", "committed"),
        ("flow", "store-refused"),
        ("flow", "compatibility-refused"),
        ("fanout", "committed"),
        ("fanout", "store-refused"),
        ("fanout", "compatibility-refused"),
    ],
    ids=[
        "flow-committed",
        "flow-store-refused",
        "flow-compatibility-refused",
        "fanout-committed",
        "fanout-store-refused",
        "fanout-compatibility-refused",
    ],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_native_cancellation_during_owner_drain_preserves_orchestration_outcome(
    orchestration_workers, monkeypatch, kind, terminal_outcome
):
    from sqlalchemy.exc import OperationalError

    from lionagi.state.compat import StateCompatibilityRefused
    from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS

    h = orchestration_workers
    env = h.make_env("late-cancel-" + kind + "-" + terminal_outcome)
    registration, drain_started = asyncio.Event(), asyncio.Event()
    cancelled_in_wait, release = asyncio.Event(), asyncio.Event()
    h.releases.append(release)
    contexts, retained, closed, terminal, observed_cancel = [], [], [], [], []
    events, terminal_attempts = [], []
    real_record = h.db_module.StateDB.register_cli_worker_spawn
    real_status = h.db_module.StateDB.update_status
    real_close = h.db_module.StateDB.close
    refusal = (
        OperationalError("synthetic terminal write", None, RuntimeError("synthetic store refusal"))
        if terminal_outcome == "store-refused"
        else StateCompatibilityRefused("update_status", "synthetic-profile", "synthetic refusal")
        if terminal_outcome == "compatibility-refused"
        else None
    )
    callback_key = "orchestration-cancel-" + str(uuid4())

    def on_terminal(event):
        if event.entity.id == str(env.session.id):
            terminal.append(event)

    DEFAULT_TERMINAL_CALLBACKS.register(callback_key, on_terminal, kinds=["session"])

    async def setup(**kwargs):
        return env

    async def register(db, record):
        if contexts and db is contexts[0]["db"]:
            registration.set()
            await release.wait()
        await real_record(db, record)
        events.append("registered")

    async def update_status(db, entity_type, entity_id, **kwargs):
        if (
            contexts
            and db is contexts[0]["db"]
            and entity_type == "session"
            and entity_id == contexts[0]["session_id"]
            and kwargs.get("new_status") != "running"
        ):
            assert release.is_set(), "terminal work must wait for the held registration"
            assert all(task.done() for task in retained), "terminal work must follow owner drain"
            terminal_attempts.append(kwargs["new_status"])
            events.append("terminal-attempt")
            if refusal is not None:
                raise refusal
        return await real_status(db, entity_type, entity_id, **kwargs)

    async def close(db):
        bound = bool(contexts) and db is contexts[0]["db"]
        if bound:
            assert release.is_set(), "bound DB close must wait for the held registration"
            assert all(task.done() for task in retained), "bound DB close must follow owner drain"
        await real_close(db)
        if bound:
            closed.append(db)
            events.append("closed")

    async def inner(*args, env, **kwargs):
        owned = h.owner_at(env, "before-late-cancel-drain")
        contexts.append(env._live_persist)
        original_wait = owned._drained.wait

        async def observed_wait():
            drain_started.set()
            try:
                return await original_wait()
            except asyncio.CancelledError as exc:
                observed_cancel.append(exc)
                cancelled_in_wait.set()
                raise

        monkeypatch.setattr(owned._drained, "wait", observed_wait)
        child = h.task(h.record_task(env, "held-late-registration", 50))
        retained.append(child)
        await _reached(registration, child)
        return "work"

    module = getattr(h, kind)
    monkeypatch.setattr(module, "setup_orchestration", setup)
    monkeypatch.setattr(module, f"_run_{kind}_inner", inner)
    monkeypatch.setattr(h.db_module.StateDB, "register_cli_worker_spawn", register)
    monkeypatch.setattr(h.db_module.StateDB, "update_status", update_status)
    monkeypatch.setattr(h.db_module.StateDB, "close", close)
    call = h.task(h.run(kind))
    try:
        await _reached(drain_started, call)
        assert registration.is_set() and len(retained) == 1 and not retained[0].done()
        assert not release.is_set() and not call.done()
        assert terminal_attempts == [] and closed == [] and terminal == [] and events == []
        call.cancel()
        await _reached(cancelled_in_wait, call)
        assert not call.done() and not retained[0].done()
        assert terminal_attempts == [] and closed == [] and terminal == [] and events == []
        ctx = contexts[0]
        assert ctx["worker_spawn_db_closed"] is None
        assert (await ctx["db"].get_session(ctx["session_id"]))["status"] == "running"
        assert env.run.read_manifest()["status"] == "running"
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(call, 5)
        assert call.cancelled() and len(observed_cancel) == 1
        assert raised.value is observed_cancel[0] or raised.value.__context__ is observed_cancel[0]
        assert retained[0].done() and not retained[0].cancelled() and retained[0].result() is None
        assert closed == [ctx["db"]] and ctx["worker_spawn_db_closed"] is ctx["db"]
        assert terminal_attempts == ["cancelled"], "teardown must make one final-status attempt"
        assert events == ["registered", "terminal-attempt", "closed"]
        assert env._live_persist is None and h.get_recorder() is None
        async with h.db_module.StateDB(h.db_path) as check:
            snapshot = await check.snapshot_cli_worker_spawns(ctx["session_id"])
            row = await check.get_session(ctx["session_id"])
        assert len(snapshot["spawns"]) == 1
        assert snapshot["spawns"][0]["generation_id"] == ctx["worker_spawn_owner"].generation_id
        assert snapshot["spawns"][0]["run_id"] == env.run.run_id
        assert snapshot["spawns"][0]["confirmed_empty_at"] is None
        manifest = env.run.read_manifest()
        assert manifest["status"] == ctx["worker_spawn_teardown_status"]
        if terminal_outcome == "store-refused":
            assert ctx["teardown_store_error"] is refusal
            assert row["status"] == manifest["status"] == "running"
            assert row["ended_at"] is None and manifest["ended_at"] is None
            assert manifest["terminal_write_lost"]["status"] == "cancelled"
            assert manifest["terminal_write_lost"]["reason_code"]
            assert terminal == []
        elif terminal_outcome == "compatibility-refused":
            assert ctx["store_refusal"] is refusal
            assert ctx["terminal_reason"][0] == "failed"
            assert "The store refused a write" in ctx["terminal_reason"][1]
            assert row["status"] == "running" and row["ended_at"] is None
            assert manifest["status"] == "failed"
            assert "terminal_write_lost" not in manifest and terminal == []
        else:
            assert row["status"] == manifest["status"] == "cancelled"
            assert row["ended_at"] is not None and manifest["ended_at"] is not None
            assert "terminal_write_lost" not in manifest
            assert len(terminal) == 1 and terminal[0].terminal_status == "cancelled"
    finally:
        release.set()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, *retained, return_exceptions=True)
        DEFAULT_TERMINAL_CALLBACKS.unregister(callback_key)

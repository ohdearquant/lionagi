# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine(agent_env=...) and make_agent(env=...): the child environment overlay.

A per-agent value cannot ride ``os.environ`` when one process runs several
agents at once, since the environment is process-global. These assert the
resolution order and that the value reaches the endpoint the child is spawned
from.
"""

import os

import pytest

from lionagi.engines import Engine
from lionagi.providers._cli_subprocess import reveal_redacted

pytestmark = pytest.mark.asyncio

_CLI_MODEL = "codex/gpt-5.5"


def _child_env(branch):
    return reveal_redacted(branch.chat_model.endpoint._runtime_state["env"])


async def test_engine_wide_agent_env_reaches_the_child():
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert _child_env(branch)["LEG_ID"] == "leg-7"


async def test_per_call_env_outranks_the_engine_default():
    """Same order as cwd, extra_prompt and mcp_config_path: explicit call first."""
    run = Engine(agent_env={"LEG_ID": "engine-wide"}).new_run()
    branch = await run.make_agent(
        "researcher", name="r1", model=_CLI_MODEL, env={"LEG_ID": "per-call"}
    )
    assert _child_env(branch)["LEG_ID"] == "per-call"


async def test_two_agents_in_one_run_get_different_environments():
    run = Engine().new_run()
    a = await run.make_agent("researcher", name="a", model=_CLI_MODEL, env={"LEG_ID": "a"})
    b = await run.make_agent("researcher", name="b", model=_CLI_MODEL, env={"LEG_ID": "b"})
    assert _child_env(a)["LEG_ID"] == "a"
    assert _child_env(b)["LEG_ID"] == "b"


async def test_no_agent_env_leaves_the_child_inheriting():
    """Control: without the overlay nothing is placed."""
    run = Engine().new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert "env" not in branch.chat_model.endpoint._runtime_state


async def test_an_engine_agent_env_that_is_not_a_mapping_raises():
    from lionagi._errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="mapping"):
        Engine(agent_env=[("LEG_ID", "leg-7")])


async def test_engine_agent_env_is_copied_not_aliased():
    """A caller's mapping changed after construction does not change later agents."""
    supplied = {"LEG_ID": "leg-7"}
    run = Engine(agent_env=supplied).new_run()
    supplied["LEG_ID"] = "changed-after-construction"
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert _child_env(branch)["LEG_ID"] == "leg-7"


async def test_the_overlay_keeps_the_inherited_environment():
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert _child_env(branch)["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize(
    "settings",
    [
        {"agent_env": {"LEG_ID": "leg-7"}},
        {"contain_account_integrations": True},
        {"agent_env": {"LEG_ID": "leg-7"}, "contain_account_integrations": True},
    ],
    ids=["agent-env", "containment", "both"],
)
async def test_planning_engine_refuses_settings_its_workers_drop(settings):
    """Its workers are built by spawn_roles from the role alone and would get neither."""
    from lionagi._errors import ConfigurationError
    from lionagi.engines import PlanningEngine

    with pytest.raises(ConfigurationError, match="PlanningEngine does not support") as exc:
        PlanningEngine(**settings)
    for name in settings:
        assert name in str(exc.value)
    assert "leg-7" not in str(exc.value)


async def test_planning_engine_refuses_a_setting_added_after_construction(monkeypatch):
    """The check runs again before planning, so no model call is made."""
    from lionagi._errors import ConfigurationError
    from lionagi.engines import PlanningEngine

    async def fail_plan(self, run, prompt, max_ops):
        raise AssertionError("planned despite the refused setting")

    monkeypatch.setattr(PlanningEngine, "_plan", fail_plan)
    engine = PlanningEngine()
    engine.contain_account_integrations = True
    with pytest.raises(ConfigurationError, match="contain_account_integrations"):
        await engine.run("do it")


async def test_planning_engine_without_either_setting_builds():
    from lionagi.engines import PlanningEngine

    engine = PlanningEngine()
    assert engine.agent_env is None
    assert engine.contain_account_integrations is False


_SETTINGS = pytest.mark.parametrize(
    "settings",
    [
        {"agent_env": {"LEG_ID": "leg-7"}},
        {"contain_account_integrations": True},
    ],
    ids=["agent-env", "containment"],
)


def _record_flow(monkeypatch):
    from lionagi.session.session import Session

    calls: list[dict] = []

    async def flow(self, graph, **kwargs):
        calls.append(kwargs)
        return {"operation_results": {}}

    monkeypatch.setattr(Session, "flow", flow)
    return calls


@_SETTINGS
async def test_run_dag_refuses_reactive_spawning_with_an_agent_setting(settings, monkeypatch):
    """A spawned node may be placed on the session's default branch, which make_agent did not build."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=branch, instruction="do it")
    with pytest.raises(ConfigurationError, match=r"reactive=True") as exc:
        await run.run_dag(builder.get_graph(), reactive=True)
    assert next(iter(settings)) in str(exc.value)
    assert "leg-7" not in str(exc.value)
    assert calls == []


@_SETTINGS
async def test_run_dag_refuses_a_node_on_a_branch_make_agent_did_not_build(settings, monkeypatch):
    """A node with no branch runs on the session's default branch; a caller's own branch carries nothing."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    own = Branch(name="own")
    run.session.include_branches(own)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a")
    builder.add_operation("operate", instruction="b")
    builder.add_operation("operate", branch=own, instruction="c")
    with pytest.raises(ConfigurationError, match=r"2 of 3 nodes") as exc:
        await run.run_dag(builder.get_graph())
    assert "leg-7" not in str(exc.value)
    assert calls == []


@_SETTINGS
async def test_run_dag_runs_nodes_on_branches_make_agent_built(settings, monkeypatch):
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    a = await run.make_agent("researcher", name="a", model=_CLI_MODEL)
    b = await run.make_agent("researcher", name="b", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    first = builder.add_operation("operate", branch=a, instruction="a")
    builder.add_operation("operate", branch=b, depends_on=[first], instruction="b")
    await run.run_dag(builder.get_graph())
    assert len(calls) == 1


async def test_run_dag_without_either_setting_runs_a_node_with_no_branch(monkeypatch):
    """Control: the check applies only when a setting is on."""
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", instruction="a")
    await Engine().new_run().run_dag(builder.get_graph(), reactive=True)
    assert len(calls) == 1


async def test_the_judge_raises_when_it_cannot_carry_the_overlay():
    """An API judge cannot take the overlay; that is not a pass."""
    from lionagi._errors import ConfigurationError

    engine = Engine(agent_env={"LEG_ID": "leg-7"}, judge_model="openai/gpt-4.1")
    run = engine.new_run()
    with pytest.raises(ConfigurationError, match="agent_env"):
        await engine.judge(run, "e1", "a subject")


_CHANGES = pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"agent_env": {"LEG_ID": "leg-7"}}, {"agent_env": {"LEG_ID": "leg-8"}}),
        ({}, {"agent_env": {"LEG_ID": "leg-7"}}),
        ({}, {"contain_account_integrations": True}),
        ({"agent_env": {"LEG_ID": "leg-7"}}, {"contain_account_integrations": True}),
    ],
    ids=["overlay-replaced", "overlay-added", "containment-added", "containment-added-to-overlay"],
)


@_CHANGES
async def test_run_dag_refuses_a_branch_built_before_a_setting_changed(before, after, monkeypatch):
    """A branch keeps what it was built with; a setting changed afterwards does not reach it."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    engine = Engine(**before)
    run = engine.new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    for name, value in after.items():
        setattr(engine, name, value)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=branch, instruction="a")
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes") as exc:
        await run.run_dag(builder.get_graph())
    assert "leg-7" not in str(exc.value) and "leg-8" not in str(exc.value)
    assert calls == []


async def test_run_dag_refuses_an_overlay_changed_in_place_after_the_branch_was_built(
    monkeypatch,
):
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    engine = Engine(agent_env={"LEG_ID": "leg-7"})
    run = engine.new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    engine.agent_env["LEG_ID"] = "leg-8"
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=branch, instruction="a")
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
async def test_run_dag_refuses_a_node_whose_branch_left_the_session(settings, monkeypatch):
    """The flow runs a node whose branch it cannot find on the default branch."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    kept = await run.make_agent("researcher", name="kept", model=_CLI_MODEL)
    removed = await run.make_agent("researcher", name="removed", model=_CLI_MODEL)
    run.session.remove_branch(removed)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=kept, instruction="a")
    builder.add_operation("operate", branch=removed, instruction="b")
    with pytest.raises(ConfigurationError, match=r"1 of 2 nodes"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
async def test_run_dag_runs_each_node_on_the_branch_it_names(settings, monkeypatch):
    """Through the real flow: each node runs on the built branch it names, not the
    session's default branch or a clone of it."""
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch

    ran: list[str] = []

    async def operate(self, **kwargs):
        ran.append(str(self.id))
        return "done"

    monkeypatch.setattr(Branch, "operate", operate)
    run = Engine(**settings).new_run()
    a = await run.make_agent("researcher", name="a", model=_CLI_MODEL)
    b = await run.make_agent("researcher", name="b", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    first = builder.add_operation("operate", branch=a, instruction="a")
    builder.add_operation("operate", branch=b, depends_on=[first], instruction="b")
    await run.run_dag(builder.get_graph())
    assert ran == [str(a.id), str(b.id)]
    assert str(run.session.default_branch.id) not in ran


async def _uncontained_model():
    """A codex model built with neither setting, to put on a built branch."""
    other = await Engine().new_run().make_agent("researcher", name="other", model=_CLI_MODEL)
    return other.chat_model


@_SETTINGS
async def test_run_dag_refuses_a_branch_whose_model_was_replaced(settings, monkeypatch):
    """The settings ride the model make_agent built; another model on the branch carries none."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    branch.chat_model = await _uncontained_model()
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=branch, instruction="a")
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes name no branch") as exc:
        await run.run_dag(builder.get_graph())
    assert "leg-7" not in str(exc.value)
    assert calls == []


@_SETTINGS
async def test_run_dag_refuses_another_branch_included_under_a_built_id(settings, monkeypatch):
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    run.session.remove_branch(built)
    run.session.include_branches(Branch(id=built.id, name="stand-in"))
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a")
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes name no branch"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
@pytest.mark.parametrize("key", ["chat_model", "imodel"])
async def test_run_dag_refuses_a_node_that_puts_another_model_on_its_branch(
    settings, key, monkeypatch
):
    """operate(chat_model=...) and run(imodel=...) leave the model on the branch for later nodes."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    other = await _uncontained_model()
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", **{key: other})
    builder.add_operation("operate", branch=built, instruction="b")
    with pytest.raises(ConfigurationError, match=r"1 of 2 nodes set chat_model or imodel"):
        await run.run_dag(builder.get_graph())
    assert calls == []


async def test_a_setting_changed_while_make_agent_builds_is_not_recorded_as_the_builds(
    monkeypatch,
):
    """make_agent reads the settings once, before it awaits the build, and records that reading."""
    import lionagi.engines.engine as engine_module
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    engine = Engine()
    run = engine.new_run()
    real_create_agent = engine_module.create_agent

    async def create_agent_then_contain(spec, **kwargs):
        engine.contain_account_integrations = True
        return await real_create_agent(spec, **kwargs)

    monkeypatch.setattr(engine_module, "create_agent", create_agent_then_contain)
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert engine.contain_account_integrations is True
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=branch, instruction="a")
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes name no branch"):
        await run.run_dag(builder.get_graph())
    assert calls == []


async def test_the_judge_fails_open_on_any_other_configuration_error(monkeypatch):
    """Only a judge that cannot carry the engine's settings is raised; every other
    error, a ConfigurationError included, reads as a pass, as it always has."""
    from lionagi._errors import ConfigurationError

    engine = Engine(agent_env={"LEG_ID": "leg-7"}, judge_model=_CLI_MODEL)
    run = engine.new_run()
    events: list[tuple] = []
    monkeypatch.setattr(run, "notify", lambda kind, **kw: events.append((kind, kw)))

    async def make_agent(*args, **kwargs):
        raise ConfigurationError("an mcp config this judge cannot use")

    monkeypatch.setattr(run, "make_agent", make_agent)
    assert await engine.judge(run, "e1", "a subject") is True
    assert events == [
        ("judge_error", {"eid": "e1", "error": "an mcp config this judge cannot use"})
    ]

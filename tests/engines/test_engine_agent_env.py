# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine(agent_env=...) and make_agent(env=...): the child environment overlay.

A per-agent value cannot ride ``os.environ`` when one process runs several
agents at once, since the environment is process-global. These assert the
resolution order and that the value reaches the endpoint the child is spawned
from.
"""

import os
import re
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from lionagi.providers._cli_subprocess import reveal_redacted
from lionhub.orchestration.engines import Engine

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
async def test_planning_engine_refuses_unverified_worker_clone_settings(settings):
    """Managed-DAG clones still require proven environment/containment provenance."""
    from lionagi._errors import ConfigurationError
    from lionhub.orchestration.engines import PlanningEngine

    with pytest.raises(ConfigurationError, match="PlanningEngine does not support") as exc:
        PlanningEngine(**settings)
    for name in settings:
        assert name in str(exc.value)
    assert "worker clones" in str(exc.value)
    assert "provenance" in str(exc.value)
    assert "leg-7" not in str(exc.value)


async def test_planning_engine_refuses_a_setting_added_after_construction(monkeypatch):
    """The check runs again before planning, so no model call is made."""
    from lionagi._errors import ConfigurationError
    from lionhub.orchestration.engines import PlanningEngine

    async def fail_plan(self, run, prompt, max_ops):
        raise AssertionError("planned despite the refused setting")

    monkeypatch.setattr(PlanningEngine, "_plan", fail_plan)
    engine = PlanningEngine()
    engine.contain_account_integrations = True
    with pytest.raises(ConfigurationError, match="contain_account_integrations"):
        await engine.run("do it")


async def test_planning_engine_without_either_setting_builds():
    from lionhub.orchestration.engines import PlanningEngine

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


def _model_keywords():
    """Every keyword a branch operation takes a model under, read off the signatures: an
    annotation naming iModel, or an unannotated model slot, as ``Branch.interpret``'s
    ``interpret_model`` is."""
    import inspect
    from typing import get_args

    from lionagi.operations.node import BranchOperations
    from lionagi.session.branch import Branch

    keys = set()
    for name in get_args(BranchOperations):
        for param in inspect.signature(getattr(Branch, name)).parameters.values():
            unannotated = param.annotation is inspect.Parameter.empty
            if "iModel" in str(param.annotation) or (
                unannotated and (param.name == "imodel" or param.name.endswith("_model"))
            ):
                keys.add(param.name)
    return sorted(keys)


@_SETTINGS
@pytest.mark.parametrize("key", _model_keywords())
@pytest.mark.parametrize("given_as", ["model", "spec"])
async def test_run_dag_refuses_a_node_that_passes_a_model(settings, key, given_as, monkeypatch):
    """The node's calls run on the model, and run() leaves the model it is given on the branch
    for later nodes. The keyword is refused whatever it holds but None, a spec string too."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    other = await _uncontained_model() if given_as == "model" else _CLI_MODEL
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", **{key: other})
    builder.add_operation("operate", branch=built, instruction="b")
    with pytest.raises(ConfigurationError, match=r"1 of 2 nodes pass a model"):
        await run.run_dag(builder.get_graph())
    assert calls == []


async def test_the_model_keywords_read_off_the_signatures_are_the_known_five():
    """Control for the table above: an empty or partial reading would test nothing."""
    assert _model_keywords() == [
        "analysis_model",
        "chat_model",
        "imodel",
        "interpret_model",
        "parse_model",
    ]


class _Holder(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    held: Any = None


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


@_SETTINGS
@pytest.mark.parametrize(
    "value",
    [
        lambda m: m,
        lambda m: {"nested": [m]},
        lambda m: (m,),
        lambda m: frozenset([m]),
        lambda m: _Holder(held=[m]),
        lambda m: _Open(held=m),
    ],
    ids=[
        "bare",
        "in-a-mapping",
        "in-a-tuple",
        "in-a-frozenset",
        "in-a-pydantic-field",
        "in-a-pydantic-extra",
    ],
)
async def test_run_dag_refuses_a_model_under_a_keyword_the_operation_does_not_name(
    settings, value, monkeypatch
):
    """An operation hands keywords it does not name on to the model call."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    other = await _uncontained_model()
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", helper=value(other))
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes pass a model"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
async def test_run_dag_refuses_a_branch_whose_parse_model_was_replaced(settings, monkeypatch):
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    branch.parse_model = await _uncontained_model()
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=branch, instruction="a")
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes name no branch"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
async def test_run_dag_refuses_an_operation_that_is_not_a_branch_operation(settings, monkeypatch):
    """A name the branch resolves to another method, connect here, builds its own model."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("connect", branch=built, provider="openai", name="side")
    builder.add_operation("operate", branch=built, instruction="b")
    with pytest.raises(ConfigurationError, match=r"1 of 2 nodes name an operation"):
        await run.run_dag(builder.get_graph())
    assert calls == []


async def test_run_dag_with_agent_env_refuses_a_node_that_passes_env(monkeypatch):
    """The node's env replaces the overlay for its calls rather than adding to it."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", env={"OTHER": "1"})
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes pass env") as exc:
        await run.run_dag(builder.get_graph())
    assert "leg-7" not in str(exc.value)
    assert calls == []


@pytest.mark.parametrize(
    "keywords",
    [
        {"interpret_kwargs": {"env": {"ONLY": "node"}}},
        {"response_kwargs": {"imodel_kw": {"env": {"ONLY": "node"}}}},
        {"imodel_kw": {"env": {"ONLY": "node"}}},
    ],
    ids=["interpret_kwargs", "response_kwargs-imodel_kw", "imodel_kw"],
)
async def test_run_dag_with_agent_env_refuses_env_handed_on_to_a_model_call(keywords, monkeypatch):
    """ReAct hands interpret_kwargs, and response_kwargs' imodel_kw, to its model calls."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("ReAct", branch=built, instruct={"instruction": "a"}, **keywords)
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes pass env"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
@pytest.mark.parametrize(
    "keywords",
    [
        {"api_call": "prebuilt"},
        {"imodel_kw": {"api_call": "prebuilt"}},
        {"interpret_kwargs": {"api_call": "prebuilt"}},
        {"response_kwargs": {"api_call": "prebuilt"}},
        {"response_kwargs": {"imodel_kw": {"api_call": "prebuilt"}}},
        {"create_event_type": "own-event"},
        {"imodel_kw": {"create_event_type": "own-event"}},
        {"interpret_kwargs": {"create_event_type": "own-event"}},
    ],
    ids=[
        "own",
        "imodel_kw",
        "interpret_kwargs",
        "response_kwargs",
        "response_kwargs-imodel_kw",
        "event-type-own",
        "event-type-imodel_kw",
        "event-type-interpret_kwargs",
    ],
)
async def test_run_dag_refuses_a_node_that_passes_a_prebuilt_api_call(
    settings, keywords, monkeypatch
):
    """A model sends a prebuilt call as it was built, and an event type of the caller's builds
    its own request, both without the settings."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.service.connections.api_calling import APICalling

    class OwnEvent(APICalling):
        pass

    def resolved(value):
        if isinstance(value, dict):
            return {k: resolved(v) for k, v in value.items()}
        return OwnEvent if value == "own-event" else value

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation(
        "ReAct", branch=built, instruct={"instruction": "a"}, **resolved(keywords)
    )
    with pytest.raises(
        ConfigurationError, match=r"1 of 1 nodes pass api_call or create_event_type"
    ):
        await run.run_dag(builder.get_graph())
    assert calls == []


@_SETTINGS
@pytest.mark.parametrize(
    "keywords",
    [
        {"api_call": None},
        {"imodel_kw": {"api_call": None}},
        {"imodel_kw": {"create_event_type": "default-event"}},
    ],
    ids=["api_call-none", "imodel_kw-api_call-none", "imodel_kw-default-event-type"],
)
async def test_run_dag_runs_a_node_that_passes_the_default_call_or_event_type(
    settings, keywords, monkeypatch
):
    """Control: None for api_call builds the call, and the default event type is the one the
    model builds its request with."""
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.service.connections.api_calling import APICalling

    def resolved(value):
        if isinstance(value, dict):
            return {k: resolved(v) for k, v in value.items()}
        return APICalling if value == "default-event" else value

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation(
        "ReAct", branch=built, instruct={"instruction": "a"}, **resolved(keywords)
    )
    await run.run_dag(builder.get_graph())
    assert len(calls) == 1


async def test_run_dag_with_agent_env_runs_a_node_whose_context_names_env(monkeypatch):
    """Control: env in data the node carries, not in a model call's keywords, is not refused."""
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", context={"env": "staging"})
    await run.run_dag(builder.get_graph())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "keywords",
    [
        {"contain_account_integrations": False},
        {"contain_account_integrations": None},
        {"account_connectors": ["github"]},
        {"interpret_kwargs": {"account_connectors": ["github"]}},
        {"response_kwargs": {"imodel_kw": {"contain_account_integrations": False}}},
    ],
    ids=["false", "none", "connectors", "interpret_kwargs", "response_kwargs-imodel_kw"],
)
async def test_run_dag_with_containment_refuses_a_node_that_lifts_it(keywords, monkeypatch):
    """Either keyword lifts the containment for the call it reaches."""
    from lionagi._errors import ConfigurationError
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(contain_account_integrations=True).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("ReAct", branch=built, instruct={"instruction": "a"}, **keywords)
    with pytest.raises(ConfigurationError, match=r"1 of 1 nodes pass contain_account_integrations"):
        await run.run_dag(builder.get_graph())
    assert calls == []


@pytest.mark.parametrize(
    "keywords",
    [{"contain_account_integrations": True}, {"account_connectors": []}],
    ids=["true", "no-connectors"],
)
async def test_run_dag_with_containment_runs_a_node_that_keeps_it(keywords, monkeypatch):
    """Control: a node that restates the containment, or names no connector, runs."""
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(contain_account_integrations=True).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", **keywords)
    await run.run_dag(builder.get_graph())
    assert len(calls) == 1


@_SETTINGS
async def test_run_dag_runs_a_node_that_passes_none_for_a_model(settings, monkeypatch):
    """None in a model slot leaves the branch's own model in place."""
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(**settings).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", chat_model=None)
    await run.run_dag(builder.get_graph())
    assert len(calls) == 1


async def test_run_dag_with_containment_alone_runs_a_node_that_passes_env(monkeypatch):
    """Control: env is refused only where an overlay is in force."""
    from lionagi.operations.builder import OperationGraphBuilder

    calls = _record_flow(monkeypatch)
    run = Engine(contain_account_integrations=True).new_run()
    built = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    builder = OperationGraphBuilder()
    builder.add_operation("operate", branch=built, instruction="a", env={"OTHER": "1"})
    await run.run_dag(builder.get_graph())
    assert len(calls) == 1


async def test_a_setting_changed_while_make_agent_builds_is_not_recorded_as_the_builds(
    monkeypatch,
):
    """make_agent reads the settings once, before it awaits the build, and records that reading."""
    import lionhub.orchestration.engines.engine as engine_module
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


@pytest.mark.parametrize(
    ("config", "refusal"),
    [
        ('profile = "work"\n', "profile = 'work'"),
        ("profile = \n", "cannot read the codex config"),
    ],
    ids=["config-selects-a-profile", "config-unreadable"],
)
async def test_the_judge_raises_when_codex_refuses_its_containment(
    config, refusal, tmp_path, monkeypatch
):
    """Codex refuses a contained call whose config could select a profile, before it
    runs and on every call; no judge ran, so the refusal is raised, never read as a pass.
    No codex binary is needed: the refusal comes before any process would start."""
    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import ContainmentRefusedError

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(config)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex_mod, "CODEX_CLI", "/nonexistent/codex-cli")
    started = _record_cli_spawns(monkeypatch)
    engine = Engine(
        contain_account_integrations=True, judge_model=_CLI_MODEL, agent_cwd=str(tmp_path)
    )
    run = engine.new_run()
    events: list[tuple] = []
    monkeypatch.setattr(run, "notify", lambda kind, **kw: events.append((kind, kw)))
    with pytest.raises(ContainmentRefusedError, match=re.escape(refusal)):
        await engine.judge(run, "e1", "a subject")
    assert started == []
    assert events == []


def _record_cli_spawns(monkeypatch):
    """Fail the spawn seam if any CLI process is started."""
    import lionagi.providers._cli_subprocess as cli_sub

    started: list[tuple] = []

    async def _spawn(*args, **kwargs):
        started.append((args, kwargs))
        raise AssertionError("no CLI process may start for this judge")

    monkeypatch.setattr(cli_sub.asyncio, "create_subprocess_exec", _spawn)
    return started


async def test_the_judge_refuses_over_an_ambient_config_nested_too_deep_to_parse(
    tmp_path, monkeypatch
):
    """A CODEX_HOME config nested past the parser's limit must not read as a pass.

    The launch-time selection check cannot parse the config and refuses. The
    judge raises the refusal, and no CLI process is ever started."""
    import subprocess

    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import _ExclusiveMcpSelectionError

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("x = " + "[" * 3000 + "]" * 3000 + "\n")
    monkeypatch.setenv("CODEX_HOME", str(home))
    fake_cli = tmp_path / "codex"
    fake_cli.write_text("fake executable, never run\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(codex_mod, "CODEX_CLI", str(fake_cli))

    calls: list[tuple] = []

    def _run(argv, **kwargs):
        if argv[0] == str(fake_cli):
            calls.append((argv, kwargs))
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("subprocess.run", _run)
    started = _record_cli_spawns(monkeypatch)

    engine = Engine(
        contain_account_integrations=True, judge_model=_CLI_MODEL, agent_cwd=str(tmp_path)
    )
    run = engine.new_run()
    real = run.make_agent

    async def make_agent(*args, **kwargs):
        return await real(*args, mcp_servers=["ambient"], **kwargs)

    monkeypatch.setattr(run, "make_agent", make_agent)

    with pytest.raises(_ExclusiveMcpSelectionError, match="cannot be read or parsed"):
        await engine.judge(run, "e1", "a subject")
    assert calls == []
    assert started == []


async def test_the_judge_refuses_a_working_directory_that_is_a_symlink_to_itself(
    tmp_path, monkeypatch
):
    """The containment walk cannot resolve such a cwd; a judge that could not read the
    configs of its working directory has no verdict to give, so the refusal is raised."""
    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import ContainmentRefusedError

    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setattr(codex_mod, "CODEX_CLI", "/nonexistent/codex-cli")
    started = _record_cli_spawns(monkeypatch)

    engine = Engine(contain_account_integrations=True, judge_model=_CLI_MODEL, agent_cwd=str(loop))
    run = engine.new_run()
    events: list[tuple] = []
    monkeypatch.setattr(run, "notify", lambda kind, **kw: events.append((kind, kw)))

    with pytest.raises(ContainmentRefusedError, match=re.escape(str(loop))):
        await engine.judge(run, "e1", "a subject")
    assert started == []
    assert events == []


async def test_the_judge_refuses_a_working_directory_that_does_not_exist(tmp_path, monkeypatch):
    """Same as above for a cwd that is absent: the refusal names the directory."""
    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import ContainmentRefusedError

    missing = tmp_path / "missing"
    monkeypatch.setattr(codex_mod, "CODEX_CLI", "/nonexistent/codex-cli")
    started = _record_cli_spawns(monkeypatch)

    engine = Engine(
        contain_account_integrations=True, judge_model=_CLI_MODEL, agent_cwd=str(missing)
    )
    run = engine.new_run()
    events: list[tuple] = []
    monkeypatch.setattr(run, "notify", lambda kind, **kw: events.append((kind, kw)))

    with pytest.raises(ContainmentRefusedError, match=re.escape(str(missing))):
        await engine.judge(run, "e1", "a subject")
    assert started == []
    assert events == []


async def test_a_contained_judge_raises_any_error_instead_of_passing(monkeypatch):
    """Under containment no error can show that a contained judge ran, so none reads as a pass."""
    engine = Engine(contain_account_integrations=True, judge_model=_CLI_MODEL)
    run = engine.new_run()
    events: list[tuple] = []
    monkeypatch.setattr(run, "notify", lambda kind, **kw: events.append((kind, kw)))

    async def make_agent(*args, **kwargs):
        raise RuntimeError("an error with no refusal on its chain")

    monkeypatch.setattr(run, "make_agent", make_agent)
    with pytest.raises(RuntimeError, match="no refusal on its chain"):
        await engine.judge(run, "e1", "a subject")
    assert events == [
        ("judge_error", {"eid": "e1", "error": "an error with no refusal on its chain"})
    ]


async def test_an_uncontained_judge_still_reads_an_error_as_a_pass(monkeypatch):
    """Control: without containment a judge error keeps failing open."""
    engine = Engine(judge_model=_CLI_MODEL)
    run = engine.new_run()
    monkeypatch.setattr(run, "notify", lambda kind, **kw: None)

    async def make_agent(*args, **kwargs):
        raise RuntimeError("an error with no refusal on its chain")

    monkeypatch.setattr(run, "make_agent", make_agent)
    assert await engine.judge(run, "e1", "a subject") is True


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
async def test_the_judge_refuses_a_working_directory_it_cannot_enter(tmp_path, monkeypatch):
    """A cwd with no permissions is refused before any process starts, naming it."""
    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import ContainmentRefusedError

    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setattr(codex_mod, "CODEX_CLI", "/nonexistent/codex-cli")
    started = _record_cli_spawns(monkeypatch)
    engine = Engine(
        contain_account_integrations=True, judge_model=_CLI_MODEL, agent_cwd=str(locked)
    )
    run = engine.new_run()
    monkeypatch.setattr(run, "notify", lambda kind, **kw: None)
    locked.chmod(0o000)
    try:
        with pytest.raises(ContainmentRefusedError, match=re.escape(str(locked))):
            await engine.judge(run, "e1", "a subject")
    finally:
        locked.chmod(0o755)
    assert started == []


async def test_a_contained_judge_raises_over_a_profile_nested_too_deep_to_parse(
    tmp_path, monkeypatch
):
    """The request model rejects the profile; under containment that error is raised."""
    import lionagi.providers.openai.codex as codex_mod

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "deep.config.toml").write_text("x = " + "[" * 3000 + "]" * 3000 + "\n")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex_mod, "CODEX_CLI", "/nonexistent/codex-cli")
    started = _record_cli_spawns(monkeypatch)
    engine = Engine(
        contain_account_integrations=True, judge_model="codex/deep", agent_cwd=str(tmp_path)
    )
    run = engine.new_run()
    monkeypatch.setattr(run, "notify", lambda kind, **kw: None)
    with pytest.raises(ValueError, match="deep.config.toml"):
        await engine.judge(run, "e1", "a subject")
    assert started == []


async def test_the_judge_raises_when_an_mcp_allowlist_cannot_be_enforced(tmp_path, monkeypatch):
    """An explicit allowlist whose ambient servers cannot be discovered is a setting the
    judge cannot carry, so it is raised with containment off too. The allowlist names a
    server: an empty one is the run that loads no user config and needs no discovery."""
    import subprocess

    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import _ExclusiveMcpSelectionError

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("x = " + "[" * 3000 + "]" * 3000 + "\n")
    monkeypatch.setenv("CODEX_HOME", str(home))
    fake_cli = tmp_path / "codex"
    fake_cli.write_text("fake executable, never run\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(codex_mod, "CODEX_CLI", str(fake_cli))
    started = _record_cli_spawns(monkeypatch)

    calls = []

    def _run(argv, **kwargs):
        if argv[0] == str(fake_cli):
            calls.append((argv, kwargs))
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", _run)
    engine = Engine(judge_model=_CLI_MODEL, agent_cwd=str(tmp_path))
    run = engine.new_run()
    monkeypatch.setattr(run, "notify", lambda kind, **kw: None)
    real = run.make_agent

    async def make_agent(*args, **kwargs):
        return await real(*args, mcp_servers=["ambient"], **kwargs)

    monkeypatch.setattr(run, "make_agent", make_agent)
    with pytest.raises(
        _ExclusiveMcpSelectionError, match="Cannot enforce an exclusive Codex MCP selection"
    ):
        await engine.judge(run, "e1", "a subject")
    assert started == []
    assert calls == []


async def test_unrelated_launch_configuration_error_still_reads_as_a_pass(tmp_path, monkeypatch):
    import lionagi.providers.openai.codex as codex_mod
    from lionagi._errors import ConfigurationError

    fake_cli = tmp_path / "codex"
    fake_cli.write_text("fake executable, never run\n")
    fake_cli.chmod(0o755)
    monkeypatch.setattr(codex_mod, "CODEX_CLI", str(fake_cli))
    started = _record_cli_spawns(monkeypatch)

    def refuse(*args, **kwargs):
        raise ConfigurationError("unrelated launch error")

    monkeypatch.setattr(codex_mod, "_exclusive_cmd_args", refuse)
    engine = Engine(judge_model=_CLI_MODEL, agent_cwd=str(tmp_path))
    run = engine.new_run()
    monkeypatch.setattr(run, "notify", lambda kind, **kw: None)
    real = run.make_agent

    async def make_agent(*args, **kwargs):
        return await real(*args, mcp_servers=["ambient"], **kwargs)

    monkeypatch.setattr(run, "make_agent", make_agent)
    assert await engine.judge(run, "e1", "a subject") is True
    assert started == []

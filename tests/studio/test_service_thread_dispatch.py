# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Service routes preserve worker execution, inputs and raw response behavior."""

import asyncio
import sys
from contextvars import ContextVar
from threading import get_ident
from types import ModuleType, SimpleNamespace

import anyio
import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
HTTPException = fastapi.HTTPException


def _prepare(monkeypatch, kind, payload, *, demo=False, failure=None):
    from lionagi.studio.services import plugins, skills

    routes = {
        "plugins-list": (plugins, "list_plugins_endpoint", "list_plugins", (), "plugins"),
        "plugins-get": (plugins, "get_plugin_endpoint", "get_plugin", ("plugin",), None),
        "plugins-skill": (
            plugins,
            "get_plugin_skill_endpoint",
            "get_plugin_skill",
            ("plugin", "skill"),
            None,
        ),
        "plugins-agent": (
            plugins,
            "get_plugin_agent_endpoint",
            "get_plugin_agent",
            ("plugin", "agent"),
            None,
        ),
        "skills-list": (skills, "list_skills_route", "list_skills", (), "skills"),
        "skills-get": (skills, "get_skill_route", "get_skill", ("skill",), None),
        "skills-validate": (
            skills,
            "validate_skill_route",
            "validate_skill_content",
            ("skill",),
            None,
        ),
    }
    module, endpoint_name, worker_name, args, envelope = routes[kind]
    route_args = args
    expected_args = args
    if kind == "skills-validate":
        route_args = ("skill", skills.SkillValidateBody(content="test-local content"))
        expected_args = ("test-local content", "skill")

    context = ContextVar("service-worker-context", default=None)
    probe = SimpleNamespace(
        calls=[],
        demo_calls=[],
        projection_calls=[],
        origin=get_ident(),
        marker=object(),
        payload=payload,
        demo=demo,
        projected={"name": "test-local projected agent"},
        args=expected_args,
        kwargs={"redact": demo} if kind in {"plugins-list", "plugins-get"} else {},
        envelope=envelope,
        projects_agent=kind == "plugins-agent" and failure is None and bool(payload),
        selects_demo=kind in {"plugins-list", "plugins-get"}
        or (kind == "plugins-agent" and failure is None and bool(payload)),
    )

    def demo_mode():
        probe.demo_calls.append((get_ident(), context.get()))
        return demo

    def worker(*received_args, **received_kwargs):
        probe.calls.append((received_args, received_kwargs, get_ident(), context.get()))
        if failure is not None:
            raise failure
        return payload

    def project_agent(agent, *, redact):
        probe.projection_calls.append((agent, redact, get_ident(), context.get()))
        return probe.projected

    monkeypatch.setattr(plugins, "demo_mode_enabled", demo_mode)
    monkeypatch.setattr(plugins, "project_agent_fields", project_agent)
    monkeypatch.setattr(module, worker_name, worker)
    endpoint = getattr(module, endpoint_name)

    async def invoke():
        token = context.set(probe.marker)
        try:
            return await endpoint(*route_args)
        finally:
            context.reset(token)

    probe.invoke = invoke
    return probe


def _assert_worker_observations(probe):
    assert len(probe.calls) == 1
    args, kwargs, worker_thread, context_value = probe.calls[0]
    assert args == probe.args
    assert kwargs == probe.kwargs
    assert worker_thread != probe.origin
    assert context_value is probe.marker
    if probe.selects_demo:
        # Demo selection remains on the caller; agent projection follows offload.
        assert probe.demo_calls == [(probe.origin, probe.marker)]
    else:
        assert probe.demo_calls == []
    if probe.projects_agent:
        assert len(probe.projection_calls) == 1
        agent, redact, caller_thread, context_value = probe.projection_calls[0]
        assert agent is probe.payload
        assert redact is probe.demo
        assert caller_thread == probe.origin
        assert context_value is probe.marker
    else:
        assert probe.projection_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "demo", "empty"),
    (
        pytest.param("plugins-list", False, False, id="plugins-list"),
        pytest.param("plugins-list", True, False, id="plugins-list-demo"),
        pytest.param("plugins-list", False, True, id="plugins-list-empty"),
        pytest.param("plugins-get", False, False, id="plugins-get"),
        pytest.param("plugins-get", True, False, id="plugins-get-demo"),
        pytest.param("plugins-skill", False, False, id="plugin-skill"),
        pytest.param("plugins-agent", False, False, id="plugin-agent"),
        pytest.param("plugins-agent", True, False, id="plugin-agent-demo"),
        pytest.param("skills-list", False, False, id="skills-list"),
        pytest.param("skills-list", False, True, id="skills-list-empty"),
        pytest.param("skills-get", False, False, id="skills-get"),
        pytest.param("skills-get", False, True, id="skill-empty-dict-is-present"),
        pytest.param("skills-validate", False, True, id="validation-no-errors"),
        pytest.param("skills-validate", False, False, id="validation-errors"),
    ),
)
async def test_service_routes_preserve_worker_inputs_context_and_results(
    monkeypatch, kind, demo, empty
):
    if kind == "skills-validate":
        payload = [] if empty else ["test-local validation error"]
    elif kind.endswith("-list"):
        payload = [] if empty else [{"name": "test-local result"}]
    else:
        payload = {} if empty else {"name": "test-local result"}
    probe = _prepare(monkeypatch, kind, payload, demo=demo)

    result = await probe.invoke()

    _assert_worker_observations(probe)
    if kind == "skills-validate":
        assert result == {"ok": not payload, "errors": payload or None}
        if payload:
            assert result["errors"] is payload
    elif probe.envelope:
        assert set(result) == {probe.envelope}
        assert result[probe.envelope] is payload
    elif probe.projects_agent:
        assert result is probe.projected
    else:
        assert result is payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "payload", "detail"),
    (
        pytest.param("plugins-get", None, "Plugin plugin not found", id="plugin-none"),
        pytest.param("plugins-get", {}, "Plugin plugin not found", id="plugin-empty"),
        pytest.param(
            "plugins-skill", None, "Skill skill not found in plugin plugin", id="skill-none"
        ),
        pytest.param(
            "plugins-skill", {}, "Skill skill not found in plugin plugin", id="skill-empty"
        ),
        pytest.param(
            "plugins-agent", None, "Agent agent not found in plugin plugin", id="agent-none"
        ),
        pytest.param(
            "plugins-agent", {}, "Agent agent not found in plugin plugin", id="agent-empty"
        ),
        pytest.param("skills-get", None, "Skill 'skill' not found", id="standalone-skill-none"),
    ),
)
async def test_service_routes_preserve_missing_result_boundaries(
    monkeypatch, kind, payload, detail
):
    probe = _prepare(monkeypatch, kind, payload)

    with pytest.raises(HTTPException) as caught:
        await probe.invoke()

    _assert_worker_observations(probe)
    assert caught.value.status_code == 404
    assert caught.value.detail == detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    (
        "plugins-list",
        "plugins-get",
        "plugins-skill",
        "plugins-agent",
        "skills-list",
        "skills-get",
        "skills-validate",
    ),
)
async def test_service_routes_propagate_the_original_worker_exception(monkeypatch, kind):
    failure = LookupError("test-local worker failure")
    probe = _prepare(monkeypatch, kind, None, failure=failure)

    with pytest.raises(LookupError) as caught:
        await probe.invoke()

    _assert_worker_observations(probe)
    assert caught.value is failure


def _connection_probe(monkeypatch, behavior):
    from lionagi.studio.services import mcp_servers

    events = []
    ping_started = asyncio.Event()

    class Client:
        def __init__(self, url):
            events.append(("client", url))

        async def __aenter__(self):
            events.append("enter")
            return self

        async def ping(self):
            events.append("ping")
            ping_started.set()
            if behavior in {"timeout", "cancel"}:
                await asyncio.Event().wait()
            if behavior == "secret":
                raise RuntimeError("ping failed: example-private-value")

        async def __aexit__(self, exc_type, exc_value, traceback):
            events.append(("exit", exc_type))

    boundary = ModuleType("fastmcp")
    boundary.Client = Client
    monkeypatch.setitem(sys.modules, "fastmcp", boundary)
    monkeypatch.setattr(
        mcp_servers, "_CONNECT_TIMEOUT_SECONDS", 0.01 if behavior == "timeout" else 5
    )
    config = {"url": "https://example.invalid/mcp", "env": {"TOKEN": "example-private-value"}}
    return mcp_servers, config, events, ping_started


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ("ok", "timeout", "secret"))
async def test_connection_probe_preserves_timeout_redaction_and_context_exit(monkeypatch, behavior):
    module, config, events, _ = _connection_probe(monkeypatch, behavior)

    with anyio.fail_after(2):
        result = await module._attempt_connection(config)

    expected = {
        "ok": {"ok": True, "error": None},
        "timeout": {"ok": False, "error": "connection attempt timed out after 0.01s"},
        "secret": {"ok": False, "error": "ping failed: ***"},
    }
    assert result == expected[behavior]
    assert events[:3] == [("client", config["url"]), "enter", "ping"]
    assert len(events) == 4
    assert events[3][0] == "exit"
    if behavior == "timeout":
        assert issubclass(events[3][1], asyncio.CancelledError)
    else:
        assert events[3][1] is (RuntimeError if behavior == "secret" else None)


@pytest.mark.asyncio
async def test_connection_probe_propagates_cancellation_and_exits_context(monkeypatch):
    module, config, events, ping_started = _connection_probe(monkeypatch, "cancel")
    task = asyncio.create_task(module._attempt_connection(config))
    try:
        with anyio.fail_after(2):
            await ping_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert events[-1] == ("exit", asyncio.CancelledError)
        assert task.cancelled()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

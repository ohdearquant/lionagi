"""Credential-name handoff and formatter binding at agent construction."""

import pytest

from lionagi.agent import factory
from lionagi.agent.hooks import auto_format_python
from lionagi.agent.spec import AgentSpec
from lionagi.service.imodel import iModel


def _model():
    return iModel(provider="openai", model="gpt-test", api_key="dummy")


def _metadata(monkeypatch):
    calls = []

    def names(**kwargs):
        calls.append(kwargs)
        return frozenset({"GH_TOKEN", "OWNED_SECRET"})

    monkeypatch.setattr("lionagi._command_env_metadata.configured_command_env_names", names)
    return calls


@pytest.mark.parametrize(
    ("tool_name", "with_receiver"),
    [("bash", False), ("bash", True), ("search", False), ("search", True)],
    ids=[
        "bash-legacy-only",
        "bash-worker-receiver",
        "search-legacy-only",
        "search-worker-receiver",
    ],
)
async def test_legacy_factory_reuses_the_prepared_names_and_grants(
    tool_name, with_receiver, monkeypatch
):
    calls = _metadata(monkeypatch)
    prepared = factory._PreparedMCPForwarding(
        None, True, {"selected": {"bearer_token_env_var": "GH_TOKEN"}}
    )
    preparations = []
    forwarded = []

    def prepare(*args, **kwargs):
        preparations.append(kwargs)
        return prepared

    monkeypatch.setattr(factory, "_prepare_mcp_forwarding", prepare)
    original_forward = factory._forward_mcp_to_cli_request

    def forward(*args, **kwargs):
        forwarded.append(kwargs["_prepared"])
        return original_forward(*args, **kwargs)

    monkeypatch.setattr(factory, "_forward_mcp_to_cli_request", forward)
    if tool_name == "bash":
        from lionagi.tools.code import bash as module

        original = module.BashTool
        symbol = "BashTool"
    else:
        from lionagi.tools.code import search as module

        original = module.SearchTool
        symbol = "SearchTool"
    constructed = []

    def tool(**kwargs):
        constructed.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(module, symbol, tool)
    received = []
    model = _model()
    try:
        spec = AgentSpec.compose(
            "implementer", tools=(tool_name,), command_env_allowlist=("SOMEVENDOR_API_KEY",)
        )
        private = {"_command_env_names_out": received.append} if with_receiver else {}
        branch = await factory.create_agent(
            spec,
            load_settings=False,
            chat_model=model,
            resolved_mcp_servers={},
            **private,
        )
        assert len(preparations) == len(calls) == len(constructed) == 1
        assert forwarded == [prepared]
        assert calls[0]["mcp_auth_env_names"] == frozenset({"GH_TOKEN"})
        assert constructed[0]["_command_env_names"] == frozenset({"GH_TOKEN", "OWNED_SECRET"})
        assert isinstance(constructed[0]["_command_env_names"], frozenset)
        if with_receiver:
            assert received == [constructed[0]["_command_env_names"]]
            assert constructed[0]["_command_env_names"] is received[0]
        else:
            assert received == []
        grant_key = "command_env_allowlist" if tool_name == "bash" else "_command_env_allowlist"
        assert constructed[0][grant_key] == ("SOMEVENDOR_API_KEY",)
        assert f"{tool_name}_tool" in branch.acts.registry
    finally:
        await model.close()


async def test_factory_does_not_publish_names_when_final_wiring_fails(monkeypatch):
    _metadata(monkeypatch)
    received = []

    def fail(*args):
        raise RuntimeError("final wiring failed")

    monkeypatch.setattr(factory, "_wire_external_hooks", fail)
    model = _model()
    try:
        with pytest.raises(RuntimeError, match="final wiring failed"):
            await factory.create_agent(
                AgentSpec.compose("implementer"),
                load_settings=False,
                chat_model=model,
                resolved_mcp_servers={},
                _command_env_names_out=received.append,
            )
        assert received == [], "an unsuccessful factory must not publish a names snapshot"
    finally:
        await model.close()


async def test_unrelated_factory_does_not_read_command_metadata(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError("an unrelated agent must not read command metadata")

    monkeypatch.setattr("lionagi._command_env_metadata.configured_command_env_names", forbidden)
    model = _model()
    try:
        await factory.create_agent(
            AgentSpec.compose("implementer"),
            load_settings=False,
            chat_model=model,
            resolved_mcp_servers={},
        )
    finally:
        await model.close()


async def test_formatter_only_factory_binds_selected_names(monkeypatch):
    import lionagi.agent.hooks as hooks

    calls = _metadata(monkeypatch)
    prepared = factory._PreparedMCPForwarding(
        None, True, {"selected": {"bearer_token_env_var": "GH_TOKEN"}}
    )
    monkeypatch.setattr(factory, "_prepare_mcp_forwarding", lambda *args, **kwargs: prepared)
    monkeypatch.setenv("GH_TOKEN", "synthetic-selected")
    monkeypatch.setenv("SOMEVENDOR_API_KEY", "synthetic-granted")
    seen = []

    async def builtin(*args, _command_env):
        seen.append((args, _command_env))

    monkeypatch.setattr(hooks, "auto_format_python", builtin)
    spec = AgentSpec.compose(
        "implementer", tools=("reader",), command_env_allowlist=("SOMEVENDOR_API_KEY",)
    )
    spec.post("reader", builtin)
    model = _model()
    try:
        branch = await factory.create_agent(
            spec,
            load_settings=False,
            chat_model=model,
            resolved_mcp_servers={},
        )
        result = {"success": True}
        await branch.acts.registry["reader_tool"].postprocessor(result)
        assert len(calls) == len(seen) == 1
        assert calls[0]["mcp_auth_env_names"] == frozenset({"GH_TOKEN"})
        assert seen[0][0] == ("reader", "", {}, result)
        assert "GH_TOKEN" not in seen[0][1]
        assert seen[0][1]["SOMEVENDOR_API_KEY"] == "synthetic-granted"
    finally:
        await model.close()


async def test_only_the_exact_builtin_receives_fresh_hook_environment(monkeypatch):
    import lionagi.agent.hooks as hooks

    calls = []
    generation = 0

    def environment():
        nonlocal generation
        generation += 1
        return {"GENERATION": str(generation)}

    async def builtin(*args, _command_env):
        calls.append((args, _command_env))

    async def custom(*args, **kwargs):
        assert kwargs == {}, "only the exact builtin may receive the private environment"
        calls.append((args, None))

    custom.__name__ = "auto_format_python"
    monkeypatch.setattr(hooks, "auto_format_python", builtin)
    chain = factory._chain_post_hooks(
        "editor", [builtin, custom], _command_env_supplier=environment
    )
    result = {"success": True}
    assert await chain(result) is result
    assert await chain(result) is result
    assert [args for args, env in calls] == [("editor", "", {}, result)] * 4
    assert [env for args, env in calls] == [
        {"GENERATION": "1"},
        None,
        {"GENERATION": "2"},
        None,
    ]


async def test_discovered_mcp_hook_receives_the_same_supplier(monkeypatch):
    from types import SimpleNamespace

    import lionagi.agent.hooks as hooks

    seen = []

    async def builtin(*args, _command_env):
        seen.append((args, _command_env))

    monkeypatch.setattr(hooks, "auto_format_python", builtin)
    monkeypatch.setattr(factory, "_resolve_mcp_path", lambda *args, **kwargs: "configured.json")
    tool = SimpleNamespace(preprocessor=None, postprocessor=None)
    loads = []

    async def load(path, **kwargs):
        loads.append((path, kwargs))
        return {"selected": ["lookup"]}

    branch = SimpleNamespace(acts=SimpleNamespace(load_mcp_config=load, registry={"lookup": tool}))
    spec = AgentSpec.compose("implementer")
    spec.post("lookup", builtin)
    environment = {"ALLOWED": "synthetic"}
    loaded = await factory._load_mcp(
        branch,
        spec,
        _command_env_supplier=lambda: environment,
    )
    result = {"success": True}
    await tool.postprocessor(result)
    assert loaded == frozenset({"selected"}) and len(loads) == 1
    assert loads[0][0] == "configured.json"
    assert seen == [(("lookup", "", {}, result), environment)]


@pytest.mark.parametrize("explicit", [False, True], ids=["owned-defaults", "operator-grant"])
async def test_direct_formatter_prepares_environment_before_thread_handoff(explicit, monkeypatch):
    from lionagi._command_env_metadata import command_environment

    _metadata(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "synthetic-selected")
    monkeypatch.setenv("SOMEVENDOR_API_KEY", "synthetic-granted")
    monkeypatch.setenv("somevendor_api_key", "synthetic-alias")
    calls = []

    async def capture(fn, cmd, shell, timeout, cwd, *, env=None):
        calls.append((cmd, shell, timeout, cwd, None if env is None else dict(env)))
        return {"returncode": 0}

    monkeypatch.setattr("lionagi.ln.concurrency.run_sync", capture)
    supplied = (
        command_environment(
            declared_names=frozenset({"GH_TOKEN"}), command_env_allowlist=("SOMEVENDOR_API_KEY",)
        )
        if explicit
        else None
    )
    private = {"_command_env": supplied} if explicit else {}
    await auto_format_python(
        "editor",
        "write",
        {"file_path": "src/example.py"},
        {"success": True},
        **private,
    )
    cmd, shell, timeout, cwd, env = calls[0]
    assert (cmd, shell, timeout, cwd) == (["ruff", "format", "src/example.py"], False, 10.0, None)
    assert env is not None, "formatter must prepare a credential-filtered environment"
    assert "GH_TOKEN" not in env
    assert "somevendor_api_key" not in env
    assert ("SOMEVENDOR_API_KEY" in env) is explicit

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exercise coding-command environments through their real execution boundaries."""

from __future__ import annotations

import json
import os
import shlex
import sys
from types import SimpleNamespace

import pytest

from lionagi.tools._seatbelt import _uv_cache_dir as _real_uv_cache_dir
from tests.cli.test_agent_api_write_set import roots as roots

_CANARY = "SOMEVENDOR_API_KEY"
_ALIAS = "somevendor_api_key"
_BEARER = "GH_TOKEN"
_HEADER = "MCP_HEADER_NAME"
_BENIGN = "MY_TOKENIZER_PATH"


@pytest.fixture
def command_scope(monkeypatch, tmp_path, roots):
    from lionagi import config

    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(roots / "tmp"),
        _CANARY: "synthetic-pattern-canary",
        _ALIAS: "synthetic-case-alias",
        _BEARER: "synthetic-bearer-reference",
        _HEADER: "synthetic-header-reference",
        _BENIGN: "literal=benign value",
        "LIONAGI_STATE_DB_URL": "sqlite:///synthetic-parent-store.db",
    }
    monkeypatch.setattr(os, "environ", environment)
    isolated = config.AppSettings.model_construct(ANTHROPIC_ACCOUNTS=())
    monkeypatch.setattr(config, "settings", isolated)
    monkeypatch.chdir(roots / "workspace")
    return SimpleNamespace(
        root=roots,
        workspace=roots / "workspace",
        home=home,
        environment=environment,
    )


def _branch():
    from lionagi.service.imodel import iModel
    from lionagi.session.branch import Branch

    return Branch(
        chat_model=iModel(provider="openai", model="no-request", api_key="dummy-key-for-testing")
    )


def _bound_tools(scope, *, grant=False, confined=False, tools=("bash",)):
    from lionagi.tools._seatbelt import SeatbeltPolicy, seatbelt_available
    from lionagi.tools.coding import CodingToolkit

    policy = None
    if confined:
        if not seatbelt_available():
            pytest.skip("needs macOS with /usr/bin/sandbox-exec")
        policy = SeatbeltPolicy.for_workspace(scope.workspace)
    options = {
        "notify": False,
        "workspace_root": scope.workspace,
        "tools": tools,
        "seatbelt": policy,
    }
    if grant:
        options["command_env_allowlist"] = (_CANARY, "LIONAGI_STATE_DB_URL")
    toolkit = CodingToolkit(**options)
    return {tool.func_callable.__name__: tool for tool in toolkit.bind(_branch())}


def _env_output(result):
    assert result["return_code"] == 0, result
    return dict(line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line)


@pytest.mark.parametrize(
    "confined,grant",
    [(False, False), (False, True), (True, False), (True, True)],
    ids=["unconfined-default", "unconfined-granted", "confined-default", "confined-granted"],
)
async def test_real_bound_bash_environment(command_scope, confined, grant):
    before = dict(command_scope.environment)
    tools = _bound_tools(command_scope, grant=grant, confined=confined)

    observed = _env_output(await tools["bash"].func_callable(command="/usr/bin/env"))

    assert observed.get(_CANARY) == (before[_CANARY] if grant else None), (
        "coding bash must honor default sensitive-name denial and exact grants"
    )
    assert _ALIAS not in observed, "an exact grant must not release a case alias"
    assert observed[_BEARER] == before[_BEARER], "undeclared GH_TOKEN is an intentional limit"
    assert observed[_BENIGN] == before[_BENIGN]
    assert "LIONAGI_STATE_DB_URL" not in observed
    assert command_scope.environment == before


async def test_declared_lookup_reference_is_removed_from_real_bash(command_scope):
    directory = command_scope.home / ".lionagi"
    directory.mkdir()
    directory.joinpath("settings.yaml").write_text(
        json.dumps(
            {
                "secrets": {
                    "lookup": {
                        "argv": ["/synthetic/not-executed", "{name}"],
                        "names": [_BEARER],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    before = dict(command_scope.environment)
    tools = _bound_tools(command_scope)

    observed = _env_output(await tools["bash"].func_callable(command="/usr/bin/env"))

    assert _BEARER not in observed, "declared lookup references must not reach coding bash"
    assert observed[_HEADER] == before[_HEADER], "an undeclared reference remains visible"
    assert observed[_BENIGN] == before[_BENIGN]
    assert _CANARY not in observed
    assert command_scope.environment == before


@pytest.mark.parametrize(
    "kind,selected",
    [("bearer", True), ("header", True), ("bearer", False), ("header", False)],
    ids=["selected-bearer", "selected-header", "deselected-bearer", "deselected-header"],
)
async def test_selected_mcp_auth_reference_reaches_bound_bash_policy(command_scope, kind, selected):
    from lionagi._command_env_metadata import configured_command_env_names
    from lionagi.agent.factory import create_agent
    from lionagi.agent.spec import AgentSpec
    from lionagi.service.imodel import iModel

    target = _BEARER if kind == "bearer" else _HEADER
    other = _HEADER if kind == "bearer" else _BEARER
    assert target not in configured_command_env_names(), "the canary must be MCP-only metadata"
    auth = (
        {"bearer_token_env_var": target}
        if kind == "bearer"
        else {"env_http_headers": {"X-Test-Authentication": target}}
    )
    servers = {
        "protected": {"url": "https://example.invalid/protected", **auth},
        "public": {"url": "https://example.invalid/public"},
    }
    spec = AgentSpec.compose("implementer", tools=("coding",), cwd=str(command_scope.workspace))
    spec.mcp_servers = ["protected" if selected else "public"]
    model = iModel(provider="claude_code", model="sonnet", api_key="dummy-key-for-testing")

    branch = await create_agent(
        spec, load_settings=False, chat_model=model, resolved_mcp_servers=servers
    )
    observed = _env_output(await branch.acts.registry["bash"].func_callable(command="/usr/bin/env"))

    assert observed.get(target) == (None if selected else command_scope.environment[target]), (
        "only selected MCP authentication names belong to the coding deny set"
    )
    assert observed[other] == command_scope.environment[other]
    assert branch.chat_model.endpoint.config.kwargs["mcp_servers"] == {
        name: servers[name] for name in spec.mcp_servers
    }
    assert observed[_BENIGN] == command_scope.environment[_BENIGN]


async def test_cli_forwarding_reuses_the_mapping_read_before_tool_registration(
    command_scope, monkeypatch
):
    from lionagi.agent.factory import create_agent
    from lionagi.agent.spec import AgentSpec
    from lionagi.protocols.action.manager import ActionManager
    from lionagi.service.imodel import iModel

    initial = {
        "protected": {
            "url": "https://example.invalid/initial",
            "bearer_token_env_var": _BEARER,
        }
    }
    replacement = {"replacement": {"url": "https://example.invalid/replacement"}}
    path = command_scope.workspace / "selected.mcp.json"
    path.write_text(json.dumps({"mcpServers": initial}), encoding="utf-8")
    native_reads = []

    async def native_registration(
        self, config_path, server_names=None, update=False, mcp_security=None
    ):
        native_reads.append((config_path, server_names, mcp_security))
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"] == initial
        path.write_text(json.dumps({"mcpServers": replacement}), encoding="utf-8")
        return {}

    monkeypatch.setattr(ActionManager, "load_mcp_config", native_registration)
    spec = AgentSpec.compose("implementer", tools=("coding",), cwd=str(command_scope.workspace))
    spec.mcp_config_path = str(path)
    model = iModel(provider="claude_code", model="sonnet", api_key="dummy-key-for-testing")

    branch = await create_agent(spec, load_settings=False, chat_model=model)
    observed = _env_output(await branch.acts.registry["bash"].func_callable(command="/usr/bin/env"))

    assert len(native_reads) == 1 and native_reads[0][0] == str(path)
    assert native_reads[0][2].allow_commands is True
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"] == replacement
    assert branch.chat_model.endpoint.config.kwargs["mcp_servers"] == initial
    assert _BEARER not in observed
    assert observed[_HEADER] == command_scope.environment[_HEADER]


_INSPECT_CHILD = """import json
import os
from pathlib import Path
result = {
    "credential": os.environ.get("SOMEVENDOR_API_KEY"),
    "alias": os.environ.get("somevendor_api_key"),
    "benign": os.environ.get("MY_TOKENIZER_PATH"),
}
Path("child-environment.json").write_text(json.dumps(result), encoding="utf-8")
print(json.dumps(result))
"""


@pytest.mark.parametrize(
    "stage,grant",
    [("gate", False), ("repair", False), ("gate", True), ("repair", True)],
    ids=["default-gate", "default-repair", "granted-gate", "granted-repair"],
)
async def test_configured_gate_and_repair_scrub_agent_written_code(command_scope, stage, grant):
    from lionhub.orchestration.engines.coding import (
        AutoRepairApplied,
        ChangeProposed,
        CodingChainEvent,
        CodingEngine,
    )

    writer = _bound_tools(command_scope, tools=("editor",))["editor"]
    edited = await writer.func_callable(
        action="write", file_path="inspect_child.py", content=_INSPECT_CHILD
    )
    assert edited["success"] is True, edited
    command = [sys.executable, "-I", "-B", str(command_scope.workspace / "inspect_child.py")]
    options = {"test_timeout_s": 10.0}
    if grant:
        options["command_env_allowlist"] = (_CANARY,)
    if stage == "repair":
        options["auto_repair_cmds"] = [shlex.join(command)]
    engine = CodingEngine(**options)
    run = engine.new_run()
    run.workspace = str(command_scope.workspace)
    run.observe(CodingChainEvent, lambda event, context: run.collect(event))
    change = run.collect(ChangeProposed(summary="inspect the child environment"))

    if stage == "gate":
        result = await engine._run_subprocess_gate(
            run, change, command, round_no=0, notify_event="testing"
        )
        assert result.passed is True and result.returncode == 0
    else:
        result = await engine._apply_auto_repairs(run, round_no=0)
        assert len(result) == 1 and isinstance(result[0], AutoRepairApplied)
        assert "child-environment.json" in result[0].files
    observed = json.loads((command_scope.workspace / "child-environment.json").read_text())

    assert observed == {
        "credential": command_scope.environment[_CANARY] if grant else None,
        "alias": None,
        "benign": command_scope.environment[_BENIGN],
    }, "configured commands must honor default credential denial and exact grants"


@pytest.mark.parametrize("boundary", ["default", "exec-cell"], ids=["default", "exec-cell"])
async def test_default_commands_and_explicit_execution_cells_keep_distinct_grants(
    command_scope, boundary
):
    from lionagi.ln.concurrency import run_sync
    from lionagi.tools._subprocess import _subprocess_sync
    from lionagi.tools.sandbox_backend import Cell, Handle, LocalWorktreeBackend

    script = command_scope.workspace / "inspect_child.py"
    script.write_text(_INSPECT_CHILD, encoding="utf-8")
    command = [sys.executable, "-I", "-B", str(script)]
    explicit = {_CANARY: "explicit-cell-grant", _BENIGN: "explicit-cell-benign"}
    before = dict(command_scope.environment)
    if boundary == "default":
        result = await run_sync(
            _subprocess_sync, command, False, 10.0, str(command_scope.workspace)
        )
        assert result["returncode"] == 0, result
        observed = json.loads(result["stdout"])
        assert observed == {"credential": None, "alias": None, "benign": before[_BENIGN]}
    else:
        handle = Handle(
            backend="local_worktree", remote_id=None, remote_repo_path=str(command_scope.workspace)
        )
        cell = Cell(
            kind="exec_cell",
            entrypoint=shlex.join(command),
            env=explicit,
            artifact_manifest=("child-environment.json",),
            timeout_s=10,
        )
        result = await LocalWorktreeBackend().run_cell(handle, cell)
        assert result.exit_code == 0, result
        observed = json.loads(result.artifacts["child-environment.json"])
        assert observed == {
            "credential": explicit[_CANARY],
            "alias": None,
            "benign": explicit[_BENIGN],
        }
        assert explicit == {_CANARY: "explicit-cell-grant", _BENIGN: "explicit-cell-benign"}
    assert command_scope.environment == before


async def test_shell_startup_file_is_not_loaded_without_an_operator_grant(command_scope):
    startup = command_scope.workspace / "startup.sh"
    startup.write_text("export STARTUP_WAS_READ=synthetic-startup-marker\n", encoding="utf-8")
    script = command_scope.workspace / "show_environment.sh"
    script.write_text("/usr/bin/env\n", encoding="utf-8")
    command_scope.environment["BASH_ENV"] = str(startup)
    command_scope.environment["ENV"] = str(startup)
    tools = _bound_tools(command_scope)

    observed = _env_output(
        await tools["bash"].func_callable(command=shlex.join(["/bin/bash", str(script)]))
    )

    assert "STARTUP_WAS_READ" not in observed
    assert "BASH_ENV" not in observed and "ENV" not in observed
    assert observed[_BENIGN] == command_scope.environment[_BENIGN]
    assert startup.read_text() == "export STARTUP_WAS_READ=synthetic-startup-marker\n"


def test_operator_grants_roundtrip_without_entering_tool_request_schemas(command_scope):
    from lionagi.agent.spec import AgentSpec
    from lionagi.tools.code.bash import BashRequest

    spec = AgentSpec.compose("implementer", tools=("coding",), command_env_allowlist=(_CANARY,))
    path = command_scope.workspace / "agent.yaml"
    spec.to_yaml(path)
    restored = AgentSpec.from_yaml(path)
    assert restored.command_env_allowlist == (_CANARY,)
    assert "synthetic-pattern-canary" not in path.read_text()
    tools = _bound_tools(command_scope, grant=True, tools=("bash", "search"))
    for tool in tools.values():
        properties = tool.tool_schema["function"]["parameters"].get("properties", {})
        assert "env" not in properties and "command_env_allowlist" not in properties
    assert "env" not in BashRequest.model_json_schema()["properties"]
    assert "command_env_allowlist" not in BashRequest.model_json_schema()["properties"]


def test_uv_cache_probe_receives_scrubbed_environment(command_scope, monkeypatch):
    from lionagi.tools import _seatbelt

    captured = []
    expected = command_scope.root / "reported-cache"
    before = dict(command_scope.environment)
    monkeypatch.setattr(_seatbelt.shutil, "which", lambda name: "/synthetic/uv")

    def completed(argv, **kwargs):
        captured.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=str(expected) + "\n")

    monkeypatch.setattr(_seatbelt.subprocess, "run", completed)
    # This canonical function was captured before roots replaces the attribute for sandbox setup.
    result = _real_uv_cache_dir()

    assert result == expected
    assert len(captured) == 1
    argv, kwargs = captured[0]
    assert argv == ["/synthetic/uv", "cache", "dir"] and kwargs["timeout"] == 10
    assert _CANARY not in kwargs["env"] and _ALIAS not in kwargs["env"]
    assert kwargs["env"][_BENIGN] == before[_BENIGN]
    assert command_scope.environment == before

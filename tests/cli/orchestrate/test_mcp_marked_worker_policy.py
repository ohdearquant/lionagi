"""Named snapshot-shaped configurations remain explicit and contain workers."""

import argparse
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.cli import orchestrate
from lionagi.cli.orchestrate import _checkpoint, _orchestration, flow
from lionagi.cli.orchestrate._checkpoint import CheckpointWriter, load_checkpoint
from lionhub.orchestration.flow.parsing import add_orchestrate_subparser
from tests.cli.orchestrate.test_no_mcp_config_handoff import _cli_args, _setup, tmp_run


def _write_config(path, *, marked):
    config = {
        "mcpServers": {
            "control": {"command": "li", "args": ["mcp"]},
            "other": {"command": "echo"},
        }
    }
    if marked:
        config["lionagiSnapshotOf"] = str(path.parent / ".mcp.json")
    path.write_text(json.dumps(config))


async def _assert_request_sets(env, expected_workers):
    assert env.mcp_config_explicit is True
    orc_args = await _cli_args(env.orc_branch.chat_model)
    assert "--strict-mcp-config" in orc_args
    assert set(json.loads(orc_args[orc_args.index("--mcp-config") + 1])["mcpServers"]) == {
        "control",
        "other",
    }
    worker, _, _, _ = await _orchestration.build_worker_branch(
        env, agent_id="w1", role="implementer", model_override="claude_code/sonnet"
    )
    worker_args = await _cli_args(worker.chat_model)
    assert "--strict-mcp-config" in worker_args
    assert set(json.loads(worker_args[worker_args.index("--mcp-config") + 1])["mcpServers"]) == (
        expected_workers
    )
    assert set(env.withheld_mcp_servers) == {"control", "other"} - expected_workers


def test_previous_submit_argv_contains_workers(tmp_run, monkeypatch):  # noqa: F811
    """Exercise the old submit carrier through the real new CLI parser."""
    snapshot = tmp_run / "mcp-servers.json"
    _write_config(snapshot, marked=True)
    # This is the previous submit command's shape; only its per-job path and
    # run identifier vary from invocation to invocation.
    argv = [
        "orchestrate",
        "flow",
        "--notify",
        "python -m lionhub.serve.mcp._notify_hook --run-id synthetic --status {status}",
        "--mcp-config",
        str(snapshot),
        "--",
        "claude_code/sonnet",
        "work",
    ]
    parser = argparse.ArgumentParser()
    add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    setup = flow.setup_orchestration

    class Captured(Exception):
        pass

    async def capture(**kwargs):
        env = await setup(**kwargs)
        await _assert_request_sets(env, {"other"})
        raise Captured

    monkeypatch.setattr(flow, "setup_orchestration", capture)
    with pytest.raises(Captured):
        orchestrate.run_orchestrate(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [True, False], ids=["marked", "unmarked"])
async def test_named_file_worker_scope(tmp_run, marked):  # noqa: F811
    named = tmp_run / "named.json"
    _write_config(named, marked=marked)
    env = await _setup(no_mcp_config=False, mcp_config=str(named))
    await _assert_request_sets(env, {"other"} if marked else {"control", "other"})


@pytest.mark.asyncio
async def test_resume_named_marked_file_contains_workers(tmp_run, monkeypatch):  # noqa: F811
    named = tmp_run / "named.json"
    _write_config(named, marked=True)
    checkpoint_path = tmp_run / "checkpoint.json"
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    monkeypatch.setattr(flow, "start_live_persist", AsyncMock())
    monkeypatch.setattr(flow, "stop_live_persist", AsyncMock(return_value="completed"))
    seen = []

    async def execute(_model, prompt, **kwargs):
        env = kwargs["env"]
        seen.append(env)
        if len(seen) == 1:
            writer = CheckpointWriter(
                path=checkpoint_path,
                session_id=str(env.session.id),
                prompt=prompt,
                plan=[],
                config=kwargs["checkpoint_config"],
            )
            await writer.flush()
        else:
            await _assert_request_sets(env, {"other"})
        return "ok"

    monkeypatch.setattr(flow, "_run_flow_inner", execute)
    await flow._run_flow("claude_code/sonnet", "work", mcp_config=str(named))
    checkpoint = load_checkpoint(checkpoint_path)
    assert checkpoint["config"]["mcp_config_origin"] == "explicit"
    monkeypatch.setattr(
        _checkpoint,
        "_find_run_dir_by_id",
        lambda _: SimpleNamespace(checkpoint_path=checkpoint_path),
    )
    await flow._resume_flow("recorded-run")
    assert len(seen) == 2

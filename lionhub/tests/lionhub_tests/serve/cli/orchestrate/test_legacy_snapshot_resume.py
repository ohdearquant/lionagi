"""Old flow and play checkpoints refuse ambiguous submit snapshots."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionhub.orchestration import mcp_config
from lionhub.orchestration.flow.checkpoint import FlowResumeError
from lionhub.serve.cli.orchestrate import flow

from .test_no_mcp_config_handoff import tmp_run


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["flow", "play"])
@pytest.mark.parametrize("shape", ["marked", "invalid", "missing", "unmarked"])
async def test_legacy_replay_reads_only_recorded_file(tmp_run, monkeypatch, pattern, shape):  # noqa: F811
    recorded = tmp_run / "recorded.json"
    data = {"mcpServers": {"one": {"command": "echo"}}}
    if shape == "marked":
        data["lionagiSnapshotOf"] = str(tmp_run / ".mcp.json")
    if shape != "missing":
        recorded.write_text("{bad" if shape == "invalid" else json.dumps(data))
    checkpoint = {
        "prompt": "work",
        "config": {
            "model_spec": "claude_code/sonnet",
            "mcp_config": str(recorded),
            "no_mcp_config": False,
            "mcp_launch_dir": str(tmp_run),
            "playbook_name": "sample" if pattern == "play" else None,
        },
    }
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, checkpoint))
    )
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    monkeypatch.setattr(
        mcp_config, "discover_mcp_config", lambda _: pytest.fail("ambient discovery")
    )
    seen = []
    read = mcp_config._read_config

    def tracked(path):
        seen.append(Path(path))
        return read(path)

    monkeypatch.setattr(mcp_config, "_read_config", tracked)
    setup = flow.setup_orchestration

    class Captured(Exception):
        pass

    async def capture_setup(**kwargs):
        env = await setup(**kwargs)
        assert env.mcp_config_explicit is True
        assert set(env.mcp_servers) == {"one"}
        raise Captured

    monkeypatch.setattr(flow, "setup_orchestration", capture_setup)
    with pytest.raises(Captured if shape == "unmarked" else FlowResumeError):
        await flow._resume_flow("recorded-run")
    assert seen == [recorded]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recorded", "launch_dir", "disabled"),
    [
        ([], "/tmp", False),
        ("", "/tmp", False),
        ("relative.json", None, False),
        ("/tmp/recorded.json", {}, False),
        ("/tmp/recorded.json", "/tmp", "false"),
    ],
)
async def test_corrupt_legacy_checkpoint_refuses_with_fixed_message(
    tmp_path, monkeypatch, recorded, launch_dir, disabled
):
    checkpoint = {
        "config": {"mcp_config": recorded, "mcp_launch_dir": launch_dir, "no_mcp_config": disabled}
    }
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, checkpoint))
    )
    run = AsyncMock()
    monkeypatch.setattr(flow, "_run_flow", run)
    with pytest.raises(FlowResumeError, match="recorded MCP.*fresh run"):
        await flow._resume_flow("recorded-run")
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"mcp_submit_snapshot": ["not", "a", "path"]},
        {"mcp_submit_snapshot": "relative.json"},
        {"mcp_submit_snapshot": ""},
        {"no_mcp_config": "false"},
        {"no_mcp_config": None},
        {"no_mcp_config": True},
        {"mcp_config_origin": "unknown"},
        {"mcp_config": "/tmp/named.json"},
        {"mcp_launch_dir": "relative-directory"},
    ],
)
async def test_invalid_recorded_snapshot_origin_is_refused(monkeypatch, override):
    config = {
        "mcp_submit_snapshot": "/tmp/recorded.json",
        "mcp_config_origin": "submit_snapshot",
        "no_mcp_config": False,
        **override,
    }
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, {"config": config}))
    )
    run = AsyncMock()
    monkeypatch.setattr(flow, "_run_flow", run)
    with pytest.raises(FlowResumeError, match="recorded.*fresh run"):
        await flow._resume_flow("recorded-run")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_absolute_snapshot_can_replay_without_stored_launch_dir(monkeypatch):
    config = {
        "mcp_submit_snapshot": "/tmp/recorded.json",
        "mcp_config_origin": "submit_snapshot",
        "no_mcp_config": False,
    }
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, {"config": config}))
    )
    run = AsyncMock(return_value=("ok", "completed"))
    monkeypatch.setattr(flow, "_run_flow", run)
    await flow._resume_flow("recorded-run")
    assert run.await_args.kwargs["mcp_submit_snapshot"] == config["mcp_submit_snapshot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [True, False])
async def test_missing_launch_dir_replays_current_discovery_with_truthful_reason(
    tmp_run,  # noqa: F811
    monkeypatch,
    found,
):
    source = tmp_run / ".mcp.json"
    if found:
        source.write_text(json.dumps({"mcpServers": {"current": {"command": "echo"}}}))
    config = {"model_spec": "claude_code/sonnet", "mcp_config": None, "no_mcp_config": False}
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, {"config": config}))
    )
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    setup = flow.setup_orchestration

    class Captured(Exception):
        pass

    async def capture_setup(**kwargs):
        env = await setup(**kwargs)
        assert (set(env.mcp_servers) if env.mcp_servers is not None else None) == (
            {"current"} if found else None
        )
        env.run.write_manifest({"status": "running"})
        manifest = env.run.read_manifest()
        assert manifest["mcp_config_searched_from"] == str(tmp_run.resolve())
        assert "no MCP launch directory was recorded" in manifest["mcp_config_reason"]
        assert str(tmp_run.resolve()) in manifest["mcp_config_reason"]
        assert ("resolution resolved" if found else "resolution no_mcp_config_found") in manifest[
            "mcp_config_reason"
        ]
        raise Captured

    monkeypatch.setattr(flow, "setup_orchestration", capture_setup)
    with pytest.raises(Captured):
        await flow._resume_flow("recorded-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", ["false", 0, None])
async def test_malformed_recorded_flag_never_falls_back_to_discovery(monkeypatch, disabled):
    config = {"mcp_config": None, "mcp_launch_dir": "/tmp", "no_mcp_config": disabled}
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, {"config": config}))
    )
    run = AsyncMock()
    monkeypatch.setattr(flow, "_run_flow", run)
    with pytest.raises(FlowResumeError, match="recorded MCP choice.*fresh run"):
        await flow._resume_flow("recorded-run")
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_named_marked_config_survives_flow_start_and_resume(tmp_run, monkeypatch):  # noqa: F811
    """A named file remains caller-owned even when it has a snapshot marker."""
    from lionhub.orchestration.flow import checkpoint as _checkpoint
    from lionhub.orchestration.flow.checkpoint import CheckpointWriter, load_checkpoint

    recorded = tmp_run / "named.json"
    recorded.write_text(
        json.dumps(
            {
                "lionagiSnapshotOf": str(tmp_run / "old.json"),
                "mcpServers": {"named": {"command": "echo"}},
            }
        )
    )
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    monkeypatch.setattr(flow, "start_live_persist", AsyncMock())
    monkeypatch.setattr(flow, "stop_live_persist", AsyncMock(return_value="completed"))
    monkeypatch.setattr(
        mcp_config, "discover_mcp_config", lambda _: pytest.fail("ambient discovery")
    )
    seen = []
    checkpoint_path = tmp_run / "checkpoint.json"

    async def execute(_model, prompt, **kwargs):
        env = kwargs["env"]
        seen.append(set(env.mcp_servers))
        if len(seen) == 1:
            writer = CheckpointWriter(
                path=checkpoint_path,
                session_id=str(env.session.id),
                prompt=prompt,
                plan=[],
                config=kwargs["checkpoint_config"],
            )
            await writer.flush()
        return "ok"

    monkeypatch.setattr(flow, "_run_flow_inner", execute)
    await flow._run_flow("claude_code/sonnet", "work", mcp_config="named.json")
    checkpoint = load_checkpoint(checkpoint_path)
    assert checkpoint["config"]["mcp_config"] == str(recorded)
    monkeypatch.setattr(
        _checkpoint,
        "_find_run_dir_by_id",
        lambda _: SimpleNamespace(checkpoint_path=checkpoint_path),
    )
    await flow._resume_flow("recorded-run")
    assert seen == [{"named"}, {"named"}]
    assert checkpoint["config"]["mcp_config_origin"] == "explicit"

    # Identical bytes from an older record have no trustworthy origin.
    checkpoint["config"].pop("mcp_config_origin")
    checkpoint_path.write_text(json.dumps(checkpoint))
    with pytest.raises(FlowResumeError, match="ambiguous"):
        await flow._resume_flow("recorded-run")
    assert seen == [{"named"}, {"named"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"mcp_config": None},
        {"mcp_config": "relative.json"},
        {"mcp_config": ""},
        {"mcp_config": []},
        {"no_mcp_config": True},
        {"no_mcp_config": None},
        {"mcp_submit_snapshot": "/tmp/snapshot.json"},
    ],
)
async def test_invalid_explicit_recorded_origin_refuses(monkeypatch, override):
    config = {
        "mcp_config_origin": "explicit",
        "mcp_config": "/tmp/named.json",
        "no_mcp_config": False,
        **override,
    }
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, {"config": config}))
    )
    run = AsyncMock()
    monkeypatch.setattr(flow, "_run_flow", run)
    with pytest.raises(FlowResumeError, match="recorded.*fresh run"):
        await flow._resume_flow("recorded-run")
    run.assert_not_awaited()

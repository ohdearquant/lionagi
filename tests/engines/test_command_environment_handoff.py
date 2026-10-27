"""Worker-only environment grants and successful construction snapshots."""

import pytest

from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionhub.orchestration.engines import Engine
from lionhub.orchestration.engines.coding import CodingEngine, WorkPlanned


def _install_factory(monkeypatch, *, fail=False):
    calls = []
    models = []

    async def create(spec, **kwargs):
        calls.append((spec, kwargs))
        receiver = kwargs.get("_command_env_names_out")
        if receiver is not None:
            receiver(frozenset({"GH_TOKEN"}))
        if fail:
            raise RuntimeError("factory failed after collecting names")
        model = iModel(provider="openai", model="gpt-test", api_key="dummy")
        models.append(model)
        return Branch(chat_model=model)

    monkeypatch.setattr("lionhub.orchestration.engines.engine.create_agent", create)
    return calls, models


async def _close(models):
    for model in models:
        await model.close()


async def test_make_agent_forwards_trusted_grants_and_names_receiver(monkeypatch):
    calls, models = _install_factory(monkeypatch)
    received = []
    run = Engine().new_run()
    try:
        await run.make_agent(
            "researcher",
            command_env_allowlist=("SOMEVENDOR_API_KEY",),
            _command_env_names_out=received.append,
        )
        assert calls[0][0].command_env_allowlist == ("SOMEVENDOR_API_KEY",)
        assert received == [frozenset({"GH_TOKEN"})]
    finally:
        await _close(models)


async def test_only_worker_construction_receives_engine_grants(monkeypatch):
    calls, models = _install_factory(monkeypatch)
    engine = CodingEngine(command_env_allowlist=("SOMEVENDOR_API_KEY",), heartbeat_interval_s=None)
    run = engine.new_run()
    old = frozenset({"PREVIOUS_TOKEN"})
    run._worker_command_env_names = old

    def before_complete(branch):
        assert run._worker_command_env_names is old

    run._on_branch_created = before_complete

    async def operate(*args, **kwargs):
        return None

    monkeypatch.setattr(run, "operate_with_repair", operate)
    try:
        await engine._implement(run, WorkPlanned(approach="make the change"))
        assert run._worker_command_env_names == frozenset({"GH_TOKEN"})
        assert calls[0][0].command_env_allowlist == ("SOMEVENDOR_API_KEY",)
        run._on_branch_created = None
        await run.make_agent("researcher")
        assert calls[1][0].command_env_allowlist == ()
        assert calls[1][1]["_command_env_names_out"] is None
    finally:
        await _close(models)


@pytest.mark.parametrize("failure", ["factory", "post-construction"])
async def test_failed_worker_construction_preserves_previous_snapshot(failure, monkeypatch):
    calls, models = _install_factory(monkeypatch, fail=failure == "factory")
    engine = CodingEngine(heartbeat_interval_s=None)
    run = engine.new_run()
    old = frozenset({"PREVIOUS_TOKEN"})
    run._worker_command_env_names = old

    def fail_after_factory(branch):
        raise RuntimeError("session construction failed")

    if failure == "post-construction":
        run._on_branch_created = fail_after_factory
    try:
        with pytest.raises(RuntimeError):
            await engine._implement(run, WorkPlanned(approach="make the change"))
        assert len(calls) == 1
        assert run._worker_command_env_names is old, (
            "a failed make_agent must preserve the snapshot"
        )
    finally:
        await _close(models)


@pytest.mark.parametrize("state", ["owned-defaults", "worker-selected", "explicit-empty"])
async def test_all_engine_command_sinks_use_current_names_and_exact_grants(
    state, monkeypatch, tmp_path
):
    import os

    import lionhub.orchestration.engines.coding as coding
    from lionagi import _command_env_metadata

    engine = CodingEngine(
        auto_repair_cmds=["synthetic-repair"],
        test_timeout_s=3.0,
        command_env_allowlist=("SOMEVENDOR_API_KEY", "LIONAGI_STATE_DB_URL"),
    )
    run = engine.new_run()
    run.workspace = str(tmp_path)
    run.observe(coding.CodingChainEvent, lambda event, context: run.collect(event))
    if state == "worker-selected":
        run._worker_command_env_names = frozenset({"GH_TOKEN", "OWNED_ONLY"})
    elif state == "explicit-empty":
        run._worker_command_env_names = frozenset()
    for name, value in {
        "GH_TOKEN": "synthetic-selected",
        "OWNED_ONLY": "synthetic-owned",
        "SOMEVENDOR_API_KEY": "synthetic-granted",
        "somevendor_api_key": "synthetic-alias",
        "LIONAGI_STATE_DB_URL": "sqlite:///synthetic-parent-store.db",
        "KEEP_VALUE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    metadata_reads = []

    def configured():
        metadata_reads.append(True)
        return frozenset({"OWNED_ONLY"})

    monkeypatch.setattr(_command_env_metadata, "configured_command_env_names", configured)
    calls = []
    status_reads = 0

    async def capture(fn, cmd, shell, timeout, cwd, *, env=None):
        nonlocal status_reads
        assert fn is coding._subprocess_sync
        assert isinstance(env, dict), "every Engine command sink must receive an environment"
        assert env["KEEP_VALUE"] == os.environ["KEEP_VALUE"]
        assert ("GH_TOKEN" in env) is (state != "worker-selected")
        assert ("OWNED_ONLY" in env) is (state == "explicit-empty")
        assert env["SOMEVENDOR_API_KEY"] == "synthetic-granted"
        assert "somevendor_api_key" not in env, "an exact grant must not release a case alias"
        assert "LIONAGI_STATE_DB_URL" not in env
        calls.append((cmd, shell, timeout, cwd, env))
        monkeypatch.setenv("KEEP_VALUE", str(len(calls)))
        stdout = ""
        returncode = 0
        if cmd == ["git", "status", "--porcelain"]:
            status_reads += 1
            stdout = "" if status_reads == 1 else "?? new.txt\n"
        elif cmd == ["git", "diff"]:
            stdout = "tracked diff"
        elif cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
            stdout = "new.txt\n"
        elif cmd[:3] == ["git", "diff", "--no-index"]:
            stdout = "new diff"
            returncode = 1
        return {"returncode": returncode, "stdout": stdout, "stderr": "", "timed_out": False}

    monkeypatch.setattr(coding, "run_sync", capture)
    change = coding.ChangeProposed(summary="a change")
    gate = await engine._run_subprocess_gate(
        run, change, ["synthetic-gate"], round_no=0, notify_event="testing"
    )
    repairs = await engine._apply_auto_repairs(run, round_no=0)
    run._ws_baseline = await engine._capture_ws_baseline(run)
    delta, failed = await engine._workspace_changed(run)
    run._ws_delta = delta
    diff = await engine._capture_diff(run)

    assert gate.passed and len(repairs) == 1
    assert delta == ["new.txt"] and failed is False
    assert diff == "tracked diff\nnew diff"
    assert [(cmd, shell, timeout, cwd) for cmd, shell, timeout, cwd, env in calls] == [
        (["synthetic-gate"], False, 3.0, str(tmp_path)),
        (["synthetic-repair"], False, 120.0, str(tmp_path)),
        (["git", "status", "--porcelain"], False, 30.0, str(tmp_path)),
        (["git", "status", "--porcelain"], False, 30.0, str(tmp_path)),
        (["git", "diff"], False, 30.0, str(tmp_path)),
        (["git", "ls-files", "--others", "--exclude-standard"], False, 30.0, str(tmp_path)),
        (
            ["git", "diff", "--no-index", "--", "/dev/null", str(tmp_path / "new.txt")],
            False,
            30.0,
            str(tmp_path),
        ),
    ]
    assert len({id(env) for cmd, shell, timeout, cwd, env in calls}) == 7
    assert [env["KEEP_VALUE"] for cmd, shell, timeout, cwd, env in calls] == [
        str(i) for i in range(7)
    ]
    assert len(metadata_reads) == (7 if state == "owned-defaults" else 0)

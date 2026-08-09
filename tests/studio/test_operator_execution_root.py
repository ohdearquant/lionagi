import logging
from pathlib import Path

import pytest

from lionagi.studio import config as studio_config
from lionagi.studio.operator import coordinator as coordinator_mod
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.engine import (
    build_operator_branch,
)
from lionagi.studio.operator.store import OperatorStore
from lionagi.studio.operator.types import OperatorEngineTurn


async def _request_permission(*_args):
    raise AssertionError("branch construction cannot request permission")


def _set_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
    execution_root: Path,
) -> None:
    monkeypatch.setattr(
        studio_config,
        "OPERATOR_CWD_DEFAULT",
        execution_root,
        raising=False,
    )


def _turn(*, provider: str, model: str, store_path: Path) -> OperatorEngineTurn:
    return OperatorEngineTurn(
        conversation_id="conversation",
        request_id="request",
        instruction="inspect the project",
        context={},
        history=(),
        request_permission=_request_permission,
        store_path=str(store_path),
        provider=provider,
        model=model,
    )


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("claude_code", "sonnet"),
        ("codex", "gpt-5.3-codex"),
        ("gemini_code", "gemini-3.5-flash"),
    ],
)
def test_operator_branch_forwards_the_resolved_execution_root(
    tmp_path: Path,
    provider: str,
    model: str,
) -> None:
    execution_root = tmp_path / "operator-root"
    execution_root.mkdir()

    branch = build_operator_branch(
        _turn(provider=provider, model=model, store_path=tmp_path / "state.db"),
        execution_root=execution_root,
    )

    configured = branch.chat_model.endpoint.config.kwargs["repo"]
    assert isinstance(configured, str)
    assert Path(configured).resolve() == execution_root.resolve()


@pytest.mark.asyncio
async def test_operator_uses_explicit_root_before_the_selected_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.studio.services import projects

    daemon_cwd = tmp_path / "app"
    execution_root = tmp_path / "operator-root"
    project_root = tmp_path / "selected-project"
    daemon_cwd.mkdir()
    execution_root.mkdir()
    project_root.mkdir()
    monkeypatch.chdir(daemon_cwd)
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(execution_root))
    captured: list[Path] = []

    async def get_project(name: str):
        assert name == "selected"
        return {"name": name, "path": str(project_root)}

    monkeypatch.setattr(projects, "get_project", get_project)

    def stop_after_resolution(turn, *, execution_root: Path | None = None):
        assert turn.context["space"] == "mission"
        if execution_root is not None:
            captured.append(execution_root)
        raise RuntimeError("stop after execution-root resolution")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", stop_after_resolution)
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    try:
        await coordinator.startup()
        conversation_id = (await coordinator.create_conversation(project="selected"))[
            "conversation"
        ]["id"]
        await coordinator.submit(
            conversation_id,
            instruction="inspect this project",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
    finally:
        await coordinator.shutdown()

    assert [path.resolve() for path in captured] == [execution_root.resolve()]
    assert captured[0].resolve() != daemon_cwd.resolve()


@pytest.mark.asyncio
async def test_operator_startup_discloses_the_default_root_and_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_root = tmp_path / "operator-root"
    execution_root.mkdir()
    monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_CWD", raising=False)
    _set_shipped_default(monkeypatch, execution_root)

    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    with caplog.at_level(logging.INFO, logger=coordinator_mod.__name__):
        await coordinator.startup()
    await coordinator.shutdown()

    disclosures = [
        record.getMessage()
        for record in caplog.records
        if "Studio Operator execution root resolved" in record.getMessage()
    ]
    assert len(disclosures) == 1
    assert str(execution_root.resolve()) in disclosures[0]
    assert "daemon-config-default:user-home" in disclosures[0]


@pytest.mark.asyncio
async def test_browser_shaped_projectless_turn_uses_the_startup_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_cwd = tmp_path / "app"
    execution_root = tmp_path / "operator-root"
    daemon_cwd.mkdir()
    execution_root.mkdir()
    monkeypatch.chdir(daemon_cwd)
    monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_CWD", raising=False)
    _set_shipped_default(monkeypatch, execution_root)
    captured: list[Path] = []

    def stop_after_resolution(turn, *, execution_root: Path | None = None):
        assert turn.context["space"] == "mission"
        if execution_root is not None:
            captured.append(execution_root)
        raise RuntimeError("stop after execution-root resolution")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", stop_after_resolution)
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    try:
        await coordinator.startup()
        conversation_id = (await coordinator.create_conversation())["conversation"]["id"]
        await coordinator.submit(
            conversation_id,
            instruction="inspect this project",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
    finally:
        await coordinator.shutdown()

    assert [path.resolve() for path in captured] == [execution_root.resolve()]
    assert captured[0].resolve() != daemon_cwd.resolve()


@pytest.mark.asyncio
async def test_operator_freezes_the_explicit_root_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_root = tmp_path / "startup-root"
    later_root = tmp_path / "later-root"
    startup_root.mkdir()
    later_root.mkdir()
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(startup_root))
    captured: list[Path] = []

    def stop_after_resolution(_turn, *, execution_root: Path | None = None):
        if execution_root is not None:
            captured.append(execution_root)
        raise RuntimeError("stop after execution-root resolution")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", stop_after_resolution)
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    try:
        await coordinator.startup()
        monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(later_root))
        conversation_id = (await coordinator.create_conversation())["conversation"]["id"]
        await coordinator.submit(
            conversation_id,
            instruction="inspect this project",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
    finally:
        await coordinator.shutdown()

    assert [path.resolve() for path in captured] == [startup_root.resolve()]


@pytest.mark.asyncio
async def test_api_created_project_conversation_keeps_its_registered_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.studio.operator.types import CreateConversationRequest
    from lionagi.studio.services import projects
    from lionagi.studio.services.operator import create_operator_conversation

    default_root = tmp_path / "operator-default"
    project_root = tmp_path / "selected-project"
    default_root.mkdir()
    project_root.mkdir()
    monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_CWD", raising=False)
    _set_shipped_default(monkeypatch, default_root)
    captured: list[Path] = []

    async def get_project(name: str):
        assert name == "selected"
        return {"name": name, "path": str(project_root)}

    monkeypatch.setattr(projects, "get_project", get_project)

    def stop_after_resolution(_turn, *, execution_root: Path | None = None):
        if execution_root is not None:
            captured.append(execution_root)
        raise RuntimeError("stop after execution-root resolution")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", stop_after_resolution)
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    monkeypatch.setattr(coordinator_mod, "_COORDINATOR", coordinator)
    try:
        await coordinator.startup()
        conversation_id = (
            await create_operator_conversation(CreateConversationRequest(project="selected"))
        )["conversation"]["id"]
        await coordinator.submit(
            conversation_id,
            instruction="inspect this project",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
    finally:
        await coordinator.shutdown()

    assert [path.resolve() for path in captured] == [project_root.resolve()]
    assert captured[0].resolve() != default_root.resolve()


@pytest.mark.asyncio
async def test_selected_invalid_project_refuses_instead_of_using_the_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.studio.services import projects

    default_root = tmp_path / "operator-default"
    default_root.mkdir()
    monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_CWD", raising=False)
    _set_shipped_default(monkeypatch, default_root)

    async def get_project(name: str):
        assert name == "selected"
        return {"name": name, "path": str(tmp_path / "missing-project")}

    monkeypatch.setattr(projects, "get_project", get_project)

    def unexpected_build(*_args, **_kwargs):
        raise AssertionError("an invalid selected project must fail before branch construction")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", unexpected_build)
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    try:
        await coordinator.startup()
        conversation_id = (await coordinator.create_conversation(project="selected"))[
            "conversation"
        ]["id"]
        await coordinator.submit(
            conversation_id,
            instruction="inspect this project",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        frames = await coordinator.store.list_frames(conversation_id)
    finally:
        await coordinator.shutdown()

    error = next(frame for frame in frames if frame["type"] == "error")["payload"]["error"]
    assert error["code"] == "service_failure"
    assert "project 'selected'" in error["message"]
    assert str(default_root.resolve()) in error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [".", "missing"])
async def test_explicit_invalid_root_refuses_instead_of_using_the_default_or_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    from lionagi.studio.services import projects

    daemon_cwd = tmp_path / "app"
    default_root = tmp_path / "operator-default"
    project_root = tmp_path / "selected-project"
    daemon_cwd.mkdir()
    default_root.mkdir()
    project_root.mkdir()
    monkeypatch.chdir(daemon_cwd)
    _set_shipped_default(monkeypatch, default_root)
    if configured == "missing":
        monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(tmp_path / configured))
    else:
        monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", configured)

    async def get_project(name: str):
        assert name == "selected"
        return {"name": name, "path": str(project_root)}

    monkeypatch.setattr(projects, "get_project", get_project)

    def unexpected_build(*_args, **_kwargs):
        raise AssertionError("an unusable execution root must fail before branch construction")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", unexpected_build)
    coordinator = OperatorCoordinator(store=OperatorStore(tmp_path / "state.db"))
    try:
        await coordinator.startup()
        conversation_id = (await coordinator.create_conversation(project="selected"))[
            "conversation"
        ]["id"]
        await coordinator.submit(
            conversation_id,
            instruction="inspect this project",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        frames = await coordinator.store.list_frames(conversation_id)
    finally:
        await coordinator.shutdown()

    error = next(frame for frame in frames if frame["type"] == "error")["payload"]["error"]
    assert error["code"] == "service_failure"
    assert "LIONAGI_STUDIO_OPERATOR_CWD" in error["message"]
    assert str(daemon_cwd.resolve()) in error["message"]
    assert str(default_root.resolve()) not in error["message"]
    assert str(project_root.resolve()) not in error["message"]
    assert error["retryable"] is False

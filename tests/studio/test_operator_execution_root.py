from pathlib import Path

import pytest

from lionagi.studio.operator import coordinator as coordinator_mod
from lionagi.studio.operator.coordinator import OperatorCoordinator
from lionagi.studio.operator.engine import (
    OperatorExecutionRootError,
    build_operator_branch,
    resolve_operator_execution_root,
)
from lionagi.studio.operator.store import OperatorStore
from lionagi.studio.operator.types import OperatorEngineTurn


async def _request_permission(*_args):
    raise AssertionError("branch construction cannot request permission")


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
    assert Path(configured).resolve() == execution_root.resolve()


@pytest.mark.asyncio
async def test_operator_uses_configured_root_when_no_project_root_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_cwd = tmp_path / "app"
    execution_root = tmp_path / "operator-root"
    daemon_cwd.mkdir()
    execution_root.mkdir()
    monkeypatch.chdir(daemon_cwd)
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(execution_root))
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
async def test_operator_uses_the_selected_projects_registered_root_when_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.studio.services import projects

    project_root = tmp_path / "selected-project"
    project_root.mkdir()
    monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_CWD", raising=False)

    async def get_project(name: str):
        assert name == "selected"
        return {"name": name, "path": str(project_root)}

    monkeypatch.setattr(projects, "get_project", get_project)

    resolved = await resolve_operator_execution_root("selected")

    assert resolved == project_root.resolve()


@pytest.mark.asyncio
async def test_invalid_configured_root_never_falls_back_to_a_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.studio.services import projects

    project_root = tmp_path / "selected-project"
    project_root.mkdir()
    configured = tmp_path / "missing-configured-root"
    monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(configured))

    async def get_project(_name: str):
        return {"name": "selected", "path": str(project_root)}

    monkeypatch.setattr(projects, "get_project", get_project)

    with pytest.raises(OperatorExecutionRootError, match=str(configured)):
        await resolve_operator_execution_root("selected")


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, ".", "missing"])
async def test_operator_refuses_an_unusable_execution_root_legibly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    daemon_cwd = tmp_path / "app"
    daemon_cwd.mkdir()
    monkeypatch.chdir(daemon_cwd)
    if configured is None:
        monkeypatch.delenv("LIONAGI_STUDIO_OPERATOR_CWD", raising=False)
    elif configured == "missing":
        monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", str(tmp_path / configured))
    else:
        monkeypatch.setenv("LIONAGI_STUDIO_OPERATOR_CWD", configured)

    def unexpected_build(*_args, **_kwargs):
        raise AssertionError("an unusable execution root must fail before branch construction")

    monkeypatch.setattr(coordinator_mod, "build_operator_branch", unexpected_build)
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
        frames = await coordinator.store.list_frames(conversation_id)
    finally:
        await coordinator.shutdown()

    error = next(frame for frame in frames if frame["type"] == "error")["payload"]["error"]
    assert error["code"] == "service_failure"
    assert "LIONAGI_STUDIO_OPERATOR_CWD" in error["message"]
    assert str(daemon_cwd.resolve()) in error["message"]
    assert error["retryable"] is False

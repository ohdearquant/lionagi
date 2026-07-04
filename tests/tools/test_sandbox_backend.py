# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for sandbox_backend.py: the ADR-0089 SandboxBackend seam.

No network, no Daytona API calls: the Daytona adapter is exercised with an
injected fake sandbox factory, never the real ``daytona`` package.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lionagi.tools.sandbox_backend import (
    DIFF_ARTIFACT,
    Capabilities,
    Cell,
    CellResult,
    DaytonaBackend,
    ExecutionLimits,
    ExecutionTarget,
    Handle,
    LocalWorktreeBackend,
    ProvisionSpec,
    SandboxBackend,
    SubstrateStreamEvent,
    get_backend,
    select_backend_for_cell,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    cmds = [
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ]
    for cmd in cmds:
        subprocess.run(cmd, cwd=str(path), capture_output=True, check=True)
    (path / "README.md").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path):
    _init_git_repo(tmp_path)
    return tmp_path


class FakeBackend:
    """A minimal in-memory SandboxBackend — no subprocess, no network, no Daytona."""

    def __init__(self, *, hosts_prompt_cell: bool = True) -> None:
        self._hosts_prompt_cell = hosts_prompt_cell
        self.provisioned: list[Handle] = []
        self.torn_down: list[Handle] = []
        self.files: dict[str, dict[str, bytes]] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            cold_start="sub100ms",
            streaming=True,
            mount_or_upload="mount",
            image_build=False,
            hosts_prompt_cell_host_side=self._hosts_prompt_cell,
        )

    async def provision(self, spec: ProvisionSpec) -> Handle:
        handle = Handle(backend="local_worktree", remote_id="fake-1", remote_repo_path="/fake/ws")
        self.files[handle.remote_id] = {}
        self.provisioned.append(handle)
        return handle

    async def run_cell(self, handle, cell, on_event=None) -> CellResult:
        store = self.files[handle.remote_id]
        for rel, content in cell.seed_inputs.items():
            store[rel] = content.encode() if isinstance(content, str) else content
        events = [SubstrateStreamEvent(type="stdout", content=f"ran {cell.entrypoint}")]
        events.append(SubstrateStreamEvent(type="result", metadata={"exit_code": 0}))
        if on_event:
            for ev in events:
                on_event(ev)
        artifacts = await self.collect(handle, cell.artifact_manifest)
        return CellResult(exit_code=0, stdout=events[0].content, artifacts=artifacts, events=events)

    async def collect(self, handle, paths) -> dict[str, bytes]:
        store = self.files[handle.remote_id]
        return {p: store.get(p, b"") for p in paths}

    async def teardown(self, handle) -> None:
        self.torn_down.append(handle)
        self.files.pop(handle.remote_id, None)


class FakeExecOnlyBackend(FakeBackend):
    """Shaped like Daytona: cannot host a prompt-cell's provider call host-side."""

    def __init__(self) -> None:
        super().__init__(hosts_prompt_cell=False)


# ---------------------------------------------------------------------------
# ADR-0079 adopted types
# ---------------------------------------------------------------------------


def test_execution_limits_defaults():
    limits = ExecutionLimits()
    assert limits.timeout_s is None
    assert limits.cpu is None


def test_execution_target_for_worker_preserves_and_overrides():
    base = ExecutionTarget(kind="daytona", cwd="/repo", sandbox_id="sb-1")
    worker = base.for_worker("agent-7", cwd="/repo/agent-7")
    assert worker.kind == "daytona"
    assert worker.cwd == "/repo/agent-7"
    assert worker.sandbox_id == "sb-1"
    assert worker.metadata["agent_id"] == "agent-7"


# ---------------------------------------------------------------------------
# (1) Fake-backend test: provision -> run_cell -> collect -> teardown,
#     for BOTH cell kinds, without launching Daytona.
# ---------------------------------------------------------------------------


async def test_fake_backend_full_lifecycle_prompt_cell():
    backend = FakeBackend()
    handle = await backend.provision(ProvisionSpec(repo_root="/repo"))
    cell = Cell(
        kind="prompt_cell",
        entrypoint="draft-and-score",
        seed_inputs={"input.txt": "seed"},
        artifact_manifest=("input.txt",),
    )
    events: list[SubstrateStreamEvent] = []
    result = await backend.run_cell(handle, cell, on_event=events.append)
    assert result.exit_code == 0
    assert result.artifacts["input.txt"] == b"seed"
    assert any(ev.type == "result" for ev in events)

    collected = await backend.collect(handle, ["input.txt"])
    assert collected == {"input.txt": b"seed"}

    await backend.teardown(handle)
    assert handle in backend.torn_down


async def test_fake_backend_full_lifecycle_exec_cell():
    backend = FakeExecOnlyBackend()
    handle = await backend.provision(ProvisionSpec(repo_root="/repo"))
    cell = Cell(
        kind="exec_cell",
        entrypoint="run-agent-under-test",
        env={"PROVIDER_KEY": "sk-fake"},
        seed_inputs={"task.json": b'{"id": 1}'},
        artifact_manifest=("task.json",),
    )
    result = await backend.run_cell(handle, cell)
    assert result.exit_code == 0
    assert result.artifacts["task.json"] == b'{"id": 1}'

    await backend.teardown(handle)
    assert handle in backend.torn_down
    assert handle.remote_id not in backend.files


# ---------------------------------------------------------------------------
# (2) capabilities()-driven degradation: callers never branch on backend name.
# ---------------------------------------------------------------------------


def test_select_backend_for_cell_degrades_prompt_cell_via_capabilities():
    remote_only = FakeExecOnlyBackend()  # Daytona-shaped: no host-side prompt-cell
    local = FakeBackend()  # can host prompt-cells

    chosen = select_backend_for_cell(
        Cell(kind="prompt_cell", entrypoint="true"), [remote_only, local]
    )
    assert chosen is local


def test_select_backend_for_cell_exec_cell_accepts_any_candidate_order():
    remote_only = FakeExecOnlyBackend()
    local = FakeBackend()

    # exec-cells don't need hosts_prompt_cell_host_side, so the first
    # candidate in the list wins regardless of which one it is.
    assert select_backend_for_cell(
        Cell(kind="exec_cell", entrypoint="true"), [remote_only, local]
    ) is (remote_only)
    assert select_backend_for_cell(
        Cell(kind="exec_cell", entrypoint="true"), [local, remote_only]
    ) is (local)


def test_select_backend_for_cell_raises_when_no_candidate_qualifies():
    remote_only = FakeExecOnlyBackend()
    with pytest.raises(LookupError):
        select_backend_for_cell(Cell(kind="prompt_cell", entrypoint="true"), [remote_only])


def test_real_backends_satisfy_the_protocol_structurally():
    assert isinstance(LocalWorktreeBackend(), SandboxBackend)
    assert isinstance(DaytonaBackend(), SandboxBackend)


def test_get_backend_returns_shared_instances_by_name():
    assert isinstance(get_backend("local_worktree"), LocalWorktreeBackend)
    assert isinstance(get_backend("daytona"), DaytonaBackend)
    assert get_backend("local_worktree") is get_backend("local_worktree")
    with pytest.raises(ValueError):
        get_backend("docker")  # slice 2, not registered yet


# ---------------------------------------------------------------------------
# LocalWorktreeBackend — real git worktree, no network.
# ---------------------------------------------------------------------------


async def test_local_worktree_backend_capabilities_hosts_prompt_cells():
    caps = LocalWorktreeBackend().capabilities()
    assert caps.hosts_prompt_cell_host_side is True
    assert caps.cold_start == "sub100ms"
    assert caps.mount_or_upload == "mount"


async def test_local_worktree_backend_full_lifecycle_prompt_cell(git_repo):
    backend = LocalWorktreeBackend()
    handle = await backend.provision(ProvisionSpec(repo_root=str(git_repo)))
    assert os.path.isdir(handle.remote_repo_path)
    assert handle.backend == "local_worktree"

    cell = Cell(
        kind="prompt_cell",
        entrypoint="echo hello-cell > out.txt",
        artifact_manifest=("out.txt",),
    )
    events: list[SubstrateStreamEvent] = []
    result = await backend.run_cell(handle, cell, on_event=events.append)
    assert result.exit_code == 0
    assert result.artifacts["out.txt"].strip() == b"hello-cell"
    assert any(ev.type == "result" and ev.metadata["exit_code"] == 0 for ev in events)

    await backend.teardown(handle)
    assert not os.path.exists(handle.remote_repo_path)


async def test_local_worktree_backend_exec_cell_with_seed_inputs(git_repo):
    backend = LocalWorktreeBackend()
    handle = await backend.provision(ProvisionSpec(repo_root=str(git_repo)))
    cell = Cell(
        kind="exec_cell",
        entrypoint="cat seed.txt > echoed.txt",
        seed_inputs={"seed.txt": "from-seed\n"},
        artifact_manifest=("echoed.txt",),
    )
    result = await backend.run_cell(handle, cell)
    assert result.artifacts["echoed.txt"] == b"from-seed\n"
    await backend.teardown(handle)


async def test_local_worktree_backend_rejects_env_on_prompt_cell(git_repo):
    backend = LocalWorktreeBackend()
    handle = await backend.provision(ProvisionSpec(repo_root=str(git_repo)))
    cell = Cell(kind="prompt_cell", entrypoint="true", env={"PROVIDER_KEY": "sk-x"})
    with pytest.raises(ValueError, match="prompt-cells"):
        await backend.run_cell(handle, cell)
    await backend.teardown(handle)


async def test_local_worktree_backend_diff_artifact_sentinel(git_repo):
    backend = LocalWorktreeBackend()
    handle = await backend.provision(ProvisionSpec(repo_root=str(git_repo)))
    cell = Cell(kind="prompt_cell", entrypoint="echo changed > tracked.txt")
    await backend.run_cell(handle, cell)
    collected = await backend.collect(handle, [DIFF_ARTIFACT])
    assert b"tracked.txt" in collected[DIFF_ARTIFACT]
    await backend.teardown(handle)


async def test_local_worktree_backend_nonzero_exit_reported(git_repo):
    backend = LocalWorktreeBackend()
    handle = await backend.provision(ProvisionSpec(repo_root=str(git_repo)))
    cell = Cell(kind="exec_cell", entrypoint="exit 3")
    result = await backend.run_cell(handle, cell)
    assert result.exit_code == 3
    await backend.teardown(handle)


# ---------------------------------------------------------------------------
# DaytonaBackend — thin adapter, exercised via an injected fake sandbox.
# ---------------------------------------------------------------------------


class _FakeDaytonaSandbox:
    """Duck-types DaytonaSandbox's surface DaytonaBackend depends on."""

    def __init__(self) -> None:
        self.id = "sandbox-fake-1"
        self._home = "/home/daytona"
        self.cloned: list[tuple] = []
        self.uploaded: dict[str, bytes] = {}
        self.deleted = False
        self.exec_calls: list[dict] = []

    @classmethod
    async def create(cls, **kwargs) -> _FakeDaytonaSandbox:
        inst = cls()
        inst.create_kwargs = kwargs
        return inst

    async def home_dir(self) -> str:
        return self._home

    async def clone(self, url, path, *, commit=None, branch=None, **kw) -> None:
        self.cloned.append((url, path, commit, branch))

    async def upload_bytes(self, data: bytes, dst: str) -> None:
        self.uploaded[dst] = data

    async def download(self, path: str) -> bytes:
        return self.uploaded.get(path, b"")

    async def git_diff(self, repo_path: str, **kw) -> str:
        return f"diff --git a/x b/x\n+ change in {repo_path}\n"

    async def exec_stream(
        self, command, *, cwd=None, env=None, on_stdout=None, on_stderr=None, **kw
    ):
        self.exec_calls.append({"command": command, "cwd": cwd, "env": env})
        if on_stdout:
            on_stdout(f"ran {command}\n")
        return 0

    async def delete(self) -> None:
        self.deleted = True


async def test_daytona_backend_capabilities_cannot_host_prompt_cells():
    caps = DaytonaBackend().capabilities()
    assert caps.hosts_prompt_cell_host_side is False
    assert caps.streaming is True
    assert caps.mount_or_upload == "upload"


async def test_daytona_backend_rejects_prompt_cell():
    backend = DaytonaBackend(create_fn=_FakeDaytonaSandbox.create)
    handle = await backend.provision(ProvisionSpec(repo_root="/repo"))
    with pytest.raises(ValueError, match="prompt-cell"):
        await backend.run_cell(handle, Cell(kind="prompt_cell", entrypoint="true"))


async def test_daytona_backend_full_lifecycle_exec_cell():
    backend = DaytonaBackend(create_fn=_FakeDaytonaSandbox.create)
    handle = await backend.provision(
        ProvisionSpec(repo_root="/repo", repo_url="https://example.com/r.git", ref="main")
    )
    assert handle.backend == "daytona"
    assert handle.remote_id == "sandbox-fake-1"
    assert handle.remote_repo_path == "/home/daytona/repo"
    sandbox: _FakeDaytonaSandbox = handle.metadata["sandbox"]
    assert sandbox.cloned == [("https://example.com/r.git", "/home/daytona/repo", None, "main")]

    events: list[SubstrateStreamEvent] = []
    cell = Cell(
        kind="exec_cell",
        entrypoint="pytest -q",
        env={"DEEPSEEK_API_KEY": "sk-fake"},
        seed_inputs={"conftest.py": "# seeded\n"},
        artifact_manifest=("conftest.py",),
    )
    result = await backend.run_cell(handle, cell, on_event=events.append)
    assert result.exit_code == 0
    assert result.artifacts["conftest.py"] == b"# seeded\n"
    assert sandbox.exec_calls[0]["command"] == "pytest -q"
    assert sandbox.exec_calls[0]["env"] == {"DEEPSEEK_API_KEY": "sk-fake"}
    assert any(ev.type == "result" and ev.metadata["exit_code"] == 0 for ev in events)

    await backend.teardown(handle)
    assert sandbox.deleted is True


async def test_daytona_backend_diff_artifact_sentinel():
    backend = DaytonaBackend(create_fn=_FakeDaytonaSandbox.create)
    handle = await backend.provision(ProvisionSpec(repo_root="/repo"))
    collected = await backend.collect(handle, [DIFF_ARTIFACT])
    assert b"diff --git" in collected[DIFF_ARTIFACT]


async def test_daytona_backend_provision_without_repo_url_uses_home_dir():
    backend = DaytonaBackend(create_fn=_FakeDaytonaSandbox.create)
    handle = await backend.provision(ProvisionSpec(repo_root="/repo"))
    assert handle.remote_repo_path == "/home/daytona"
    sandbox: _FakeDaytonaSandbox = handle.metadata["sandbox"]
    assert sandbox.cloned == []

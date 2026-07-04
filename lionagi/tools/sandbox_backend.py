# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Sandbox backend seam — one contract for provision/run_cell/collect/teardown.

Backend divergence (local worktree vs. Daytona vs. future backends) is absorbed
in ``provision()`` and ``capabilities()``; ``run_cell()``'s signature never
changes per backend. A ``Cell`` is one scored trial and declares a ``kind``:
``prompt_cell`` (the provider call runs host-side, already authenticated; no
secrets cross into the box) or ``exec_cell`` (untrusted code runs inside the
box; secrets are injected explicitly). Callers read ``capabilities()`` to
decide what a backend can do; they never branch on a backend's name.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from lionagi.ln.concurrency import run_sync

from . import sandbox as _worktree
from ._subprocess import _subprocess_sync
from .daytona import DaytonaSandbox

__all__ = (
    "ExecutionLimits",
    "ExecutionTarget",
    "SubstrateStreamEvent",
    "BackendName",
    "CellKind",
    "ColdStartClass",
    "Capabilities",
    "ProvisionSpec",
    "Handle",
    "Cell",
    "CellResult",
    "SandboxBackend",
    "DIFF_ARTIFACT",
    "LocalWorktreeBackend",
    "DaytonaBackend",
    "get_backend",
    "select_backend_for_cell",
)

# ---------------------------------------------------------------------------
# ADR-0079 types, adopted verbatim (frozen, codeless data types).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionLimits:
    """Resource ceiling for one execution; every field is optional/unbounded when None."""

    timeout_s: int | None = None
    cpu: int | None = None
    memory_mb: int | None = None
    disk_mb: int | None = None


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """Where an operation runs — host, a worktree, or a remote sandbox."""

    kind: Literal["host", "local_worktree", "daytona", "remote_agent", "process"] = "host"
    cwd: str | None = None
    repo: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)
    sandbox_id: str | None = None
    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def for_worker(self, agent_id: str, *, cwd: str | None = None) -> ExecutionTarget:
        return ExecutionTarget(
            kind=self.kind,
            cwd=cwd or self.cwd,
            repo=self.repo,
            env=self.env,
            limits=self.limits,
            sandbox_id=self.sandbox_id,
            session_id=self.session_id,
            metadata={**self.metadata, "agent_id": agent_id},
        )


# ---------------------------------------------------------------------------
# ADR-0080's stream event shape, absorbed here (ADR-0089 §4 Lineage).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubstrateStreamEvent:
    """One live event out of ``run_cell``: stdout/stderr/signal/artifact/result/error."""

    type: Literal["stdout", "stderr", "signal", "artifact", "result", "error"]
    content: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# New ADR-0089 seam types.
# ---------------------------------------------------------------------------

BackendName = Literal["local_worktree", "daytona"]
CellKind = Literal["prompt_cell", "exec_cell"]
ColdStartClass = Literal["sub100ms", "seconds", "minutes"]

#: Sentinel artifact name: ``collect(handle, [DIFF_ARTIFACT])`` returns the
#: workspace's unified diff instead of reading a literal file at that path.
#: The same sentinel works across backends so callers never need a
#: backend-specific way to ask for "what changed".
DIFF_ARTIFACT = "__diff__"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a backend can do — the seam callers read instead of branching on backend name."""

    cold_start: ColdStartClass
    streaming: bool
    mount_or_upload: Literal["mount", "upload"]
    image_build: bool
    hosts_prompt_cell_host_side: bool


@dataclass(frozen=True, slots=True)
class ProvisionSpec:
    """Request to provision a workspace. Backends ignore fields they don't need."""

    repo_root: str
    base_branch: str | None = None
    name: str | None = None
    repo_url: str | None = None
    ref: str | None = None
    daytona_snapshot: str | None = None
    daytona_image: Any | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    resources: Mapping[str, int] | None = None


@dataclass(slots=True)
class Handle:
    """State, not behavior — extends ADR-0080's ``SandboxSession`` shape."""

    backend: BackendName
    remote_id: str | None
    remote_repo_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Cell:
    """One scored trial: seed inputs, an entrypoint, and an artifact manifest."""

    kind: CellKind
    entrypoint: str
    seed_inputs: Mapping[str, str | bytes] = field(default_factory=dict)
    artifact_manifest: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: int | None = None


@dataclass(slots=True)
class CellResult:
    """Outcome of one ``run_cell`` call."""

    exit_code: int
    stdout: str
    stderr: str = ""
    artifacts: dict[str, bytes] = field(default_factory=dict)
    events: list[SubstrateStreamEvent] = field(default_factory=list)


@runtime_checkable
class SandboxBackend(Protocol):
    """The one contract every backend implements. Never add a per-backend branch outside it."""

    async def provision(self, spec: ProvisionSpec) -> Handle: ...

    async def run_cell(
        self,
        handle: Handle,
        cell: Cell,
        on_event: Callable[[SubstrateStreamEvent], None] | None = None,
    ) -> CellResult: ...

    async def collect(self, handle: Handle, paths: Sequence[str]) -> dict[str, bytes]: ...

    async def teardown(self, handle: Handle) -> None: ...

    def capabilities(self) -> Capabilities: ...


# ---------------------------------------------------------------------------
# local_worktree backend — wraps sandbox.py's SandboxSession lifecycle.
# ---------------------------------------------------------------------------

#: ``run_cell``'s subprocess never blanket-inherits the host environment (a
#: credential-leak vector: any secret in this process's env would otherwise be
#: visible to the cell's command). Only these host variables are forwarded —
#: enough to resolve the interpreter/tool chain and locate a home directory —
#: plus whatever ``cell.env`` explicitly allow-lists (empty for prompt-cells,
#: enforced below).
_SAFE_ENV_KEYS = ("PATH", "HOME", "PYTHONPATH", "VIRTUAL_ENV")


def _minimal_subprocess_env(cell_env: Mapping[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env.update(cell_env)
    return env


class LocalWorktreeBackend:
    """Git-worktree isolation; ``run_cell`` is a host-side subprocess in the worktree."""

    def capabilities(self) -> Capabilities:
        return Capabilities(
            cold_start="sub100ms",
            streaming=False,
            mount_or_upload="mount",
            image_build=False,
            hosts_prompt_cell_host_side=True,
        )

    async def provision(self, spec: ProvisionSpec) -> Handle:
        session = await _worktree.create_sandbox(
            spec.repo_root, base_branch=spec.base_branch, name=spec.name
        )
        return Handle(
            backend="local_worktree",
            remote_id=None,
            remote_repo_path=session.worktree_path,
            metadata={"session": session},
        )

    async def run_cell(
        self,
        handle: Handle,
        cell: Cell,
        on_event: Callable[[SubstrateStreamEvent], None] | None = None,
    ) -> CellResult:
        if cell.kind == "prompt_cell" and cell.env:
            raise ValueError(
                "prompt-cells never receive env/secrets in the box "
                "(ADR-0089 §3): put provider auth on the host, not on cell.env"
            )

        base = Path(handle.remote_repo_path)
        for rel_path, content in cell.seed_inputs.items():
            dst = base / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8") if isinstance(content, str) else content
            dst.write_bytes(data)

        timeout_s = float(cell.timeout_s or 300)
        env = _minimal_subprocess_env(cell.env)
        result = await run_sync(
            _subprocess_sync, cell.entrypoint, True, timeout_s, str(base), env=env
        )

        events = [SubstrateStreamEvent(type="stdout", content=result["stdout"])]
        if result["stderr"]:
            events.append(SubstrateStreamEvent(type="stderr", content=result["stderr"]))
        events.append(
            SubstrateStreamEvent(type="result", metadata={"exit_code": result["returncode"]})
        )
        if on_event:
            for ev in events:
                on_event(ev)

        artifacts = (
            await self.collect(handle, cell.artifact_manifest) if cell.artifact_manifest else {}
        )
        return CellResult(
            exit_code=result["returncode"],
            stdout=result["stdout"],
            stderr=result["stderr"],
            artifacts=artifacts,
            events=events,
        )

    async def collect(self, handle: Handle, paths: Sequence[str]) -> dict[str, bytes]:
        base = Path(handle.remote_repo_path)
        out: dict[str, bytes] = {}
        for rel in paths:
            if rel == DIFF_ARTIFACT:
                diff = await _worktree.sandbox_diff(handle.metadata["session"])
                out[rel] = diff["patch"].encode("utf-8")
                continue
            fp = base / rel
            out[rel] = fp.read_bytes() if fp.is_file() else b""
        return out

    async def teardown(self, handle: Handle) -> None:
        session = handle.metadata.get("session")
        if session is not None:
            await _worktree.sandbox_discard(session)


# ---------------------------------------------------------------------------
# daytona backend — thin adapter over daytona.py; behavior unchanged there.
# ---------------------------------------------------------------------------


class DaytonaBackend:
    """Wraps ``DaytonaSandbox``: create/exec_stream/download+git_diff/delete map onto the four legs."""

    def __init__(self, *, create_fn: Callable[..., Any] = DaytonaSandbox.create) -> None:
        self._create_fn = create_fn

    def capabilities(self) -> Capabilities:
        return Capabilities(
            cold_start="sub100ms",
            streaming=True,
            mount_or_upload="upload",
            image_build=True,
            hosts_prompt_cell_host_side=False,
        )

    async def provision(self, spec: ProvisionSpec) -> Handle:
        sandbox = await self._create_fn(
            snapshot=spec.daytona_snapshot,
            image=spec.daytona_image,
            env=dict(spec.env) or None,
            resources=dict(spec.resources) if spec.resources else None,
            delete_on_exit=False,
        )
        home = await sandbox.home_dir()
        repo_path = f"{home}/repo" if spec.repo_url else home
        if spec.repo_url:
            ref = spec.ref
            is_sha = (
                bool(ref) and len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower())
            )
            await sandbox.clone(
                spec.repo_url,
                repo_path,
                commit=ref if is_sha else None,
                branch=None if is_sha else ref,
            )
        return Handle(
            backend="daytona",
            remote_id=sandbox.id,
            remote_repo_path=repo_path,
            metadata={"sandbox": sandbox},
        )

    async def run_cell(
        self,
        handle: Handle,
        cell: Cell,
        on_event: Callable[[SubstrateStreamEvent], None] | None = None,
    ) -> CellResult:
        if cell.kind == "prompt_cell":
            raise ValueError(
                "daytona backend cannot host a prompt-cell's provider call host-side "
                "(capabilities().hosts_prompt_cell_host_side is False); "
                "select a backend by capability, not by trying this one first"
            )

        sandbox: DaytonaSandbox = handle.metadata["sandbox"]
        for rel_path, content in cell.seed_inputs.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            await sandbox.upload_bytes(data, f"{handle.remote_repo_path}/{rel_path}")

        events: list[SubstrateStreamEvent] = []
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def _on_stdout(chunk: str) -> None:
            stdout_parts.append(chunk)
            ev = SubstrateStreamEvent(type="stdout", content=chunk)
            events.append(ev)
            if on_event:
                on_event(ev)

        def _on_stderr(chunk: str) -> None:
            stderr_parts.append(chunk)
            ev = SubstrateStreamEvent(type="stderr", content=chunk)
            events.append(ev)
            if on_event:
                on_event(ev)

        exit_code = await sandbox.exec_stream(
            cell.entrypoint,
            cwd=handle.remote_repo_path,
            env=dict(cell.env) or None,
            on_stdout=_on_stdout,
            on_stderr=_on_stderr,
        )
        result_event = SubstrateStreamEvent(type="result", metadata={"exit_code": exit_code})
        events.append(result_event)
        if on_event:
            on_event(result_event)

        artifacts = (
            await self.collect(handle, cell.artifact_manifest) if cell.artifact_manifest else {}
        )
        return CellResult(
            exit_code=exit_code,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            artifacts=artifacts,
            events=events,
        )

    async def collect(self, handle: Handle, paths: Sequence[str]) -> dict[str, bytes]:
        sandbox: DaytonaSandbox = handle.metadata["sandbox"]
        out: dict[str, bytes] = {}
        for rel in paths:
            if rel == DIFF_ARTIFACT:
                diff = await sandbox.git_diff(handle.remote_repo_path)
                out[rel] = diff.encode("utf-8")
                continue
            out[rel] = await sandbox.download(f"{handle.remote_repo_path}/{rel}")
        return out

    async def teardown(self, handle: Handle) -> None:
        sandbox: DaytonaSandbox = handle.metadata["sandbox"]
        await sandbox.delete()


# ---------------------------------------------------------------------------
# Registry + capability-driven selection.
# ---------------------------------------------------------------------------

_BACKENDS: dict[BackendName, SandboxBackend] = {}


def get_backend(name: BackendName) -> SandboxBackend:
    """Return the (lazily constructed, shared) backend instance for ``name``."""
    if name not in _BACKENDS:
        if name == "local_worktree":
            _BACKENDS[name] = LocalWorktreeBackend()
        elif name == "daytona":
            _BACKENDS[name] = DaytonaBackend()
        else:
            raise ValueError(f"unknown sandbox backend: {name!r}")
    return _BACKENDS[name]


def select_backend_for_cell(cell: Cell, candidates: Sequence[SandboxBackend]) -> SandboxBackend:
    """Pick the first candidate whose ``capabilities()`` can host ``cell``.

    Reads ``capabilities()`` only — never a backend's name or type. This is
    the pattern every caller is expected to follow (ADR-0089 §1).
    """
    for backend in candidates:
        caps = backend.capabilities()
        if cell.kind == "prompt_cell" and not caps.hosts_prompt_cell_host_side:
            continue
        return backend
    raise LookupError(f"no candidate backend can host a {cell.kind!r} cell")

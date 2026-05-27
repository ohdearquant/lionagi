# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi/runtime/sandbox.py.

Covers SandboxConfig, SandboxResult, LocalSandboxBackend,
SandboxManager, upload/download round-trip, timeout enforcement,
output truncation, and the SandboxBackend Protocol.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

from lionagi.runtime.sandbox import (
    LocalSandboxBackend,
    SandboxBackend,
    SandboxConfig,
    SandboxManager,
    SandboxResult,
)

# ---------------------------------------------------------------------------
# SandboxConfig
# ---------------------------------------------------------------------------


def test_sandbox_config_defaults():
    cfg = SandboxConfig()
    assert cfg.timeout_seconds == 300
    assert cfg.max_memory_mb is None
    assert cfg.allowed_paths is None
    assert cfg.env_vars is None
    assert cfg.working_dir is None
    assert isinstance(cfg.sandbox_id, str)
    assert len(cfg.sandbox_id) > 0


def test_sandbox_config_explicit_fields():
    cfg = SandboxConfig(
        sandbox_id="test-123",
        timeout_seconds=60,
        max_memory_mb=512,
        allowed_paths=["/tmp", "/home"],
        env_vars={"FOO": "bar"},
        working_dir="/tmp",
    )
    assert cfg.sandbox_id == "test-123"
    assert cfg.timeout_seconds == 60
    assert cfg.max_memory_mb == 512
    assert cfg.allowed_paths == ["/tmp", "/home"]
    assert cfg.env_vars == {"FOO": "bar"}
    assert cfg.working_dir == "/tmp"


def test_sandbox_config_unique_ids():
    ids = {SandboxConfig().sandbox_id for _ in range(10)}
    assert len(ids) == 10


def test_sandbox_config_positive_timeout():
    with pytest.raises(Exception):
        SandboxConfig(timeout_seconds=0)


# ---------------------------------------------------------------------------
# SandboxResult
# ---------------------------------------------------------------------------


def test_sandbox_result_fields():
    result = SandboxResult(
        sandbox_id="s1",
        exit_code=0,
        stdout="hello\n",
        stderr="",
        duration_ms=12.5,
    )
    assert result.sandbox_id == "s1"
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.duration_ms == 12.5
    assert result.artifacts == []
    assert result.truncated is False


def test_sandbox_result_is_frozen():
    result = SandboxResult(sandbox_id="s1", exit_code=0, stdout="", stderr="", duration_ms=1.0)
    with pytest.raises(Exception):
        result.exit_code = 1  # type: ignore[misc]


def test_sandbox_result_with_artifacts_and_truncation():
    result = SandboxResult(
        sandbox_id="s2",
        exit_code=1,
        stdout="x" * 10,
        stderr="err",
        duration_ms=50.0,
        artifacts=["out.txt", "diff.patch"],
        truncated=True,
    )
    assert result.artifacts == ["out.txt", "diff.patch"]
    assert result.truncated is True


# ---------------------------------------------------------------------------
# SandboxBackend Protocol
# ---------------------------------------------------------------------------


def test_sandbox_backend_protocol_isinstance():
    """LocalSandboxBackend must satisfy the runtime_checkable Protocol."""
    backend = LocalSandboxBackend()
    assert isinstance(backend, SandboxBackend)


def test_custom_class_satisfies_protocol():
    """Any class with the correct method signatures satisfies SandboxBackend."""

    class _FakeBackend:
        async def create(self, config: SandboxConfig) -> str:
            return config.sandbox_id

        async def execute(self, sandbox_id: str, command: str, stdin: str = "") -> SandboxResult:
            return SandboxResult(
                sandbox_id=sandbox_id,
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=0.0,
            )

        async def upload(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
            pass

        async def download(self, sandbox_id: str, remote_path: str) -> bytes:
            return b""

        async def destroy(self, sandbox_id: str) -> None:
            pass

        async def is_alive(self, sandbox_id: str) -> bool:
            return True

    assert isinstance(_FakeBackend(), SandboxBackend)


# ---------------------------------------------------------------------------
# LocalSandboxBackend — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_create_and_is_alive():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    assert sandbox_id == cfg.sandbox_id
    assert await backend.is_alive(sandbox_id)
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_destroy_removes_tmpdir():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    entry = backend._sandboxes[sandbox_id]
    tmp_dir = entry.tmp_dir
    assert os.path.isdir(tmp_dir)
    await backend.destroy(sandbox_id)
    assert not os.path.exists(tmp_dir)


@pytest.mark.asyncio
async def test_local_backend_is_alive_after_destroy():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    assert await backend.is_alive(sandbox_id)
    await backend.destroy(sandbox_id)
    assert not await backend.is_alive(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_is_alive_unknown():
    backend = LocalSandboxBackend()
    assert not await backend.is_alive("nonexistent-sandbox")


@pytest.mark.asyncio
async def test_local_backend_destroy_unknown_is_noop():
    """Destroying a non-existent sandbox must not raise."""
    backend = LocalSandboxBackend()
    await backend.destroy("ghost-id")  # no exception


# ---------------------------------------------------------------------------
# LocalSandboxBackend — execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_execute_echo():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_execute_captures_stdout():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "printf 'line1\\nline2\\n'")
    assert "line1" in result.stdout
    assert "line2" in result.stdout
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_execute_captures_stderr():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "echo error_message >&2")
    assert "error_message" in result.stderr
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_execute_exit_code_nonzero():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "exit 42")
    assert result.exit_code == 42
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_execute_exit_code_zero():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "true")
    assert result.exit_code == 0
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_execute_duration_is_positive():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "echo ok")
    assert result.duration_ms > 0
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_execute_with_env_vars():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig(env_vars={"MY_TEST_VAR": "secret_value"})
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "echo $MY_TEST_VAR")
    assert "secret_value" in result.stdout
    await backend.destroy(sandbox_id)


# ---------------------------------------------------------------------------
# LocalSandboxBackend — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_execute_timeout():
    """A command that sleeps beyond the timeout must return exit_code=-1."""
    backend = LocalSandboxBackend()
    cfg = SandboxConfig(timeout_seconds=1)
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "sleep 60")
    assert result.exit_code == -1
    assert "timed out" in result.stderr.lower()
    await backend.destroy(sandbox_id)


# ---------------------------------------------------------------------------
# LocalSandboxBackend — output truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_output_truncation():
    """Large output must be truncated and truncated flag set True."""
    backend = LocalSandboxBackend(output_size_limit=100)
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    # python3 is reliably available via sys.executable
    py = sys.executable
    result = await backend.execute(sandbox_id, f"{py} -c \"print('x'*200)\"")
    assert result.truncated is True
    assert len(result.stdout) <= 100
    await backend.destroy(sandbox_id)


@pytest.mark.asyncio
async def test_local_backend_small_output_not_truncated():
    backend = LocalSandboxBackend(output_size_limit=1024)
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)
    result = await backend.execute(sandbox_id, "echo small")
    assert result.truncated is False
    await backend.destroy(sandbox_id)


# ---------------------------------------------------------------------------
# LocalSandboxBackend — upload / download round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_backend_upload_download_roundtrip():
    backend = LocalSandboxBackend()
    cfg = SandboxConfig()
    sandbox_id = await backend.create(cfg)

    # Write a local temp file to upload.
    fd, local_path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"hello from upload")

        await backend.upload(sandbox_id, local_path, "subdir/test.txt")
        downloaded = await backend.download(sandbox_id, "subdir/test.txt")
    finally:
        if os.path.exists(local_path):
            os.unlink(local_path)

    assert downloaded == b"hello from upload"
    await backend.destroy(sandbox_id)


# ---------------------------------------------------------------------------
# SandboxManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_manager_run_isolated_basic():
    manager = SandboxManager()
    result = await manager.run_isolated("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_manager_run_isolated_with_config():
    cfg = SandboxConfig(timeout_seconds=30)
    manager = SandboxManager()
    result = await manager.run_isolated("echo configured", config=cfg)
    assert result.exit_code == 0
    assert "configured" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_manager_run_script_python():
    manager = SandboxManager()
    py = sys.executable
    result = await manager.run_script("print('from script')", interpreter=py)
    assert result.exit_code == 0
    assert "from script" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_manager_run_script_captures_output():
    manager = SandboxManager()
    py = sys.executable
    script = "import sys; sys.stdout.write('out\\n'); sys.stderr.write('err\\n')"
    result = await manager.run_script(script, interpreter=py)
    assert "out" in result.stdout
    assert "err" in result.stderr


@pytest.mark.asyncio
async def test_sandbox_manager_run_isolated_cleans_up():
    """After run_isolated, manager._active must be empty."""
    manager = SandboxManager()
    await manager.run_isolated("true")
    assert len(manager._active) == 0


@pytest.mark.asyncio
async def test_sandbox_manager_close():
    """close() must not raise even when no sandboxes are active."""
    manager = SandboxManager()
    await manager.run_isolated("echo x")
    await manager.close()
    assert len(manager._active) == 0


@pytest.mark.asyncio
async def test_sandbox_manager_context_manager():
    async with SandboxManager() as manager:
        result = await manager.run_isolated("echo ctx")
    assert result.exit_code == 0
    assert "ctx" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_manager_custom_backend():
    """SandboxManager accepts any SandboxBackend implementation."""

    class _CountingBackend(LocalSandboxBackend):
        execute_count = 0

        async def execute(self, sandbox_id, command, stdin=""):  # noqa: ANN001
            self.__class__.execute_count += 1
            return await super().execute(sandbox_id, command, stdin)

    backend = _CountingBackend()
    manager = SandboxManager(backend=backend)
    await manager.run_isolated("echo backend")
    assert _CountingBackend.execute_count == 1

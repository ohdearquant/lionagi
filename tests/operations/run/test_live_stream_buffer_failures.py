# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A failed diagnostic buffer does not stop a healthy streamed turn."""

from __future__ import annotations

import asyncio
import importlib
from unittest.mock import Mock

import pytest

from lionagi.operations.run.run import RunParam, run
from lionagi.protocols.messages import AssistantResponse
from lionagi.session.branch import Branch
from tests.operations.run.test_live_stream_buffer import _collect, _paused_model

run_mod = importlib.import_module("lionagi.operations.run.run")


@pytest.mark.parametrize(
    "failure",
    ["create", "truncate-open", "truncate-close", "append-open", "append-write", "append-close"],
)
async def test_diagnostic_buffer_io_failure_does_not_interrupt_stream(
    tmp_path, monkeypatch, failure
):
    stream_dir = tmp_path / "stream"
    snapshot_dir = tmp_path / "snapshots"
    paused, release = asyncio.Event(), asyncio.Event()
    release.set()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    real_create = run_mod.acreate_path
    real_open = run_mod.anyio.open_file
    warning = Mock()
    monkeypatch.setattr(run_mod.logger, "warning", warning)
    counts = {"create": 0, "w": 0, "a": 0}
    private = "synthetic-private-path-marker"

    async def create(path, name, *args, **kwargs):
        if str(name).endswith(".buffer.jsonl"):
            counts["create"] += 1
            if failure == "create":
                raise OSError(private)
        return await real_create(path, name, *args, **kwargs)

    class FileProxy:
        def __init__(self, wrapped, mode):
            self.wrapped = wrapped
            self.mode = mode

        async def __aenter__(self):
            await self.wrapped.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            result = await self.wrapped.__aexit__(exc_type, exc, tb)
            if (self.mode, failure) in (("w", "truncate-close"), ("a", "append-close")):
                raise OSError(private)
            return result

        async def write(self, value):
            if self.mode == "a" and failure == "append-write":
                raise OSError(private)
            return await self.wrapped.write(value)

    async def open_file(path, mode="r", *args, **kwargs):
        if str(path).endswith(".buffer.jsonl") and mode in ("w", "a"):
            counts[mode] += 1
            if (mode, failure) in (("w", "truncate-open"), ("a", "append-open")):
                raise OSError(private)
            return FileProxy(await real_open(path, mode, *args, **kwargs), mode)
        return await real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(run_mod, "acreate_path", create)
    monkeypatch.setattr(run_mod.anyio, "open_file", open_file)
    results = await _collect(
        run(
            branch,
            "hello",
            RunParam(stream_persist=True, persist_dir=stream_dir, snapshot_dir=snapshot_dir),
        )
    )
    messages = [item.response for item in results if isinstance(item, AssistantResponse)]
    assert messages == ["first streamed text then more"]
    snapshots = list(snapshot_dir.glob("*.json"))
    assert len(snapshots) == 1
    assert "first streamed text then more" in snapshots[0].read_text()
    assert warning.call_count == 1
    assert private not in repr(warning.call_args)
    assert not list(stream_dir.glob("*.buffer.jsonl"))
    assert counts["create"] == 1
    assert counts["a"] == (1 if failure.startswith("append") else 0)


async def test_snapshot_failure_is_not_treated_as_buffer_failure(tmp_path, monkeypatch):
    paused, release = asyncio.Event(), asyncio.Event()
    release.set()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    warning = Mock()
    monkeypatch.setattr(run_mod.logger, "warning", warning)

    async def fail_snapshot(*_args, **_kwargs):
        raise OSError("snapshot failed")

    monkeypatch.setattr(run_mod, "_write_branch_snapshot", fail_snapshot)
    with pytest.raises(OSError, match="snapshot failed"):
        await _collect(run(branch, "hello", RunParam(stream_persist=True, persist_dir=tmp_path)))
    warning.assert_not_called()


@pytest.mark.parametrize("stage", ["create", "truncate-open", "append-open"])
async def test_cancellation_during_buffer_open_is_not_swallowed(tmp_path, monkeypatch, stage):
    paused, release = asyncio.Event(), asyncio.Event()
    release.set()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    real_create = run_mod.acreate_path
    real_open = run_mod.anyio.open_file
    warning = Mock()
    monkeypatch.setattr(run_mod.logger, "warning", warning)

    async def create(path, name, *args, **kwargs):
        if stage == "create" and str(name).endswith(".buffer.jsonl"):
            raise asyncio.CancelledError
        return await real_create(path, name, *args, **kwargs)

    async def open_file(path, mode="r", *args, **kwargs):
        if str(path).endswith(".buffer.jsonl") and (
            (stage == "truncate-open" and mode == "w") or (stage == "append-open" and mode == "a")
        ):
            raise asyncio.CancelledError
        return await real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(run_mod, "acreate_path", create)
    monkeypatch.setattr(run_mod.anyio, "open_file", open_file)
    with pytest.raises(asyncio.CancelledError):
        await _collect(run(branch, "hello", RunParam(stream_persist=True, persist_dir=tmp_path)))
    warning.assert_not_called()


@pytest.mark.parametrize(
    ("fault", "expected"),
    [(asyncio.CancelledError, asyncio.CancelledError), (RuntimeError, Exception)],
    ids=["cancellation", "runtime-error"],
)
async def test_buffer_close_error_does_not_hide_write_failure(
    tmp_path, monkeypatch, fault, expected
):
    paused, release = asyncio.Event(), asyncio.Event()
    release.set()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    real_open = run_mod.anyio.open_file
    warning = Mock()
    monkeypatch.setattr(run_mod.logger, "warning", warning)

    class CancelThenCloseError:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def __aenter__(self):
            await self.wrapped.__aenter__()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            await self.wrapped.__aexit__(exc_type, exc, tb)
            raise OSError("diagnostic close failed")

        async def write(self, _value):
            raise fault("write failed")

    async def open_file(path, mode="r", *args, **kwargs):
        wrapped = await real_open(path, mode, *args, **kwargs)
        if str(path).endswith(".buffer.jsonl") and mode == "a":
            return CancelThenCloseError(wrapped)
        return wrapped

    monkeypatch.setattr(run_mod.anyio, "open_file", open_file)
    with pytest.raises(expected):
        await _collect(run(branch, "hello", RunParam(stream_persist=True, persist_dir=tmp_path)))
    warning.assert_not_called()


@pytest.mark.parametrize("mode_to_fail", ["w", "a"], ids=["truncate", "append"])
async def test_buffer_io_failure_in_unrelated_exception_context_is_still_best_effort(
    tmp_path, monkeypatch, mode_to_fail
):
    paused, release = asyncio.Event(), asyncio.Event()
    release.set()
    branch = Branch()
    branch.chat_model = _paused_model(paused, release)
    real_open = run_mod.anyio.open_file
    warning = Mock()
    monkeypatch.setattr(run_mod.logger, "warning", warning)

    async def open_file(path, mode="r", *args, **kwargs):
        if str(path).endswith(".buffer.jsonl") and mode == mode_to_fail:
            raise OSError("buffer unavailable")
        return await real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(run_mod.anyio, "open_file", open_file)
    try:
        raise ValueError("unrelated caller exception")
    except ValueError:
        results = await _collect(
            run(branch, "hello", RunParam(stream_persist=True, persist_dir=tmp_path))
        )
    assert [item.response for item in results if isinstance(item, AssistantResponse)] == [
        "first streamed text then more"
    ]
    assert warning.call_count == 1

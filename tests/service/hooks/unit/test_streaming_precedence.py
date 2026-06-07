# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test streaming precedence: hook registry takes priority over streaming_process_func."""

import pytest

from lionagi.service.hooks import HookRegistry
from lionagi.service.imodel import iModel


class TestStreamingPrecedence:
    @pytest.mark.asyncio
    async def test_hook_registry_takes_priority_over_streaming_process_func(self):
        """When a hook handles the chunk, streaming_process_func must not be called."""
        processor_called = []

        async def handler(_event, _chunk_type, chunk, **_kw):
            return {"hooked": True, "original": chunk}

        def processor(chunk):
            processor_called.append(chunk)
            return "PROCESSED"

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            streaming_process_func=processor,
            hook_registry=HookRegistry(stream_handlers={"dict": handler}),
        )

        result = await imodel.process_chunk({"value": "raw"})

        assert result == {"hooked": True, "original": {"value": "raw"}}
        assert processor_called == [], "streaming_process_func must not run when hook handles chunk"

    @pytest.mark.asyncio
    async def test_streaming_process_func_runs_when_no_hook_registered(self):
        """When no hook matches the chunk type, streaming_process_func is called."""
        processor_called = []

        def processor(chunk):
            processor_called.append(chunk)
            return f"processed:{chunk}"

        imodel = iModel(
            provider="openai",
            model="gpt-4.1-mini",
            api_key="test-key",
            streaming_process_func=processor,
        )

        result = await imodel.process_chunk("raw_string")

        assert result == "processed:raw_string"
        assert processor_called == ["raw_string"]

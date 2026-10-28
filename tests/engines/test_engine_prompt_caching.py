# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine(prompt_caching=True): every agent model the engine builds places the
prompt cache when its provider declares prefix caching."""

import os
from unittest.mock import patch

import pytest

from lionhub.orchestration.engines import Engine, ReviewEngine

pytestmark = pytest.mark.asyncio

MODEL = "anthropic/claude-haiku-5-5"


async def _agent(engine: Engine, name: str, model: str = MODEL):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        return await engine.new_run().make_agent("researcher", name=name, model=model)


async def test_engine_wide_prompt_caching_reaches_every_agent():
    engine = Engine(prompt_caching=True)
    for name in ("a", "b"):
        branch = await _agent(engine, name)
        assert branch.chat_model.endpoint.places_prompt_cache is True


async def test_an_engine_without_it_builds_uncached_agents():
    """Control: the default engine changes nothing."""
    branch = await _agent(Engine(), "r1")
    assert branch.chat_model.endpoint.config.prompt_caching is False


async def test_an_engine_with_it_still_builds_cli_agents():
    branch = await _agent(Engine(prompt_caching=True), "r1", model="claude_code/haiku")
    assert branch.chat_model.endpoint.places_prompt_cache is False


async def test_a_subclass_passes_it_through():
    branch = await _agent(ReviewEngine(prompt_caching=True), "r1")
    assert branch.chat_model.endpoint.places_prompt_cache is True

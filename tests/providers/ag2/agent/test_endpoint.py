# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for AG2BetaEndpoint pre-built agent passthrough."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.providers.ag2.agent.models import AG2AgentRequest, AgentConfig

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_fake_agent(name: str = "fake-agent") -> MagicMock:
    """Return a minimal MagicMock that quacks like an autogen.beta.Agent."""
    agent = MagicMock()
    agent.name = name
    return agent


def _make_stream_events(text: str = "hello") -> list[dict[str, Any]]:
    return [{"type": "response", "text": text, "content": None, "typed_result": None}]


async def _fake_run_beta_agent(
    config,
    message,
    llm_config,
    tool_registry=None,
    agent=None,
    *,
    captured: list | None = None,
) -> Any:
    """Coroutine generator that records which agent object was used."""
    if captured is not None:
        captured.append(agent)
    for ev in _make_stream_events(f"response to: {message}"):
        yield ev


# ---------------------------------------------------------------------------
# Model field tests (no AG2 import needed)
# ---------------------------------------------------------------------------


class TestAG2AgentRequestModel:
    def test_agent_field_defaults_to_none(self):
        req = AG2AgentRequest(prompt="hello")
        assert req.agent is None

    def test_agent_field_accepts_arbitrary_object(self):
        fake = _make_fake_agent()
        req = AG2AgentRequest(prompt="hello", agent=fake)
        assert req.agent is fake

    def test_agent_and_agent_config_can_coexist(self):
        """Both fields populated — endpoint decides precedence, model just stores."""
        fake = _make_fake_agent()
        cfg = AgentConfig(name="cfg-agent")
        req = AG2AgentRequest(prompt="hi", agent=fake, agent_config=cfg)
        assert req.agent is fake
        assert req.agent_config is cfg

    def test_agent_config_alone_still_works(self):
        cfg = AgentConfig(name="cfg-only")
        req = AG2AgentRequest(prompt="hi", agent_config=cfg)
        assert req.agent is None
        assert req.agent_config.name == "cfg-only"


# ---------------------------------------------------------------------------
# run_beta_agent signature / routing tests
# ---------------------------------------------------------------------------


class TestRunBetaAgentRouting:
    """Test that run_beta_agent routes correctly without hitting AG2 imports."""

    @pytest.mark.asyncio
    async def test_neither_agent_nor_config_raises(self):
        from lionagi.providers.ag2.agent.models import run_beta_agent

        with pytest.raises(ValueError, match="requires either a pre-built"):
            async for _ in run_beta_agent(config=None, message="hi", llm_config=None, agent=None):
                pass

    @pytest.mark.asyncio
    async def test_prebuilt_agent_with_config_logs_info(self, caplog):
        """When both are supplied the pre-built agent wins and a log is emitted."""
        import logging

        from lionagi.providers.ag2.agent import models as agent_models

        fake = _make_fake_agent()
        cfg = AgentConfig(name="ignored")

        # Patch AG2 stream machinery so the function can run without autogen installed.
        mock_stream = MagicMock()
        mock_stream.subscribe.return_value = MagicMock()
        mock_stream.unsubscribe = MagicMock()

        fake_reply = MagicMock()
        fake_reply.response = MagicMock()
        fake_reply.response.message = MagicMock()
        getattr(fake_reply.response.message, "content", "done")
        fake_reply.response.message.content = "done"
        fake_reply.response.usage = None
        fake.ask = AsyncMock(return_value=fake_reply)

        with patch.dict(
            "sys.modules",
            {
                "autogen": MagicMock(),
                "autogen.beta": MagicMock(),
                "autogen.beta.agent": MagicMock(),
                "autogen.beta.events.tool_events": MagicMock(),
                "autogen.beta.stream": MagicMock(),
                "autogen.beta.events.conditions": MagicMock(),
                "autogen.beta.events.input_events": MagicMock(),
            },
        ):
            import autogen.beta.stream as stream_mod

            stream_mod.MemoryStream.return_value = mock_stream

            import autogen.beta.events.conditions as cond_mod

            cond_mod.TypeCondition = MagicMock(return_value=MagicMock())

            # ToolCallsEvent / ToolResultEvent used in type annotations only here
            import autogen.beta.events.tool_events as te_mod

            te_mod.ToolCallsEvent = MagicMock
            te_mod.ToolResultEvent = MagicMock

            with caplog.at_level(logging.INFO, logger="lionagi.providers.ag2.agent.models"):
                results = []
                async for ev in agent_models.run_beta_agent(
                    config=cfg,
                    message="test",
                    llm_config=MagicMock(),
                    agent=fake,
                ):
                    results.append(ev)

        assert any("agent_config is ignored" in r.message for r in caplog.records), (
            "Expected log message about agent_config being ignored"
        )


# ---------------------------------------------------------------------------
# AG2BetaEndpoint.stream() integration tests (mocked AG2)
# ---------------------------------------------------------------------------


class TestAG2BetaEndpointStream:
    """Test stream() with pre-built agent, using mocked run_beta_agent."""

    def _make_endpoint(self, with_llm_config: bool = False):
        from lionagi.providers.ag2.agent.endpoint import AG2BetaEndpoint
        from lionagi.service.connections import EndpointConfig

        cfg = EndpointConfig(
            name="ag2-beta",
            provider="ag2",
            base_url="",
            endpoint="agent",
            method="stream",
            kwargs={},
        )
        ep = AG2BetaEndpoint(config=cfg)
        if with_llm_config:
            ep._llm_config = {
                "api_type": "openai",
                "model": "gpt-4o-mini",
                "api_key": "test",
            }
        return ep

    @pytest.mark.asyncio
    async def test_prebuilt_agent_used_directly(self):
        """Pre-built agent is forwarded to run_beta_agent without construction."""

        fake_agent = _make_fake_agent("my-agent")
        captured_agents: list = []

        async def mock_run(config, message, llm_config, tool_registry=None, agent=None):
            captured_agents.append(agent)
            yield {
                "type": "response",
                "text": "ok",
                "content": None,
                "typed_result": None,
            }

        with patch("lionagi.providers.ag2.agent.models.run_beta_agent", side_effect=mock_run):
            endpoint = self._make_endpoint()
            chunks = []
            async for chunk in endpoint.stream({"prompt": "hello", "agent": fake_agent}):
                chunks.append(chunk)

        assert len(captured_agents) == 1
        assert captured_agents[0] is fake_agent, "Pre-built agent must be passed through as-is"

    @pytest.mark.asyncio
    async def test_same_agent_instance_reused_across_two_calls(self):
        """Verify the SAME agent object (by id) is used across two stream() calls."""

        fake_agent = _make_fake_agent("reused-agent")
        captured_ids: list[int] = []

        async def mock_run(config, message, llm_config, tool_registry=None, agent=None):
            captured_ids.append(id(agent))
            yield {
                "type": "response",
                "text": f"response to {message}",
                "content": None,
                "typed_result": None,
            }

        with patch("lionagi.providers.ag2.agent.models.run_beta_agent", side_effect=mock_run):
            endpoint = self._make_endpoint()

            # First call
            async for _ in endpoint.stream({"prompt": "first", "agent": fake_agent}):
                pass

            # Second call — same agent object
            async for _ in endpoint.stream({"prompt": "second", "agent": fake_agent}):
                pass

        assert len(captured_ids) == 2, "run_beta_agent should be called once per stream() call"
        assert captured_ids[0] == captured_ids[1], (
            f"Agent id must be identical across calls: {captured_ids[0]} != {captured_ids[1]}"
        )

    @pytest.mark.asyncio
    async def test_agent_wins_over_agent_config(self):
        """When both agent and agent_config are supplied, agent takes precedence."""

        fake_agent = _make_fake_agent("winner-agent")
        cfg = AgentConfig(name="loser-config")
        captured: list[dict] = []

        async def mock_run(config, message, llm_config, tool_registry=None, agent=None):
            captured.append({"config": config, "agent": agent})
            yield {
                "type": "response",
                "text": "ok",
                "content": None,
                "typed_result": None,
            }

        with patch("lionagi.providers.ag2.agent.models.run_beta_agent", side_effect=mock_run):
            endpoint = self._make_endpoint()
            async for _ in endpoint.stream(
                {"prompt": "test", "agent": fake_agent, "agent_config": cfg}
            ):
                pass

        assert len(captured) == 1
        assert captured[0]["agent"] is fake_agent, "Pre-built agent must win over agent_config"

    @pytest.mark.asyncio
    async def test_no_agent_no_config_raises(self):
        """Without agent or agent_config the endpoint should raise ValueError."""

        async def mock_run(config, message, llm_config, tool_registry=None, agent=None):
            if agent is None and config is None:
                raise ValueError("requires either a pre-built 'agent' or a non-None 'config'.")
            yield {
                "type": "response",
                "text": "ok",
                "content": None,
                "typed_result": None,
            }

        with patch("lionagi.providers.ag2.agent.models.run_beta_agent", side_effect=mock_run):
            endpoint = self._make_endpoint()
            # Override _agent_config so create_payload yields no config either
            endpoint._agent_config = {}
            with pytest.raises(ValueError):
                async for _ in endpoint.stream({"prompt": "oops"}):
                    pass

    @pytest.mark.asyncio
    async def test_config_path_unaffected(self):
        """Config-driven path still works exactly as before."""

        captured: list[dict] = []

        async def mock_run(config, message, llm_config, tool_registry=None, agent=None):
            captured.append({"config": config, "agent": agent})
            yield {
                "type": "response",
                "text": "from config",
                "content": None,
                "typed_result": None,
            }

        mock_model_cfg = MagicMock()

        with (
            patch(
                "lionagi.providers.ag2.agent.models.run_beta_agent",
                side_effect=mock_run,
            ),
            patch(
                "lionagi.providers.ag2.agent.endpoint._resolve_model_config",
                return_value=mock_model_cfg,
            ),
        ):
            endpoint = self._make_endpoint(with_llm_config=True)
            endpoint._agent_config = {"name": "config-agent"}
            chunks = []
            async for chunk in endpoint.stream({"prompt": "via config"}):
                chunks.append(chunk)

        assert len(captured) == 1
        assert captured[0]["agent"] is None, "Config path must not pass a pre-built agent"
        assert captured[0]["config"] is not None, "Config path must have a config"

    @pytest.mark.asyncio
    async def test_system_chunk_reflects_prebuilt_agent_name(self):
        """System chunk metadata should show the pre-built agent's name."""

        fake_agent = _make_fake_agent("named-agent")

        async def mock_run(config, message, llm_config, tool_registry=None, agent=None):
            yield {
                "type": "response",
                "text": "ok",
                "content": None,
                "typed_result": None,
            }

        with patch("lionagi.providers.ag2.agent.models.run_beta_agent", side_effect=mock_run):
            endpoint = self._make_endpoint()
            chunks = []
            async for chunk in endpoint.stream({"prompt": "hi", "agent": fake_agent}):
                chunks.append(chunk)

        system_chunks = [c for c in chunks if c.type == "system"]
        assert system_chunks, "Expected at least one system chunk"
        first = system_chunks[0]
        assert first.metadata.get("agent") == "named-agent"
        assert first.metadata.get("pre_built") is True

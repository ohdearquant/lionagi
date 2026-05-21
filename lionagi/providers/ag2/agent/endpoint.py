# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AG2 beta Agent endpoint for lionagi.

Wraps autogen.beta.Agent as a lionagi agentic endpoint.
Events from the beta stream are converted to StreamChunks.

Pre-built agent passthrough
---------------------------
Pass a pre-constructed ``autogen.beta.Agent`` object via
``AG2AgentRequest.agent`` (or the ``"agent"`` key in a dict request) to
bypass the config-driven ``Agent(**kwargs)`` construction that normally
happens on every call.  When ``agent`` is provided it takes precedence over
``agent_config``; ``agent_config`` is silently ignored and a log message is
emitted.  This allows reuse of agents with expensive initialization (custom
tools, observers, populated knowledge stores) across many ``stream()`` calls
while preserving their accumulated state.

When neither ``agent`` nor ``agent_config`` is supplied a ``ValueError`` is
raised, preserving the existing validation behavior.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel

from lionagi.service.connections import AgenticEndpoint, EndpointConfig
from lionagi.service.types import StreamChunk
from lionagi.utils import to_dict

from .._config import AG2Configs

logger = logging.getLogger(__name__)


@AG2Configs.AGENT.register
class AG2BetaEndpoint(AgenticEndpoint):
    """Wraps AG2 beta Agent as a lionagi endpoint.

    Single-agent execution with full middleware stack:
    tools, observers, policies, knowledge, subtasks.
    """

    DEFAULT_CONCURRENCY_LIMIT = 1
    DEFAULT_QUEUE_CAPACITY = 3

    def __init__(self, config: EndpointConfig | None = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._agent_config: dict[str, Any] = kwargs.get("agent_config", {})
        self._llm_config: Any = kwargs.get("llm_config", None)
        self._tool_registry: dict[str, Any] = kwargs.get("tool_registry", {})

    async def _call(self, payload, headers, **kwargs):
        raise NotImplementedError(
            "AG2 beta Agent is stream-only. Use stream() to iterate events."
        )

    def create_payload(self, request: dict | BaseModel, **kwargs):
        from .models import AG2AgentRequest

        req_dict = {**self.config.kwargs, **to_dict(request), **kwargs}
        messages = req_dict.pop("messages", [])
        prompt = req_dict.pop("prompt", "")
        agent_config = req_dict.pop("agent_config", None)
        agent = req_dict.pop("agent", None)
        return {
            "request": AG2AgentRequest(
                messages=messages,
                prompt=prompt,
                agent_config=agent_config,
                agent=agent,
            )
        }, {}

    async def stream(
        self, request: dict | BaseModel, **kwargs
    ) -> AsyncIterator[StreamChunk]:
        from .models import AgentConfig, run_beta_agent

        if isinstance(request, dict) and "request" in request:
            request_obj = request["request"]
        else:
            payload, _ = self.create_payload(request, **kwargs)
            request_obj = payload["request"]

        prompt = request_obj.prompt or (
            request_obj.messages[-1]["content"] if request_obj.messages else ""
        )
        if not prompt:
            raise ValueError(
                "AG2BetaEndpoint requires a non-empty prompt or at least one message."
            )

        # Resolve the pre-built agent (if any) and the agent_config.
        # Pre-built agent takes precedence; agent_config is the fallback.
        prebuilt_agent = request_obj.agent or kwargs.get("agent")

        if prebuilt_agent is None:
            agent_config = request_obj.agent_config
            if agent_config is None:
                agent_config = AgentConfig(
                    **kwargs.get("agent_config", self._agent_config)
                )
        else:
            # Pre-built agent path: agent_config may be None; that's fine.
            agent_config = request_obj.agent_config  # kept for metadata only

        llm_config = kwargs.get("llm_config", self._llm_config)
        tool_registry = kwargs.get("tool_registry", self._tool_registry)

        if llm_config is None and prebuilt_agent is None:
            raise ValueError("AG2BetaEndpoint requires llm_config")

        model_config = (
            _resolve_model_config(llm_config) if llm_config is not None else None
        )

        # Build system chunk metadata. When a pre-built agent is used the
        # config fields are not authoritative; surface what we know.
        if prebuilt_agent is not None:
            agent_name = getattr(prebuilt_agent, "name", "pre-built")
            system_meta: dict = {
                "provider": "ag2",
                "api": "beta",
                "agent": agent_name,
                "pre_built": True,
            }
        else:
            system_meta = {
                "provider": "ag2",
                "api": "beta",
                "agent": agent_config.name,
                "tools": agent_config.tools,
                "observers": agent_config.observers,
                "policies": agent_config.policies,
            }

        yield StreamChunk(type="system", metadata=system_meta)

        # Agent name used in per-event metadata below.
        _agent_name = system_meta["agent"]

        try:
            async for event in run_beta_agent(
                config=agent_config,
                message=prompt,
                llm_config=model_config,
                tool_registry=tool_registry,
                agent=prebuilt_agent,
            ):
                etype = event.get("type")

                if etype == "tool_use":
                    yield StreamChunk(
                        type="tool_use",
                        tool_name=event.get("name"),
                        tool_id=event.get("id"),
                        tool_input=event.get("arguments"),
                        metadata={"agent": _agent_name},
                    )

                elif etype == "tool_result":
                    yield StreamChunk(
                        type="tool_result",
                        tool_output=event.get("content"),
                        metadata={
                            "agent": _agent_name,
                            "tool_name": event.get("name"),
                        },
                    )

                elif etype == "response":
                    content = event.get("text", "")
                    typed_result = event.get("typed_result")

                    yield StreamChunk(
                        type="text",
                        content=content,
                        metadata={
                            "agent": _agent_name,
                            "typed_result": typed_result,
                        },
                    )

        except Exception:
            logger.exception("AG2 beta Agent execution failed")
            raise

        yield StreamChunk(
            type="result",
            content="Agent complete",
            metadata={"agent": _agent_name},
        )


def _resolve_model_config(llm_config: Any) -> Any:
    """Convert a dict llm_config to an AG2 beta ModelConfig."""
    if not isinstance(llm_config, dict):
        return llm_config

    api_type = llm_config.get("api_type", "openai")
    model = llm_config.get("model", "gpt-4o-mini")
    api_key = llm_config.get("api_key")
    temperature = llm_config.get("temperature")

    kwargs = {"model": model}
    if api_key:
        kwargs["api_key"] = api_key
    if temperature is not None:
        kwargs["temperature"] = temperature

    if api_type == "openai":
        from autogen.beta.config.openai.config import OpenAIConfig

        return OpenAIConfig(**kwargs)

    elif api_type == "anthropic":
        from autogen.beta.config.anthropic.config import AnthropicConfig

        return AnthropicConfig(**kwargs)

    elif api_type == "gemini":
        from autogen.beta.config.gemini.config import GeminiConfig

        kwargs.pop("temperature", None)
        return GeminiConfig(**kwargs)

    elif api_type == "ollama":
        from autogen.beta.config.ollama.config import OllamaConfig

        kwargs.pop("api_key", None)
        kwargs.pop("temperature", None)
        return OllamaConfig(**kwargs)

    raise ValueError(f"Unknown api_type: {api_type}")

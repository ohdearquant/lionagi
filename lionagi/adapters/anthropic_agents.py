# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GovernedAnthropicAgent: wrap an Anthropic Agent SDK agent under governance.

The Anthropic Agent SDK must be installed separately (``anthropic[agents]`` or
the standalone SDK when released).  This module never imports it at module level.

Usage::

    from anthropic.agents import Agent  # Anthropic Agent SDK
    from lionagi.adapters.anthropic_agents import GovernedAnthropicAgent

    agent = Agent(...)
    adapter = GovernedAnthropicAgent(agent, charter="policy.yaml", session_id="s1")
    result, cert = await adapter.run("Draft a summary of this document.")
"""

from __future__ import annotations

from typing import Any

from .governed_base import GovernedAdapter

__all__ = ["GovernedAnthropicAgent"]


def _require_anthropic_agents() -> Any:
    """Import the Anthropic Agent SDK or raise a clear ImportError."""
    # The canonical import path may be ``anthropic.agents`` or a top-level
    # ``anthropic_agents`` package depending on the SDK release.  We try
    # both so the adapter works across versions.
    for module_path in ("anthropic.agents", "anthropic_agents"):
        try:
            import importlib

            return importlib.import_module(module_path)
        except ImportError:
            continue
    raise ImportError(
        "GovernedAnthropicAgent requires the Anthropic Agent SDK. "
        'Install it with: uv pip install "anthropic[agents]"'
    )


class GovernedAnthropicAgent(GovernedAdapter):
    """Governed wrapper for an Anthropic Agent SDK ``Agent``.

    Parameters
    ----------
    wrapped:
        An Anthropic Agent SDK ``Agent`` instance.
    charter:
        Optional charter activating governance.
    session_id:
        Identifier embedded in the certificate.
    on_deny:
        ``"raise"`` | ``"skip"`` | ``"log"``
    """

    def _get_tool_name(self) -> str:
        return "anthropic_agents.run"

    async def run(self, query: str, **kwargs: Any) -> tuple[Any, Any]:
        """Run the wrapped Anthropic agent under governance.

        Parameters
        ----------
        query:
            The user prompt or query to pass to the agent.
        **kwargs:
            Additional keyword arguments forwarded to the agent's run method.

        Returns
        -------
        tuple[Any, TaskCertificate | None]
        """
        return await self.execute(query, **kwargs)

    async def _call_wrapped(self, query: str, **kwargs: Any) -> Any:
        _require_anthropic_agents()  # validate SDK is installed
        agent = self._wrapped
        # Anthropic Agent SDK agents expose .run() (sync) or .arun() (async).
        # Prefer async; fall back to sync via asyncio.
        if hasattr(agent, "arun"):
            return await agent.arun(query, **kwargs)
        if hasattr(agent, "run"):
            import asyncio

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: agent.run(query, **kwargs))
        raise AttributeError(
            f"Wrapped Anthropic agent {type(agent).__name__!r} has neither "
            ".arun() nor .run() method."
        )

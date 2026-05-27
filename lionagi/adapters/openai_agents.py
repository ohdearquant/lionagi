# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GovernedOpenAIAgent: wrap an openai-agents Runner or Agent under governance.

The ``openai-agents`` SDK must be installed separately::

    uv pip install openai-agents

This module never imports openai-agents at module level.  The import
happens the first time :meth:`GovernedOpenAIAgent.run` is called so that
the rest of lionagi can be imported on machines without the SDK.

Usage::

    from agents import Agent, Runner  # openai-agents
    from lionagi.adapters.openai_agents import GovernedOpenAIAgent

    agent = Agent(name="demo", instructions="You are helpful.")
    adapter = GovernedOpenAIAgent(agent, charter="policy.yaml", session_id="s1")
    result, cert = await adapter.run("Summarise the meeting notes.")
"""

from __future__ import annotations

from typing import Any

from .governed_base import GovernedAdapter

__all__ = ["GovernedOpenAIAgent"]


def _require_openai_agents() -> Any:
    """Import and return the ``agents`` package or raise a clear error."""
    try:
        import agents  # noqa: F401

        return agents
    except ImportError as exc:
        raise ImportError(
            "GovernedOpenAIAgent requires the openai-agents SDK. "
            'Install it with: uv pip install "openai-agents"'
        ) from exc


class GovernedOpenAIAgent(GovernedAdapter):
    """Governed wrapper for an openai-agents ``Agent`` or ``Runner``.

    The ``wrapped`` argument accepts either an ``agents.Agent`` or an
    ``agents.Runner`` instance.  When an ``Agent`` is passed directly a
    ``Runner`` is constructed implicitly on the first call.

    Parameters
    ----------
    wrapped:
        An ``agents.Agent`` or ``agents.Runner`` instance.
    charter:
        Optional charter activating governance (path, YAML string, or
        ``CharterDocument``).
    session_id:
        Identifier embedded in the certificate.
    on_deny:
        ``"raise"`` | ``"skip"`` | ``"log"``
    """

    def _get_tool_name(self) -> str:
        return "openai_agents.run"

    async def run(self, user_input: str, **kwargs: Any) -> tuple[Any, Any]:
        """Run the wrapped agent/runner under governance.

        Parameters
        ----------
        user_input:
            The text prompt or task description to pass to the agent.
        **kwargs:
            Additional keyword arguments forwarded to ``Runner.run()``.

        Returns
        -------
        tuple[RunResult, TaskCertificate | None]
        """
        return await self.execute(user_input, **kwargs)

    async def _call_wrapped(self, user_input: str, **kwargs: Any) -> Any:
        agents_mod = _require_openai_agents()
        Runner = agents_mod.Runner  # noqa: N806

        wrapped = self._wrapped
        # Accept either Agent or Runner — both use Runner.run()
        result = await Runner.run(wrapped, user_input, **kwargs)
        return result

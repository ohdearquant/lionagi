# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GovernedCrew: wrap a CrewAI ``Crew`` under governance.

CrewAI must be installed separately.  This module never imports it at
module level — the import happens only when :meth:`GovernedCrew.run` is
first called.

Usage::

    from crewai import Crew, Agent, Task
    from lionagi.adapters.crewai import GovernedCrew

    crew = Crew(agents=[...], tasks=[...])
    adapter = GovernedCrew(crew, charter="policy.yaml", session_id="s1")
    result, cert = await adapter.run()
"""

from __future__ import annotations

from typing import Any

from .governed_base import GovernedAdapter

__all__ = ["GovernedCrew"]


def _require_crewai() -> Any:
    """Import crewai or raise a clear ImportError."""
    try:
        import crewai  # noqa: F401

        return crewai
    except ImportError as exc:
        raise ImportError(
            'GovernedCrew requires CrewAI. Install it with: uv pip install "crewai"'
        ) from exc


class GovernedCrew(GovernedAdapter):
    """Governed wrapper for a CrewAI ``Crew``.

    The wrapper calls ``crew.kickoff()`` (sync) via an executor since
    CrewAI does not expose a native async interface.  If a future version
    of CrewAI exposes ``akickoff`` it is preferred automatically.

    Parameters
    ----------
    wrapped:
        A ``crewai.Crew`` instance.
    charter:
        Optional charter activating governance.
    session_id:
        Identifier embedded in the certificate.
    on_deny:
        ``"raise"`` | ``"skip"`` | ``"log"``
    """

    def _get_tool_name(self) -> str:
        return "crewai.crew"

    async def run(self, **kwargs: Any) -> tuple[Any, Any]:
        """Kick off the wrapped crew under governance.

        Parameters
        ----------
        **kwargs:
            Keyword arguments forwarded to ``crew.kickoff()`` (e.g. ``inputs``).

        Returns
        -------
        tuple[CrewOutput, TaskCertificate | None]
        """
        return await self.execute(**kwargs)

    async def _call_wrapped(self, **kwargs: Any) -> Any:
        _require_crewai()  # validate CrewAI is installed
        crew = self._wrapped

        if hasattr(crew, "akickoff"):
            return await crew.akickoff(**kwargs)
        if hasattr(crew, "kickoff"):
            import asyncio

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: crew.kickoff(**kwargs))
        raise AttributeError(
            f"Wrapped CrewAI object {type(crew).__name__!r} has neither .akickoff() nor .kickoff()."
        )

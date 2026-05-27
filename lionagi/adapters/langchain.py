# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GovernedChain: wrap a LangChain Runnable/Chain/Agent under governance.

LangChain must be installed separately.  This module never imports it at
module level — the import happens only when :meth:`GovernedChain.run` is
first called.

Works with any LangChain object that implements the ``Runnable`` interface
(``Chain``, ``AgentExecutor``, ``Retriever``, ``RunnableSequence``, etc.).

Usage::

    from langchain.chains import LLMChain
    from lionagi.adapters.langchain import GovernedChain

    chain = LLMChain(llm=..., prompt=...)
    adapter = GovernedChain(chain, charter="policy.yaml", session_id="s1")
    result, cert = await adapter.run({"question": "What is 2+2?"})
"""

from __future__ import annotations

from typing import Any

from .governed_base import GovernedAdapter

__all__ = ["GovernedChain"]


def _require_langchain() -> Any:
    """Import langchain core or raise a clear ImportError."""
    try:
        import langchain_core  # noqa: F401

        return langchain_core
    except ImportError:
        pass
    try:
        import langchain  # noqa: F401

        return langchain
    except ImportError as exc:
        raise ImportError(
            "GovernedChain requires LangChain. "
            'Install it with: uv pip install "langchain" or "langchain-core"'
        ) from exc


class GovernedChain(GovernedAdapter):
    """Governed wrapper for a LangChain ``Chain``, ``Agent``, or ``Runnable``.

    The wrapper prefers ``ainvoke`` for async execution and falls back to
    the synchronous ``invoke`` run via an executor if only sync is available.

    Parameters
    ----------
    wrapped:
        A LangChain ``Runnable``-compatible object.
    charter:
        Optional charter activating governance.
    session_id:
        Identifier embedded in the certificate.
    on_deny:
        ``"raise"`` | ``"skip"`` | ``"log"``
    """

    def _get_tool_name(self) -> str:
        return "langchain.chain"

    async def run(self, user_input: Any, **kwargs: Any) -> tuple[Any, Any]:
        """Run the wrapped chain/agent under governance.

        Parameters
        ----------
        user_input:
            The input dict, string, or message list accepted by the chain.
        **kwargs:
            Additional keyword arguments forwarded to ``ainvoke`` / ``invoke``.

        Returns
        -------
        tuple[Any, TaskCertificate | None]
        """
        return await self.execute(user_input, **kwargs)

    async def _call_wrapped(self, user_input: Any, **kwargs: Any) -> Any:
        _require_langchain()  # validate LangChain is installed
        chain = self._wrapped

        if hasattr(chain, "ainvoke"):
            return await chain.ainvoke(user_input, **kwargs)
        if hasattr(chain, "invoke"):
            import asyncio

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: chain.invoke(user_input, **kwargs))
        if hasattr(chain, "arun"):
            # Legacy LangChain Chain.arun() API
            if isinstance(user_input, dict):
                return await chain.arun(**user_input, **kwargs)
            return await chain.arun(user_input, **kwargs)
        raise AttributeError(
            f"Wrapped LangChain object {type(chain).__name__!r} has none of "
            ".ainvoke(), .invoke(), or .arun()."
        )

# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GovernedAdapter: base class for zero-rewrite framework adapter wrappers.

Any existing agent framework object (LangChain chain, CrewAI crew,
openai-agents Runner, Anthropic Agent SDK agent, etc.) can be wrapped
in a subclass to gain lionagi governance without rewriting the object.

Governance features (gate checks, evidence recording, certificate minting)
are activated only when ``charter`` is supplied.  Without a charter the
adapter is a pure pass-through — no imports from the governance module are
required and no overhead is added.

Usage::

    class GovernedChain(GovernedAdapter):
        def _get_tool_name(self) -> str:
            return "langchain.chain"

        async def run(self, input, **kwargs):
            return await self.execute(input=input, **kwargs)

    adapter = GovernedChain(my_chain)
    result, cert = await adapter.run("What is 2+2?")
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import warnings
from typing import Any

__all__ = [
    "GovernedAdapter",
    "GovernanceViolationError",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GovernanceViolationError — standalone fallback when governance module absent
# ---------------------------------------------------------------------------


class GovernanceViolationError(Exception):
    """Raised when a governance gate denies an operation.

    When the full governance module is available this class is replaced by
    the canonical ``lionagi.protocols.governance.GovernanceViolationError``
    at import time.  Tests that only need the error class can import from
    this module without requiring the full governance stack.
    """

    def __init__(self, message: str = "Governance gate denied the operation") -> None:
        super().__init__(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_of(value: Any) -> str:
    """Return the SHA-256 hex digest of the JSON-serialised *value*."""
    try:
        serialised = json.dumps(value, sort_keys=True, default=str)
    except Exception:
        serialised = repr(value)
    return hashlib.sha256(serialised.encode()).hexdigest()


# ---------------------------------------------------------------------------
# GovernedAdapter
# ---------------------------------------------------------------------------


class GovernedAdapter:
    """Wrap any agent-framework object and apply optional lionagi governance.

    Parameters
    ----------
    wrapped:
        The framework object to wrap (Chain, Crew, Agent, Runner, …).
    charter:
        Path/string/CharterDocument activating governance.  ``None`` (default)
        disables governance entirely — the adapter is a transparent pass-through.
    session_id:
        Caller-supplied session identifier embedded in the certificate.
        Defaults to the empty string.
    on_deny:
        What to do when a governance gate denies the operation.

        ``"raise"`` (default) — raise ``GovernanceViolationError``.
        ``"skip"``            — return ``(None, None)`` silently.
        ``"log"``             — emit a warning and continue execution.
    """

    def __init__(
        self,
        wrapped: Any,
        charter: Any = None,
        session_id: str = "",
        on_deny: str = "raise",
    ) -> None:
        if on_deny not in ("raise", "skip", "log"):
            raise ValueError(f"on_deny must be 'raise', 'skip', or 'log'; got {on_deny!r}")
        self._wrapped = wrapped
        self._on_deny = on_deny
        self._session_id = session_id
        self._controller: Any = None  # GovernedFlowController or None

        if charter is not None:
            try:
                from lionagi.protocols.governance.flow_integration import (
                    GovernedFlowController,
                )

                self._controller = GovernedFlowController(charter=charter, session_id=session_id)
            except ImportError as exc:
                raise ImportError(
                    "Governance features require the lionagi governance module. "
                    "Ensure you are using a lionagi version that includes "
                    "lionagi.protocols.governance (>=0.27.0)."
                ) from exc

    # ------------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------------

    async def execute(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
        """Execute the wrapped object under governance and return (result, cert).

        The certificate is a ``TaskCertificate`` when a charter is active,
        ``None`` otherwise.

        Subclasses override ``_call_wrapped`` to invoke their specific
        framework object.  They may also override ``execute`` directly, but
        calling ``super().execute()`` is the recommended pattern.
        """
        tool_name = self._get_tool_name()
        args_hash = _sha256_of({"args": args, "kwargs": kwargs})

        gate_result = self._pre_op_check(tool_name)
        if gate_result is not None and self._is_denied(gate_result):
            if self._on_deny == "log":
                warnings.warn(
                    f"Governance gate denied operation '{tool_name}' "
                    f"(gate={getattr(gate_result, 'gate_id', '?')}); "
                    f"continuing due to on_deny='log'.",
                    stacklevel=2,
                )
                # Fall through to execution below
            else:
                return self._handle_deny(gate_result)

        t0 = time.perf_counter()
        result = await self._call_wrapped(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        result_hash = _sha256_of(result)
        self._post_op_record(tool_name, args_hash, result_hash, gate_result, elapsed_ms)

        cert = self._mint_certificate()
        return result, cert

    async def _call_wrapped(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped object.  Override in subclasses."""
        raise NotImplementedError(f"{type(self).__name__} must implement _call_wrapped()")

    # ------------------------------------------------------------------
    # Extension points
    # ------------------------------------------------------------------

    def _get_tool_name(self) -> str:
        """Return the tool-name string used for gate registration lookup."""
        return f"governed.{type(self._wrapped).__name__.lower()}"

    def _hash_args(self, args: tuple, kwargs: dict) -> str:
        """SHA-256 of the serialised args/kwargs."""
        return _sha256_of({"args": args, "kwargs": kwargs})

    def _hash_result(self, result: Any) -> str:
        """SHA-256 of the serialised result."""
        return _sha256_of(result)

    # ------------------------------------------------------------------
    # Internal governance helpers
    # ------------------------------------------------------------------

    def _pre_op_check(self, tool_name: str) -> Any:
        """Return a GateResult or None when no controller is active."""
        if self._controller is None:
            return None
        return self._controller.pre_op_check(tool_name)

    def _is_denied(self, gate_result: Any) -> bool:
        """Return True when gate_result represents a hard denial."""
        try:
            from lionagi.protocols.governance.gates import GateVerdict

            return gate_result.verdict == GateVerdict.DENY
        except ImportError:
            return False

    def _handle_deny(self, gate_result: Any) -> tuple[Any, Any]:
        """Apply on_deny policy for a hard denial."""
        if self._on_deny == "raise":
            msg = (
                f"Gate {getattr(gate_result, 'gate_id', '?')} denied: "
                f"{getattr(gate_result, 'justification', 'governance denied')}"
            )
            raise GovernanceViolationError(msg)
        if self._on_deny == "skip":
            return None, None
        # "log"
        warnings.warn(
            f"Governance gate denied operation '{self._get_tool_name()}' "
            f"(gate={getattr(gate_result, 'gate_id', '?')}); continuing due to on_deny='log'.",
            stacklevel=4,
        )
        return None, None  # caller must handle None result

    def _post_op_record(
        self,
        tool_name: str,
        args_hash: str,
        result_hash: str,
        gate_result: Any,
        elapsed_ms: float,
    ) -> None:
        """Record the operation in the evidence chain (no-op without controller)."""
        if self._controller is None:
            return
        # Provide a passthrough GateResult when governance was skipped
        if gate_result is None:
            try:
                from lionagi.protocols.governance.gates import GateResult, GateVerdict

                gate_result = GateResult(
                    verdict=GateVerdict.ALLOW,
                    justification="No charter active",
                    gate_id="",
                )
            except ImportError:
                return
        self._controller.post_op_record(tool_name, args_hash, result_hash, gate_result, elapsed_ms)

    def _mint_certificate(self) -> Any:
        """Mint a TaskCertificate or return None when no controller is active."""
        if self._controller is None:
            return None
        return self._controller.mint_certificate()

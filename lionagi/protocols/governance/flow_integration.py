# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GovernedFlowController: governance integration point for Session.flow() (P18).

Usage pattern (context manager — preferred)::

    with GovernedFlowController(charter="path/to/charter.yaml", session_id=sid) as ctrl:
        result = ctrl.pre_op_check(tool_name, ctx)
        ctrl.post_op_record(tool_name, args_hash, result_hash, result, elapsed_ms)
        cert = ctrl.mint_certificate()

Usage pattern (manual lifecycle)::

    controller = GovernedFlowController(charter="path/to/charter.yaml", session_id=session_id)
    try:
        # Before each op:
        result = controller.pre_op_check(tool_name, ctx)
        # After each op:
        controller.post_op_record(tool_name, args_hash, result_hash, result, elapsed_ms)
        # At completion:
        cert = controller.mint_certificate()
    finally:
        controller.close()

If *charter* is None the controller is a no-op pass-through (backward compat).
``close()`` is idempotent and safe to call multiple times.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lionagi.protocols.governance.certificate import CertificateGrade, TaskCertificate
from lionagi.protocols.governance.compiler import CharterCompiler, CompilationResult
from lionagi.protocols.governance.context import (
    OperationContext,
    set_operation_context,
)
from lionagi.protocols.governance.context import (
    PolicyPin as ContextPolicyPin,
)
from lionagi.protocols.governance.dsl import CharterDocument
from lionagi.protocols.governance.evidence import EvidenceChain, LogTier
from lionagi.protocols.governance.gates import GateExecutor, GateResult, GateVerdict

__all__ = ["GovernedFlowController"]


def _hash_str(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class GovernedFlowController:
    """Controls governance for a single flow execution.

    Parameters
    ----------
    charter:
        A CharterDocument, a YAML string, or a path (str/Path) to a YAML file.
        Pass None to create a no-op controller (backward compatibility).
    session_id:
        Caller-supplied session identifier embedded in the certificate.
    """

    def __init__(
        self,
        charter: CharterDocument | str | Path | None,
        session_id: str,
    ) -> None:
        self._session_id = session_id
        self._started_at: datetime = datetime.now(tz=timezone.utc)
        self._op_count: int = 0
        self._ops_allowed: int = 0
        self._gate_results_summary: dict[str, int] = {}
        self._evidence_chain: EvidenceChain | None = None
        self._compilation: CompilationResult | None = None
        self._charter_doc: CharterDocument | None = None
        self._gate_executor: GateExecutor | None = None
        self._ctx_token = None

        if charter is None:
            # No-op mode — governance is skipped entirely.
            return

        # Parse charter if it's not already a CharterDocument.
        if isinstance(charter, CharterDocument):
            self._charter_doc = charter
        else:
            from lionagi.protocols.governance.charter import parse_charter

            self._charter_doc = parse_charter(charter)

        # Compile charter to runtime targets.
        compiler = CharterCompiler()
        self._compilation = compiler.compile(self._charter_doc)

        # Set up the gate executor.
        self._gate_executor = GateExecutor(self._compilation.gates)

        # Set up evidence chain.
        self._evidence_chain = EvidenceChain()

        # Set up OperationContext so context-aware gates can access it.
        pin = self._compilation.policy_pin
        ctx_pin = ContextPolicyPin(
            charter_id=self._charter_doc.metadata.charter_id,
            charter_version=pin.version if pin else "1.0",
            charter_hash=pin.charter_hash if pin else "",
            pinned_at=self._started_at,
        )
        op_ctx = OperationContext(
            actor_id=session_id,
            actor_role="session",
            policy_pin=ctx_pin,
            trace_id=uuid.uuid4().hex,
            span_id=uuid.uuid4().hex,
        )
        self._ctx_token = set_operation_context(op_ctx)

    # ── Pre-op ────────────────────────────────────────────────────────────

    def pre_op_check(self, tool_name: str, ctx: Any = None) -> GateResult:
        """Check budget and gates before executing an operation.

        Returns a GateResult.  Callers MUST check GateVerdict and decide
        whether to raise GovernanceViolationError themselves, or they can
        call ``GovernanceViolationError(result)`` directly on DENY.

        If no charter is active, always returns ALLOW.
        """
        if self._gate_executor is None:
            return GateResult(
                verdict=GateVerdict.ALLOW,
                justification="No charter active — governance skipped",
                gate_id="",
            )

        gate_result = self._gate_executor.evaluate(tool_name, ctx)
        return gate_result

    # ── Post-op ───────────────────────────────────────────────────────────

    def post_op_record(
        self,
        tool_name: str,
        args_hash: str,
        result_hash: str,
        gate_result: GateResult,
        elapsed_ms: float = 0.0,
    ) -> None:
        """Record an evidence sidecar node after a completed operation.

        Parameters
        ----------
        tool_name:
            Name of the tool / operation that was executed.
        args_hash:
            SHA-256 hex of the serialised arguments (caller responsibility).
        result_hash:
            SHA-256 hex of the serialised result (caller responsibility).
        gate_result:
            The GateResult from the preceding pre_op_check call.
        elapsed_ms:
            Wall-clock duration of the operation in milliseconds.
        """
        self._op_count += 1
        verdict = gate_result.verdict.value
        self._gate_results_summary[verdict] = self._gate_results_summary.get(verdict, 0) + 1
        if gate_result.verdict == GateVerdict.ALLOW:
            self._ops_allowed += 1

        if self._evidence_chain is None:
            return

        self._evidence_chain.append(
            content={
                "op_index": self._op_count,
                "tool_name": tool_name,
                "args_hash": args_hash,
                "result_hash": result_hash,
                "verdict": verdict,
                "gate_id": gate_result.gate_id,
                "elapsed_ms": elapsed_ms,
            },
            tier=LogTier.IMMUTABLE,
        )

    # ── Certificate minting ───────────────────────────────────────────────

    def mint_certificate(self) -> TaskCertificate:
        """Mint and return a TaskCertificate at the end of the flow.

        Grade computation:
          - FAILED  if any hard denial occurred (deny count > 0)
          - PARTIAL if advisory gate(s) fired but no hard denial
          - FULL    if zero denials and zero advisories (or no gates)
        """
        deny_count = self._gate_results_summary.get("deny", 0)
        advisory_count = self._gate_results_summary.get("advisory", 0)

        if deny_count > 0:
            grade = CertificateGrade.FAILED
        elif advisory_count > 0:
            grade = CertificateGrade.PARTIAL
        else:
            grade = CertificateGrade.FULL

        charter_id = self._charter_doc.metadata.charter_id if self._charter_doc is not None else ""
        charter_hash = (
            self._compilation.policy_pin.charter_hash
            if self._compilation is not None and self._compilation.policy_pin is not None
            else ""
        )
        evidence_head = self._evidence_chain.head_hash() if self._evidence_chain is not None else ""

        return TaskCertificate(
            session_id=self._session_id,
            charter_id=charter_id,
            charter_hash=charter_hash,
            grade=grade,
            evidence_chain_head=evidence_head,
            started_at=self._started_at,
            completed_at=datetime.now(tz=timezone.utc),
            op_count=self._op_count,
            ops_allowed=self._ops_allowed,
            gate_results_summary=dict(self._gate_results_summary),
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Reset the OperationContext ContextVar set during construction.

        Idempotent: safe to call multiple times.  Must be called when the
        controller is no longer in scope so the context does not leak to
        subsequent operations on the same thread/task.
        """
        if self._ctx_token is not None:
            from lionagi.protocols.governance.context import _operation_context_var

            _operation_context_var.reset(self._ctx_token)
            self._ctx_token = None

    def __enter__(self) -> GovernedFlowController:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""GoverningContext: runtime gate + evidence handle for one governed task run."""

from __future__ import annotations

from datetime import datetime, timezone

from lionagi.governance.certificate import TaskCertificate
from lionagi.governance.errors import GovernanceViolationError
from lionagi.governance.evidence import EvidenceChain, LogTier
from lionagi.governance.gates import GateExecutor, GatePolicy, GateResult, GateVerdict

__all__ = [
    "GoverningContext",
]


class GoverningContext:
    """Runtime governance handle: evaluate gates and record evidence for one task.

    Usage::

        ctx = GoverningContext(task_id="run-42", policies=[...])
        result = ctx.check("my_tool")          # raises GovernanceViolationError on hard DENY
        ctx.record({"event": "tool_called"})
        cert = ctx.complete()
    """

    def __init__(
        self,
        task_id: str,
        policies: list[GatePolicy] | None = None,
        *,
        raise_on_deny: bool = True,
    ) -> None:
        self.task_id = task_id
        self.raise_on_deny = raise_on_deny
        self._executor = GateExecutor(policies or [])
        self._chain = EvidenceChain()
        self._started_at: datetime = datetime.now(tz=timezone.utc)
        self._op_count: int = 0
        self._ops_allowed: int = 0
        self._ops_denied: int = 0
        self._ops_advisory: int = 0
        self._gate_tally: dict[str, int] = {}

    def check(self, tool_name: str, ctx: object = None) -> GateResult:
        """Evaluate all policies for *tool_name*.

        Appends the result to the evidence chain.  Raises
        GovernanceViolationError when the verdict is DENY and
        *raise_on_deny* is True; otherwise returns the result.

        Also emits a GateDenied signal if a branch is provided via *ctx*
        (duck-typed: ``ctx.emit`` must exist).
        """
        result = self._executor.evaluate(tool_name, ctx)
        self._op_count += 1
        verdict = result.verdict

        if verdict is GateVerdict.ALLOW:
            self._ops_allowed += 1
        elif verdict is GateVerdict.ADVISORY:
            self._ops_advisory += 1
        else:
            self._ops_denied += 1

        gate_id = result.gate_id or "_allow"
        self._gate_tally[gate_id] = self._gate_tally.get(gate_id, 0) + 1

        self._chain.append(result.to_evidence_dict(), tier=LogTier.IMMUTABLE)

        if verdict is GateVerdict.DENY:
            self._emit_gate_denied(tool_name, result, ctx)
            if self.raise_on_deny:
                raise GovernanceViolationError(result)

        return result

    def record(self, content: dict, *, tier: LogTier = LogTier.IMMUTABLE) -> None:
        """Append an arbitrary evidence entry to the chain."""
        self._chain.append(content, tier=tier)

    def complete(self) -> TaskCertificate:
        """Mint a TaskCertificate from the accumulated run state."""
        return TaskCertificate.mint(
            task_id=self.task_id,
            evidence_chain_head=self._chain.head_hash(),
            started_at=self._started_at,
            completed_at=datetime.now(tz=timezone.utc),
            op_count=self._op_count,
            ops_allowed=self._ops_allowed,
            ops_denied=self._ops_denied,
            ops_advisory=self._ops_advisory,
            gate_results_summary=dict(self._gate_tally),
        )

    @property
    def evidence_chain(self) -> EvidenceChain:
        return self._chain

    @staticmethod
    def _emit_gate_denied(tool_name: str, result: GateResult, ctx: object) -> None:
        if ctx is None or not hasattr(ctx, "emit"):
            return
        from lionagi.session.signal import GateDenied  # noqa: PLC0415

        try:
            import asyncio  # noqa: PLC0415

            signal = GateDenied(
                data={
                    "tool_name": tool_name,
                    "gate_id": result.gate_id,
                    "justification": result.justification,
                },
                emitter_role="governance",
            )
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(ctx.emit(signal))  # type: ignore[attr-defined]
        except Exception:  # noqa: S110 — best-effort fire-and-forget; never block the caller
            pass

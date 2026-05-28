# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""TaskCertificate: minted at the end of a governed flow execution (P18).

A TaskCertificate records the auditable outcome of a Session.flow() run
that operated under a CharterDocument.  Grades are:

  FULL    — zero hard denials, all operations completed
  PARTIAL — advisory-only issues (soft gates fired, no hard denial)
  FAILED  — at least one hard denial blocked an operation
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "CertificateGrade",
    "TaskCertificate",
]


class CertificateGrade(str, enum.Enum):
    FULL = "full"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskCertificate(BaseModel):
    """Auditable certificate minted when a governed flow completes.

    Attributes:
        certificate_id:        Unique hex identifier for this certificate.
        session_id:            The session that produced this certificate.
        charter_id:            Identifier from CharterDocument.metadata.charter_id.
        charter_hash:          SHA-256 hex of the compiled charter JSON.
        grade:                 Outcome grade computed from gate results.
        evidence_chain_head:   Tip hash of the evidence chain at completion.
        started_at:            UTC datetime when governance was initialised.
        completed_at:          UTC datetime when the certificate was minted.
        op_count:              Number of operations executed.
        ops_allowed:           Number of operations that received ALLOW verdict.
        gate_results_summary:  Counts by verdict string (allow/advisory/deny).
    """

    certificate_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str
    charter_id: str
    charter_hash: str
    grade: CertificateGrade
    evidence_chain_head: str
    started_at: datetime
    completed_at: datetime
    op_count: int
    ops_allowed: int
    gate_results_summary: dict[str, int] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict with ISO-formatted datetimes."""
        return {
            "certificate_id": self.certificate_id,
            "session_id": self.session_id,
            "charter_id": self.charter_id,
            "charter_hash": self.charter_hash,
            "grade": self.grade.value,
            "evidence_chain_head": self.evidence_chain_head,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "op_count": self.op_count,
            "ops_allowed": self.ops_allowed,
            "gate_results_summary": self.gate_results_summary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskCertificate:
        """Deserialise from a plain dict (as produced by to_dict)."""
        return cls(
            certificate_id=d["certificate_id"],
            session_id=d["session_id"],
            charter_id=d["charter_id"],
            charter_hash=d["charter_hash"],
            grade=CertificateGrade(d["grade"]),
            evidence_chain_head=d["evidence_chain_head"],
            started_at=datetime.fromisoformat(d["started_at"]),
            completed_at=datetime.fromisoformat(d["completed_at"]),
            op_count=d["op_count"],
            ops_allowed=d.get("ops_allowed", 0),
            gate_results_summary=d.get("gate_results_summary", {}),
        )

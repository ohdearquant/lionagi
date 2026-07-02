# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""TaskCertificate: auditable outcome record produced when a governed task completes."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import Field

from lionagi.protocols.generic.element import Element

__all__ = [
    "CertificateGrade",
    "TaskCertificate",
]


class CertificateGrade(str, enum.Enum):
    FULL = "full"  # zero hard denials
    PARTIAL = "partial"  # advisory flags only
    FAILED = "failed"  # at least one hard denial


class TaskCertificate(Element):
    """Immutable certificate minted when a governed task run finishes.

    Grades: FULL (clean), PARTIAL (advisories only), FAILED (hard denial hit).
    """

    task_id: str
    grade: CertificateGrade
    evidence_chain_head: str
    started_at: datetime
    completed_at: datetime
    op_count: int = 0
    ops_allowed: int = 0
    ops_denied: int = 0
    ops_advisory: int = 0
    chain_verified: bool = True
    gate_results_summary: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def mint(
        cls,
        *,
        task_id: str,
        evidence_chain_head: str,
        started_at: datetime,
        completed_at: datetime,
        op_count: int,
        ops_allowed: int,
        ops_denied: int,
        ops_advisory: int,
        chain_verified: bool = True,
        gate_results_summary: dict[str, int] | None = None,
    ) -> TaskCertificate:
        if not chain_verified or ops_denied > 0:
            grade = CertificateGrade.FAILED
        elif ops_advisory > 0:
            grade = CertificateGrade.PARTIAL
        else:
            grade = CertificateGrade.FULL
        return cls(
            task_id=task_id,
            grade=grade,
            evidence_chain_head=evidence_chain_head,
            started_at=started_at,
            completed_at=completed_at,
            op_count=op_count,
            ops_allowed=ops_allowed,
            ops_denied=ops_denied,
            ops_advisory=ops_advisory,
            chain_verified=chain_verified,
            gate_results_summary=gate_results_summary or {},
        )

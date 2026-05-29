# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""TaskCertificate: auditable outcome of a governed flow execution."""

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
    FULL = "full"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskCertificate(Element):
    """Auditable certificate minted when a governed flow completes.

    Grades: FULL (zero denials), PARTIAL (advisory only), FAILED (hard denial).
    """

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

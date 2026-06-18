# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionagi.governance — minimal gates, evidence chain, and TaskCertificate.

Public surface (in scope):
  - GatePolicy / GateExecutor / GateVerdict / GateResult / Enforcement
  - EvidenceChain / EvidenceNode / ChainVerification / ChainVerifier
  - TaskCertificate / CertificateGrade
  - GoverningContext  (binds the above into one per-run handle)
  - GovernanceViolationError

Out of scope at library level: charter DSL, SoD, JIT grants, break-glass.
"""

from lionagi.governance.certificate import CertificateGrade, TaskCertificate
from lionagi.governance.context import GoverningContext
from lionagi.governance.errors import GovernanceViolationError
from lionagi.governance.evidence import (
    GENESIS_HASH,
    ChainVerification,
    ChainVerifier,
    EvidenceChain,
    EvidenceNode,
    LogTier,
    compute_node_hash,
)
from lionagi.governance.gates import (
    Enforcement,
    GateExecutor,
    GatePolicy,
    GateResult,
    GateVerdict,
)

__all__ = [
    "GENESIS_HASH",
    "CertificateGrade",
    "ChainVerification",
    "ChainVerifier",
    "Enforcement",
    "EvidenceChain",
    "EvidenceNode",
    "GateExecutor",
    "GatePolicy",
    "GateResult",
    "GateVerdict",
    "GovernanceViolationError",
    "GoverningContext",
    "LogTier",
    "TaskCertificate",
    "compute_node_hash",
]

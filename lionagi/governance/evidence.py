# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Append-only hash-linked evidence chain for governance audit trails."""

from __future__ import annotations

import hashlib
import json
import threading
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lionagi.protocols.generic.element import Element

__all__ = [
    "GENESIS_HASH",
    "ChainVerification",
    "ChainVerifier",
    "EvidenceChain",
    "EvidenceNode",
    "LogTier",
    "compute_node_hash",
]

GENESIS_HASH = "0" * 64


def _canonical_json(content: dict[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_node_hash(content: dict[str, Any], previous_hash: str) -> str:
    return _sha256_hex(_canonical_json(content) + "|" + previous_hash)


class LogTier(str, Enum):
    MUTABLE = "MUTABLE"
    PROTECTED = "PROTECTED"
    IMMUTABLE = "IMMUTABLE"


class EvidenceNode(Element):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    content: dict[str, Any] = Field(default_factory=dict)
    previous_hash: str = Field(default=GENESIS_HASH)
    node_hash: str = Field(default="")
    tier: LogTier = Field(default=LogTier.IMMUTABLE)

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        if not self.node_hash:
            object.__setattr__(
                self, "node_hash", compute_node_hash(self.content, self.previous_hash)
            )

    def verify_hash(self) -> bool:
        return self.node_hash == compute_node_hash(self.content, self.previous_hash)


class ChainVerification(BaseModel):
    valid: bool
    checked_count: int = 0
    expected_count: int = 0
    first_invalid_index: int | None = None
    violation_type: str | None = None
    message: str = ""


class EvidenceChain(Element):
    """Append-only blockchain-style evidence log.  Nodes are stored in insertion order."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    _nodes: list[EvidenceNode] = []
    tip_hash: str = Field(default=GENESIS_HASH)
    node_count: int = Field(default=0)

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        object.__setattr__(self, "_nodes", [])
        object.__setattr__(self, "_lock", threading.Lock())

    def append(
        self,
        content: dict[str, Any],
        tier: LogTier = LogTier.IMMUTABLE,
    ) -> EvidenceNode:
        with self._lock:  # type: ignore[attr-defined]
            node = EvidenceNode(content=content, previous_hash=self.tip_hash, tier=tier)
            self._nodes.append(node)  # type: ignore[attr-defined]
            object.__setattr__(self, "tip_hash", node.node_hash)
            object.__setattr__(self, "node_count", self.node_count + 1)
        return node

    def nodes(self) -> list[EvidenceNode]:
        return list(self._nodes)  # type: ignore[attr-defined]

    def verify(self) -> ChainVerification:
        return ChainVerifier.verify(self)

    def head_hash(self) -> str:
        return self.tip_hash


class ChainVerifier:
    @staticmethod
    def verify(chain: EvidenceChain) -> ChainVerification:
        nodes = chain.nodes()
        expected_count = chain.node_count

        if len(nodes) != expected_count:
            return ChainVerification(
                valid=False,
                checked_count=len(nodes),
                expected_count=expected_count,
                first_invalid_index=min(len(nodes), expected_count),
                violation_type="delete",
                message="Node count does not match expected chain length.",
            )

        expected_prev = GENESIS_HASH
        for idx, node in enumerate(nodes):
            if node.previous_hash != expected_prev:
                return ChainVerification(
                    valid=False,
                    checked_count=idx + 1,
                    expected_count=expected_count,
                    first_invalid_index=idx,
                    violation_type="reorder",
                    message="Previous hash does not match predecessor.",
                )
            expected_node_hash = compute_node_hash(node.content, node.previous_hash)
            if node.node_hash != expected_node_hash:
                return ChainVerification(
                    valid=False,
                    checked_count=idx + 1,
                    expected_count=expected_count,
                    first_invalid_index=idx,
                    violation_type="tamper",
                    message="Node hash does not match content.",
                )
            expected_prev = node.node_hash

        if expected_prev != chain.tip_hash:
            return ChainVerification(
                valid=False,
                checked_count=len(nodes),
                expected_count=expected_count,
                first_invalid_index=len(nodes) - 1 if nodes else None,
                violation_type="head_mismatch",
                message="Computed head does not match chain tip.",
            )

        return ChainVerification(
            valid=True,
            checked_count=len(nodes),
            expected_count=expected_count,
            message="Chain verified.",
        )

# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import threading
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lionagi._class_registry import LION_CLASS_REGISTRY
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

__all__ = (
    "LogTier",
    "EvidenceNode",
    "ChainVerification",
    "EvidenceChain",
    "ChainVerifier",
    "GENESIS_HASH",
    "compute_node_hash",
)

GENESIS_HASH = "0" * 64


def _canonical_json(content: dict[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_node_hash(content: dict[str, Any], previous_hash: str) -> str:
    return _sha256_hex(_canonical_json(content) + "|" + previous_hash)


def _filter_sensitive(
    payload: dict[str, Any],
    sensitive_fields: list[str] | None,
) -> dict[str, Any]:
    out = dict(payload)
    for key in sensitive_fields or []:
        out.pop(key, None)
    return out


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
    sensitive_fields: list[str] = Field(default_factory=list)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        LION_CLASS_REGISTRY[cls.class_name(full=True)] = cls

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        if not self.node_hash:
            object.__setattr__(
                self,
                "node_hash",
                compute_node_hash(self.content, self.previous_hash),
            )

    def recompute_hash(self) -> str:
        return compute_node_hash(self.content, self.previous_hash)

    def verify_hash(self) -> bool:
        return self.node_hash == self.recompute_hash()

    def display_content(self) -> dict[str, Any]:
        return {k: v for k, v in self.content.items() if k not in set(self.sensitive_fields)}

    def to_audit_dict(self) -> dict[str, Any]:
        data = self.to_dict(mode="json")
        data["content"] = self.display_content()
        data.pop("sensitive_fields", None)
        return data


class ChainVerification(BaseModel):
    valid: bool
    checked_count: int = 0
    expected_count: int = 0
    first_invalid_index: int | None = None
    violation_type: str | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    message: str = ""


class EvidenceChain(Element):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    nodes: Pile = Field(
        default_factory=lambda: Pile(
            collections=[],
            item_type={EvidenceNode},
            strict_type=False,
            append_only=True,
        )
    )
    tip_hash: str = Field(default=GENESIS_HASH)
    node_count: int = Field(default=0)

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        # Lock is not a Pydantic field (not serialisable); bypass the model
        # validator using object.__setattr__ so Pydantic's extra="forbid"
        # does not raise.
        object.__setattr__(self, "_lock", threading.Lock())

    def append(
        self,
        content: dict[str, Any],
        tier: LogTier = LogTier.IMMUTABLE,
        *,
        sensitive_fields: list[str] | None = None,
    ) -> EvidenceNode:
        filtered = _filter_sensitive(content, sensitive_fields)
        with self._lock:  # type: ignore[attr-defined]
            node = EvidenceNode(
                content=filtered,
                previous_hash=self.tip_hash,
                tier=tier,
                sensitive_fields=list(sensitive_fields or []),
            )
            self.nodes.include(node)
            self.tip_hash = node.node_hash
            self.node_count += 1
        return node

    def verify(self) -> ChainVerification:
        return ChainVerifier.verify(self)

    def head_hash(self) -> str:
        return self.tip_hash


class ChainVerifier:
    @staticmethod
    def verify(chain: EvidenceChain) -> ChainVerification:
        expected_prev = GENESIS_HASH
        nodes = list(chain.nodes)

        if len(nodes) != chain.node_count:
            return ChainVerification(
                valid=False,
                checked_count=len(nodes),
                expected_count=chain.node_count,
                first_invalid_index=min(len(nodes), chain.node_count),
                violation_type="delete",
                message="Node count does not match expected chain length.",
            )

        for idx, node in enumerate(nodes):
            if node.previous_hash != expected_prev:
                return ChainVerification(
                    valid=False,
                    checked_count=idx + 1,
                    expected_count=chain.node_count,
                    first_invalid_index=idx,
                    violation_type="reorder",
                    expected_hash=expected_prev,
                    actual_hash=node.previous_hash,
                    message="Previous hash does not match predecessor.",
                )
            expected_node_hash = compute_node_hash(node.content, node.previous_hash)
            if node.node_hash != expected_node_hash:
                return ChainVerification(
                    valid=False,
                    checked_count=idx + 1,
                    expected_count=chain.node_count,
                    first_invalid_index=idx,
                    violation_type="tamper",
                    expected_hash=expected_node_hash,
                    actual_hash=node.node_hash,
                    message="Node hash does not match content.",
                )
            expected_prev = node.node_hash

        if expected_prev != chain.tip_hash:
            return ChainVerification(
                valid=False,
                checked_count=len(nodes),
                expected_count=chain.node_count,
                first_invalid_index=len(nodes) - 1 if nodes else None,
                violation_type="head_mismatch",
                expected_hash=chain.tip_hash,
                actual_hash=expected_prev,
                message="Computed head does not match chain tip.",
            )

        return ChainVerification(
            valid=True,
            checked_count=len(nodes),
            expected_count=chain.node_count,
            message="Chain verified.",
        )

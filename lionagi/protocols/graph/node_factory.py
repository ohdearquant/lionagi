# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field

__all__ = (
    "NodeConfig",
    "create_node",
)


@dataclass(frozen=True, slots=True)
class NodeConfig:
    """Frozen config for Node lifecycle and persistence; see docs/reference/protocols-core.md."""

    # Persistence
    table_name: str | None = None
    schema: str = "public"

    # Lifecycle
    soft_delete: bool = False
    versioning: bool = False
    content_hashing: bool = False
    track_updated_at: bool = False

    # Embedding configuration
    embedding_enabled: bool = False
    embedding_dim: int | None = None
    embedding_model: str | None = None

    # Content configuration
    content_type: type | None = None
    flatten_content: bool = False

    # Integrity/audit
    track_created_by: bool = False
    immutable_content: bool = False

    @property
    def is_persisted(self) -> bool:
        """True if table_name is set (node has DB backing)."""
        return self.table_name is not None

    @property
    def has_audit_fields(self) -> bool:
        """True if any audit/lifecycle tracking is enabled."""
        return (
            self.content_hashing
            or self.soft_delete
            or self.versioning
            or self.track_updated_at
            or self.track_created_by
        )

    @property
    def has_embedding(self) -> bool:
        """True if embedding support is enabled."""
        return self.embedding_enabled


def create_node(
    name: str,
    *,
    table_name: str | None = None,
    schema: str = "public",
    soft_delete: bool = False,
    versioning: bool = False,
    content_hashing: bool = False,
    track_updated_at: bool = False,
    embedding_enabled: bool = False,
    embedding_dim: int | None = None,
    embedding_model: str | None = None,
    content_type: type | None = None,
    flatten_content: bool = False,
    track_created_by: bool = False,
    immutable_content: bool = False,
    extra_fields: dict[str, tuple[type, Any]] | None = None,
    doc: str | None = None,
) -> type:
    """Return a Node subclass with node_config set and Pydantic fields generated for enabled features."""
    from .node import Node

    config = NodeConfig(
        table_name=table_name,
        schema=schema,
        soft_delete=soft_delete,
        versioning=versioning,
        content_hashing=content_hashing,
        track_updated_at=track_updated_at,
        embedding_enabled=embedding_enabled,
        embedding_dim=embedding_dim,
        embedding_model=embedding_model,
        content_type=content_type,
        flatten_content=flatten_content,
        track_created_by=track_created_by,
        immutable_content=immutable_content,
    )

    annotations: dict[str, type] = {}
    namespace: dict[str, Any] = {
        "node_config": config,
        "__annotations__": {},
    }

    # Generate real Pydantic fields for enabled audit features
    if config.versioning:
        annotations["version"] = int
        namespace["version"] = Field(default=0)

    if config.track_updated_at:
        annotations["updated_at"] = str | None
        namespace["updated_at"] = Field(default=None)

    if config.track_created_by:
        annotations["created_by"] = str | None
        namespace["created_by"] = Field(default=None)

    if config.content_hashing:
        annotations["content_hash"] = str | None
        namespace["content_hash"] = Field(default=None)

    if config.soft_delete:
        annotations["is_deleted"] = bool
        namespace["is_deleted"] = Field(default=False)
        annotations["deleted_at"] = str | None
        namespace["deleted_at"] = Field(default=None)

    if extra_fields:
        for field_name, (field_type, default) in extra_fields.items():
            annotations[field_name] = field_type
            namespace[field_name] = Field(default=default)

    if annotations:
        namespace["__annotations__"] = annotations

    if doc:
        namespace["__doc__"] = doc

    cls = type(name, (Node,), namespace)
    return cls

# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar

import orjson
from pydantic import BaseModel, field_serializer, field_validator

from lionagi._class_registry import LION_CLASS_REGISTRY
from lionagi.adapters._base import Adaptable, AsyncAdaptable
from lionagi.ln.types import DataClass

from .._concepts import Relational
from ..generic.element import Element

_ADAPTER_REGISTERED = False


class Node(Element, Relational, AsyncAdaptable, Adaptable):
    """
    A base class for all Nodes in a graph, storing:
      - Arbitrary content
      - Metadata as a dict
      - An optional numeric embedding (list of floats)
      - Automatic subclass registration

    Lifecycle methods (touch, soft_delete, restore, rehash) are
    available when ``node_config`` is set on a subclass. They are
    no-ops when ``node_config`` is None (the default), preserving
    backwards compatibility for all existing Node subclasses.
    """

    node_config: ClassVar[Any] = None

    content: Any = None
    embedding: list[float] | None = None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Initialize and register subclasses in the global class registry."""
        super().__pydantic_init_subclass__(**kwargs)
        LION_CLASS_REGISTRY[cls.class_name(full=True)] = cls

    @field_validator("embedding", mode="before")
    def _parse_embedding(cls, value: list[float] | str | None) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                loaded = orjson.loads(value)
                if not isinstance(loaded, list):
                    raise ValueError
                return [float(x) for x in loaded]
            except Exception as e:
                raise ValueError("Invalid embedding string.") from e
        if isinstance(value, list):
            try:
                return [float(x) for x in value]
            except Exception as e:
                raise ValueError("Invalid embedding list.") from e
        raise ValueError("Invalid embedding type; must be list or JSON-encoded string.")

    async def adapt_to_async(self, obj_key: str, many=False, **kwargs: Any) -> Any:
        # Only register postgres adapter if this specific operation needs it
        if obj_key == "lionagi_async_pg":
            _ensure_postgres_adapter()

        kwargs["adapt_meth"] = "to_dict"
        kwargs["adapt_kw"] = {"mode": "db"}
        return await super().adapt_to_async(obj_key=obj_key, many=many, **kwargs)

    @classmethod
    async def adapt_from_async(
        cls,
        obj: Any,
        obj_key: str,
        many=False,
        **kwargs: Any,
    ) -> Node:
        # Only register postgres adapter if this specific operation needs it
        if obj_key == "lionagi_async_pg":
            _ensure_postgres_adapter()

        kwargs["adapt_meth"] = "from_dict"
        return await super().adapt_from_async(obj, obj_key=obj_key, many=many, **kwargs)

    def adapt_to(self, obj_key: str, many=False, **kwargs: Any) -> Any:
        """
        Convert this Node to another format using a registered adapter.
        """
        kwargs["adapt_meth"] = "to_dict"
        kwargs["adapt_kw"] = {"mode": "db"}
        return super().adapt_to(obj_key=obj_key, many=many, **kwargs)

    @classmethod
    def adapt_from(
        cls,
        obj: Any,
        obj_key: str,
        many=False,
        **kwargs: Any,
    ) -> Node:
        """
        Construct a Node from an external format using a registered adapter.
        If the adapter returns a dictionary with 'lion_class', we can
        auto-delegate to the correct subclass via from_dict.
        """
        kwargs["adapt_meth"] = "from_dict"
        return super().adapt_from(obj, obj_key=obj_key, many=many, **kwargs)

    @field_serializer("content")
    def _serialize_content(self, value: Any) -> Any:
        if isinstance(value, Element):
            return value.to_dict()
        if isinstance(value, BaseModel):
            return value.model_dump()
        if isinstance(value, DataClass):
            return value.to_dict(exclude=value._config.serialize_exclude or None)
        return value

    @field_validator("content", mode="before")
    def _validate_content(cls, value: Any) -> Any:
        if isinstance(value, dict) and "lion_class" in value.get("metadata", {}):
            return Element.from_dict(value)
        return value

    # ==================== Lifecycle Methods ====================

    def _has_real_field(self, name: str) -> bool:
        """Check if a real Pydantic model field exists on this instance."""
        return name in self.__class__.model_fields

    def touch(self, by: str | None = None) -> None:
        """Update timestamps, increment version, and rehash per config.

        No-op if ``node_config`` is None. Prefers real Pydantic fields
        (generated by :func:`create_node`) when available, falling back
        to metadata dict storage for manual subclasses.

        Args:
            by: Actor identifier for updated_by tracking.
        """
        config = self.node_config
        if config is None:
            return

        if config.track_updated_at:
            ts = datetime.now(tz=timezone.utc).isoformat()
            if self._has_real_field("updated_at"):
                self.updated_at = ts
            else:
                self.metadata["updated_at"] = ts

        if by is not None:
            if (
                config.track_created_by
                and self._has_real_field("created_by")
                and not getattr(self, "created_by", None)
            ):
                self.created_by = str(by)
            self.metadata["updated_by"] = str(by)

        if config.versioning:
            if self._has_real_field("version"):
                self.version = (self.version or 0) + 1
            else:
                self.metadata["version"] = self.metadata.get("version", 0) + 1

        if config.content_hashing:
            self.rehash()

    def soft_delete(self, by: str | None = None) -> None:
        """Mark as deleted (reversible). Requires soft_delete in config.

        Prefers real Pydantic fields when available, falling back to
        metadata dict storage for manual subclasses.

        Args:
            by: Actor identifier for deleted_by tracking.

        Raises:
            RuntimeError: If soft_delete not enabled in config.
        """
        config = self.node_config
        if config is None or not config.soft_delete:
            raise RuntimeError(
                f"{self.__class__.__name__} does not support soft_delete. "
                "Enable with NodeConfig(soft_delete=True)."
            )

        ts = datetime.now(tz=timezone.utc).isoformat()

        if self._has_real_field("is_deleted"):
            self.is_deleted = True
        else:
            self.metadata["is_deleted"] = True

        if self._has_real_field("deleted_at"):
            self.deleted_at = ts
        else:
            self.metadata["deleted_at"] = ts

        if by is not None:
            self.metadata["deleted_by"] = str(by)

        self.touch(by)

    def restore(self, by: str | None = None) -> None:
        """Undelete a soft-deleted node. Requires soft_delete in config.

        Prefers real Pydantic fields when available, falling back to
        metadata dict storage for manual subclasses.

        Args:
            by: Actor identifier for updated_by tracking.

        Raises:
            RuntimeError: If soft_delete not enabled in config.
        """
        config = self.node_config
        if config is None or not config.soft_delete:
            raise RuntimeError(
                f"{self.__class__.__name__} does not support restore. "
                "Enable with NodeConfig(soft_delete=True)."
            )

        if self._has_real_field("is_deleted"):
            self.is_deleted = False
        else:
            self.metadata["is_deleted"] = False

        if self._has_real_field("deleted_at"):
            self.deleted_at = None
        else:
            self.metadata.pop("deleted_at", None)

        self.metadata.pop("deleted_by", None)
        self.touch(by)

    def rehash(self) -> str | None:
        """Recompute and store content_hash. Returns hash or None if disabled.

        Prefers the real ``content_hash`` Pydantic field when available,
        falling back to metadata dict storage.

        Returns:
            Hex-encoded SHA-256 hash of content, or None if content_hashing
            is not enabled.
        """
        config = self.node_config
        if config is None or not config.content_hashing:
            return None

        from lionagi.ln import compute_hash

        new_hash = compute_hash(self.content, none_as_valid=True)
        if self._has_real_field("content_hash"):
            self.content_hash = new_hash
        else:
            self.metadata["content_hash"] = new_hash
        return new_hash


def _ensure_postgres_adapter():
    """Lazy registration of postgres adapter when needed.

    Imports and constructs LionAGIAsyncPostgresAdapter only when the
    lionagi[postgres] extra (pydapter[postgres], sqlalchemy, asyncpg) is
    present.  Safe to call multiple times — only acts on the first call.
    """
    if not hasattr(Node, "_postgres_adapter_checked"):
        from lionagi.adapters._utils import check_async_postgres_available

        if check_async_postgres_available() is True:
            try:
                from lionagi.adapters.async_postgres_adapter import (
                    create_lionagi_async_postgres_adapter,
                )

                adapter_cls = create_lionagi_async_postgres_adapter()
                Node.register_async_adapter(adapter_cls)
            except Exception:  # noqa: BLE001, S110
                pass  # Graceful degradation if postgres dependencies missing
        Node._postgres_adapter_checked = True


if not _ADAPTER_REGISTERED:
    from lionagi.adapters.json_ import JsonAdapter
    from lionagi.adapters.toml_ import TomlAdapter

    Node.register_adapter(JsonAdapter)
    Node.register_adapter(TomlAdapter)

    # PostgreSQL adapter registration is now lazy - only loaded when needed
    # Call _ensure_postgres_adapter() in methods that actually use async adapters

    _ADAPTER_REGISTERED = True

Node = Node

__all__ = ("Node",)

# File: lionagi/protocols/graph/node.py

# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for expanded NodeConfig fields, create_node() audit field generation, and Node lifecycle with real Pydantic fields."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from lionagi.ln import compute_hash
from lionagi.protocols.graph.node import Node
from lionagi.protocols.graph.node_factory import NodeConfig, create_node


def _content_hash(content):
    """Wrapper for compute_hash matching the rehash() calling convention."""
    return compute_hash(content, none_as_valid=True)


class TestNodeConfigNewFields:
    """Test new fields added to NodeConfig."""

    def test_embedding_enabled_default(self):
        cfg = NodeConfig()
        assert cfg.embedding_enabled is False

    def test_embedding_enabled_set(self):
        cfg = NodeConfig(embedding_enabled=True, embedding_dim=768)
        assert cfg.embedding_enabled is True
        assert cfg.embedding_dim == 768

    def test_embedding_model_default(self):
        cfg = NodeConfig()
        assert cfg.embedding_model is None

    def test_embedding_model_set(self):
        cfg = NodeConfig(
            embedding_enabled=True,
            embedding_dim=1536,
            embedding_model="text-embedding-3-small",
        )
        assert cfg.embedding_model == "text-embedding-3-small"
        assert cfg.embedding_dim == 1536

    def test_content_type_default(self):
        cfg = NodeConfig()
        assert cfg.content_type is None

    def test_content_type_set(self):
        cfg = NodeConfig(content_type=dict)
        assert cfg.content_type is dict

    def test_flatten_content_default(self):
        cfg = NodeConfig()
        assert cfg.flatten_content is False

    def test_flatten_content_set(self):
        cfg = NodeConfig(flatten_content=True)
        assert cfg.flatten_content is True

    def test_track_created_by_default(self):
        cfg = NodeConfig()
        assert cfg.track_created_by is False

    def test_track_created_by_set(self):
        cfg = NodeConfig(track_created_by=True)
        assert cfg.track_created_by is True

    def test_immutable_content_default(self):
        cfg = NodeConfig()
        assert cfg.immutable_content is False

    def test_immutable_content_set(self):
        cfg = NodeConfig(immutable_content=True)
        assert cfg.immutable_content is True


class TestNodeConfigNewProperties:
    """Test new computed properties on NodeConfig."""

    def test_has_embedding_false_by_default(self):
        cfg = NodeConfig()
        assert cfg.has_embedding is False

    def test_has_embedding_true_when_enabled(self):
        cfg = NodeConfig(embedding_enabled=True)
        assert cfg.has_embedding is True

    def test_has_audit_fields_true_when_track_created_by(self):
        cfg = NodeConfig(track_created_by=True)
        assert cfg.has_audit_fields is True


class TestNodeConfigNewFieldsImmutability:
    """Frozen enforcement on new fields."""

    def test_cannot_set_embedding_enabled(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.embedding_enabled = True

    def test_cannot_set_embedding_dim(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.embedding_dim = 768

    def test_cannot_set_embedding_model(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.embedding_model = "model"

    def test_cannot_set_content_type(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.content_type = dict

    def test_cannot_set_flatten_content(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.flatten_content = True

    def test_cannot_set_track_created_by(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.track_created_by = True

    def test_cannot_set_immutable_content(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.immutable_content = True


class TestCreateNodeRealFieldsVersioning:
    """create_node() with versioning=True generates a 'version' field."""

    def test_version_field_exists(self):
        cls = create_node("Versioned", versioning=True)
        inst = cls()
        assert hasattr(inst, "version")
        assert "version" in cls.model_fields

    def test_version_field_default(self):
        cls = create_node("Versioned", versioning=True)
        inst = cls()
        assert inst.version == 0

    def test_version_field_custom_value(self):
        cls = create_node("Versioned", versioning=True)
        inst = cls(version=5)
        assert inst.version == 5

    def test_version_in_serialization(self):
        cls = create_node("Versioned", versioning=True)
        inst = cls(version=3)
        d = inst.to_dict()
        assert d["version"] == 3


class TestCreateNodeRealFieldsSoftDelete:
    """create_node() with soft_delete=True generates is_deleted and deleted_at fields."""

    def test_is_deleted_field_exists(self):
        cls = create_node("Deletable", soft_delete=True)
        inst = cls()
        assert hasattr(inst, "is_deleted")
        assert "is_deleted" in cls.model_fields

    def test_is_deleted_default(self):
        cls = create_node("Deletable", soft_delete=True)
        inst = cls()
        assert inst.is_deleted is False

    def test_deleted_at_field_exists(self):
        cls = create_node("Deletable", soft_delete=True)
        inst = cls()
        assert hasattr(inst, "deleted_at")
        assert "deleted_at" in cls.model_fields

    def test_deleted_at_default(self):
        cls = create_node("Deletable", soft_delete=True)
        inst = cls()
        assert inst.deleted_at is None


class TestCreateNodeRealFieldsTrackUpdatedAt:
    """create_node() with track_updated_at=True generates updated_at field."""

    def test_updated_at_field_exists(self):
        cls = create_node("Tracked", track_updated_at=True)
        inst = cls()
        assert hasattr(inst, "updated_at")
        assert "updated_at" in cls.model_fields

    def test_updated_at_default(self):
        cls = create_node("Tracked", track_updated_at=True)
        inst = cls()
        assert inst.updated_at is None


class TestCreateNodeRealFieldsTrackCreatedBy:
    """create_node() with track_created_by=True generates created_by field."""

    def test_created_by_field_exists(self):
        cls = create_node("Audited", track_created_by=True)
        inst = cls()
        assert hasattr(inst, "created_by")
        assert "created_by" in cls.model_fields

    def test_created_by_default(self):
        cls = create_node("Audited", track_created_by=True)
        inst = cls()
        assert inst.created_by is None


class TestCreateNodeRealFieldsContentHashing:
    """create_node() with content_hashing=True generates content_hash field."""

    def test_content_hash_field_exists(self):
        cls = create_node("Hashed", content_hashing=True)
        inst = cls()
        assert hasattr(inst, "content_hash")
        assert "content_hash" in cls.model_fields

    def test_content_hash_default(self):
        cls = create_node("Hashed", content_hashing=True)
        inst = cls()
        assert inst.content_hash is None


class TestCreateNodeNewParams:
    """create_node() passes new config params through."""

    def test_embedding_config(self):
        cls = create_node(
            "Embedded",
            embedding_enabled=True,
            embedding_dim=768,
            embedding_model="text-embedding-3-small",
        )
        cfg = cls.node_config
        assert cfg.embedding_enabled is True
        assert cfg.embedding_dim == 768
        assert cfg.embedding_model == "text-embedding-3-small"

    def test_content_config(self):
        cls = create_node(
            "Typed",
            content_type=dict,
            flatten_content=True,
        )
        cfg = cls.node_config
        assert cfg.content_type is dict
        assert cfg.flatten_content is True

    def test_integrity_config(self):
        cls = create_node(
            "Integrity",
            track_created_by=True,
            immutable_content=True,
        )
        cfg = cls.node_config
        assert cfg.track_created_by is True
        assert cfg.immutable_content is True


class TestTouchRealFields:
    """touch() prefers real Pydantic fields over metadata when available."""

    def test_touch_sets_version_field(self):
        cls = create_node("V", versioning=True)
        v = cls()
        v.touch()
        assert v.version == 1
        v.touch()
        assert v.version == 2

    def test_touch_sets_updated_at_field(self):
        cls = create_node("T", track_updated_at=True)
        t = cls()
        before = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        t.touch()
        after = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        assert t.updated_at is not None
        assert before <= t.updated_at <= after

    def test_touch_sets_created_by_field_first_time(self):
        cls = create_node("CB", track_created_by=True)
        cb = cls()
        cb.touch(by="alice")
        assert cb.created_by == "alice"

    def test_touch_does_not_overwrite_created_by(self):
        cls = create_node("CB", track_created_by=True)
        cb = cls()
        cb.touch(by="alice")
        assert cb.created_by == "alice"
        cb.touch(by="bob")
        # created_by should NOT be overwritten
        assert cb.created_by == "alice"
        # updated_by still tracked in metadata
        assert cb.metadata["updated_by"] == "bob"

    def test_touch_sets_content_hash_field(self):
        cls = create_node("H", content_hashing=True)
        h = cls(content="payload")
        h.touch()
        assert h.content_hash is not None
        assert h.content_hash == _content_hash("payload")

    def test_touch_all_real_fields_combined(self):
        cls = create_node(
            "FullReal",
            versioning=True,
            track_updated_at=True,
            track_created_by=True,
            content_hashing=True,
        )
        inst = cls(content="data")
        inst.touch(by="system")

        assert inst.version == 1
        assert inst.updated_at is not None
        assert inst.created_by == "system"
        assert inst.content_hash == _content_hash("data")


class TestTouchMetadataFallback:
    """touch() uses metadata when the model has no real fields (manual subclass)."""

    def test_manual_subclass_touch_uses_metadata(self):
        class Article(Node):
            node_config = NodeConfig(
                versioning=True,
                track_updated_at=True,
                content_hashing=True,
            )

        a = Article(content="hello")
        a.touch(by="editor")

        # version in metadata (no 'version' model field)
        assert a.metadata["version"] == 1
        assert "updated_at" in a.metadata
        assert a.metadata["updated_by"] == "editor"
        assert "content_hash" in a.metadata

    def test_manual_subclass_no_version_field(self):
        class Article(Node):
            node_config = NodeConfig(versioning=True)

        a = Article()
        assert "version" not in a.model_fields
        a.touch()
        # Falls back to metadata
        assert a.metadata["version"] == 1


class TestSoftDeleteRealFields:
    """soft_delete() and restore() use real fields when available."""

    def test_soft_delete_sets_real_is_deleted(self):
        cls = create_node("Del", soft_delete=True)
        d = cls(content="bye")
        d.soft_delete()
        assert d.is_deleted is True

    def test_soft_delete_sets_real_deleted_at(self):
        cls = create_node("Del", soft_delete=True)
        d = cls()
        before = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        d.soft_delete()
        after = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        assert d.deleted_at is not None
        assert before <= d.deleted_at <= after

    def test_restore_clears_real_is_deleted(self):
        cls = create_node("Rest", soft_delete=True)
        r = cls()
        r.soft_delete()
        assert r.is_deleted is True
        r.restore()
        assert r.is_deleted is False

    def test_restore_clears_real_deleted_at(self):
        cls = create_node("Rest", soft_delete=True)
        r = cls()
        r.soft_delete()
        assert r.deleted_at is not None
        r.restore()
        assert r.deleted_at is None

    def test_full_cycle_with_real_fields(self):
        cls = create_node(
            "FullCycle",
            soft_delete=True,
            versioning=True,
            track_updated_at=True,
            content_hashing=True,
        )
        a = cls(content={"status": "active"})

        # Initial state: real fields at defaults
        assert a.version == 0
        assert a.updated_at is None
        assert a.is_deleted is False
        assert a.deleted_at is None
        assert a.content_hash is None

        # touch
        a.touch(by="alice")
        assert a.version == 1
        assert a.updated_at is not None
        assert a.content_hash is not None
        hash_v1 = a.content_hash

        # soft_delete
        a.soft_delete(by="bob")
        assert a.is_deleted is True
        assert a.deleted_at is not None
        assert a.version == 2  # touch was called inside soft_delete

        # restore
        a.restore(by="carol")
        assert a.is_deleted is False
        assert a.deleted_at is None
        assert a.version == 3  # touch was called inside restore

        # content change + rehash
        a.content = {"status": "updated"}
        a.touch(by="dave")
        assert a.version == 4
        assert a.content_hash != hash_v1

    def test_soft_delete_metadata_fallback(self):
        """Manual subclass without real fields uses metadata."""

        class LegacyDel(Node):
            node_config = NodeConfig(soft_delete=True, versioning=True)

        d = LegacyDel()
        d.soft_delete()
        assert d.metadata["is_deleted"] is True
        assert "deleted_at" in d.metadata
        assert d.metadata.get("version") == 1


class TestRehashRealFields:
    """rehash() stores in content_hash field when available."""

    def test_rehash_stores_in_real_field(self):
        cls = create_node("H", content_hashing=True)
        h = cls(content="payload")
        result = h.rehash()
        assert h.content_hash == result
        assert result == _content_hash("payload")

    def test_rehash_updates_real_field_on_content_change(self):
        cls = create_node("H", content_hashing=True)
        h = cls(content="before")
        hash1 = h.rehash()
        h.content = "after"
        hash2 = h.rehash()
        assert hash1 != hash2
        assert h.content_hash == hash2

    def test_rehash_metadata_fallback(self):
        """Manual subclass uses metadata for content_hash."""

        class Doc(Node):
            node_config = NodeConfig(content_hashing=True)

        d = Doc(content="hello")
        result = d.rehash()
        assert d.metadata["content_hash"] == result


class TestImmutableContentConfig:
    """immutable_content flag is stored in config but not enforced yet."""

    def test_immutable_content_flag_set(self):
        cls = create_node("Immutable", immutable_content=True)
        assert cls.node_config.immutable_content is True

    def test_content_still_modifiable(self):
        """Since immutable_content is config-only, content is still modifiable."""
        cls = create_node("Immutable", immutable_content=True)
        inst = cls(content="original")
        inst.content = "modified"
        assert inst.content == "modified"


class TestBackwardsCompatibilityWithRealFields:
    """Existing tests and patterns still work after the changes."""

    def test_base_node_has_no_config(self):
        assert Node.node_config is None

    def test_base_node_touch_is_noop(self):
        n = Node(content="safe")
        n.touch()
        assert n.metadata == {}

    def test_create_node_without_audit_no_extra_fields(self):
        """create_node with no audit flags generates no extra fields."""
        cls = create_node("Plain")
        assert "version" not in cls.model_fields
        assert "is_deleted" not in cls.model_fields
        assert "updated_at" not in cls.model_fields
        assert "content_hash" not in cls.model_fields
        assert "created_by" not in cls.model_fields

    def test_extra_fields_still_work(self):
        cls = create_node(
            "Job",
            versioning=True,
            extra_fields={"priority": (int, 0)},
        )
        job = cls(content="build", priority=5)
        assert job.priority == 5
        assert job.version == 0
        job.touch()
        assert job.version == 1

    def test_config_does_not_bleed_across_subclasses(self):
        cls_a = create_node("A", versioning=True, soft_delete=True)
        cls_b = create_node("B")
        assert "version" in cls_a.model_fields
        assert "is_deleted" in cls_a.model_fields
        assert "version" not in cls_b.model_fields
        assert "is_deleted" not in cls_b.model_fields
        assert Node.node_config is None

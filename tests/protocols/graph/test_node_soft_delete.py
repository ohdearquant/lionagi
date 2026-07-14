# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Node soft_delete, restore, rehash, and full delete-restore cycle."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lionagi.ln import compute_hash
from lionagi.protocols.graph import node as node_mod
from lionagi.protocols.graph.node import Node
from lionagi.protocols.graph.node_factory import create_node


def _content_hash(content):
    """Wrapper for compute_hash matching the old rehash() calling convention."""
    return compute_hash(content, none_as_valid=True)


def _frozen_datetime(fixed):
    """A datetime subclass whose now() always returns `fixed`."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    return _Frozen


class TestNodeSoftDelete:
    """Node.soft_delete() behaviour."""

    def test_soft_delete_raises_on_base_node(self):
        n = Node()
        with pytest.raises(RuntimeError, match="does not support soft_delete"):
            n.soft_delete()

    def test_soft_delete_raises_when_flag_disabled(self):
        cls = create_node("NoDel", soft_delete=False)
        nd = cls()
        with pytest.raises(RuntimeError, match="does not support soft_delete"):
            nd.soft_delete()

    def test_soft_delete_sets_is_deleted(self):
        cls = create_node("Del", soft_delete=True)
        d = cls(content="bye")
        d.soft_delete()
        assert d.is_deleted is True

    def test_soft_delete_sets_deleted_at(self, monkeypatch):
        fixed = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(node_mod, "datetime", _frozen_datetime(fixed))
        cls = create_node("Del", soft_delete=True)
        d = cls()
        d.soft_delete()
        assert d.deleted_at == fixed.isoformat()

    def test_soft_delete_with_by_param(self):
        cls = create_node("Del", soft_delete=True)
        d = cls()
        d.soft_delete(by="admin")
        assert d.metadata["deleted_by"] == "admin"

    def test_soft_delete_without_by_param(self):
        cls = create_node("Del", soft_delete=True)
        d = cls()
        d.soft_delete()
        assert "deleted_by" not in d.metadata

    def test_soft_delete_calls_touch(self):
        cls = create_node("Del", soft_delete=True, versioning=True)
        d = cls()
        d.soft_delete()
        # touch() should have incremented version
        assert d.version == 1

    def test_soft_delete_with_track_updated_at(self):
        cls = create_node(
            "Del",
            soft_delete=True,
            track_updated_at=True,
        )
        d = cls()
        d.soft_delete(by="admin")
        assert d.is_deleted is True
        assert d.updated_at is not None
        assert d.metadata.get("updated_by") == "admin"

    def test_soft_delete_raises_on_subclass_without_config(self):
        """Subclass inheriting node_config=None from base Node."""

        class PlainNode(Node):
            pass

        pn = PlainNode()
        with pytest.raises(RuntimeError, match="does not support soft_delete"):
            pn.soft_delete()


class TestNodeRestore:
    """Node.restore() behaviour."""

    def test_restore_raises_on_base_node(self):
        n = Node()
        with pytest.raises(RuntimeError, match="does not support restore"):
            n.restore()

    def test_restore_raises_when_flag_disabled(self):
        cls = create_node("NoRestore", soft_delete=False)
        nr = cls()
        with pytest.raises(RuntimeError, match="does not support restore"):
            nr.restore()

    def test_restore_sets_is_deleted_false(self):
        cls = create_node("Rest", soft_delete=True)
        r = cls()
        r.soft_delete()
        assert r.is_deleted is True
        r.restore()
        assert r.is_deleted is False

    def test_restore_removes_deleted_at(self):
        cls = create_node("Rest", soft_delete=True)
        r = cls()
        r.soft_delete()
        assert r.deleted_at is not None
        r.restore()
        assert r.deleted_at is None

    def test_restore_removes_deleted_by(self):
        cls = create_node("Rest", soft_delete=True)
        r = cls()
        r.soft_delete(by="admin")
        assert "deleted_by" in r.metadata
        r.restore()
        assert "deleted_by" not in r.metadata

    def test_restore_calls_touch(self):
        cls = create_node("Rest", soft_delete=True, versioning=True)
        r = cls()
        r.soft_delete()
        assert r.version == 1
        r.restore()
        # touch was called again during restore
        assert r.version == 2

    def test_restore_with_by_param(self):
        cls = create_node("Rest", soft_delete=True, track_updated_at=True)
        r = cls()
        r.soft_delete(by="admin")
        r.restore(by="manager")
        assert r.metadata.get("updated_by") == "manager"
        assert r.is_deleted is False

    def test_restore_without_prior_delete(self):
        """Calling restore on a never-deleted node still works (sets is_deleted=False)."""
        cls = create_node("Rest", soft_delete=True)
        r = cls()
        r.restore()
        assert r.is_deleted is False

    def test_restore_raises_on_subclass_without_config(self):
        class PlainNode(Node):
            pass

        pn = PlainNode()
        with pytest.raises(RuntimeError, match="does not support restore"):
            pn.restore()


class TestNodeRehash:
    """Node.rehash() behaviour."""

    def test_rehash_returns_none_on_base_node(self):
        n = Node(content="hi")
        assert n.rehash() is None

    def test_rehash_returns_none_when_hashing_disabled(self):
        cls = create_node("NoHash", content_hashing=False)
        nh = cls(content="hi")
        assert nh.rehash() is None

    def test_rehash_returns_hex_string(self):
        cls = create_node("Hash", content_hashing=True)
        h = cls(content="payload")
        result = h.rehash()
        assert isinstance(result, str)
        assert len(result) == 64

    def test_rehash_stores_in_field(self):
        cls = create_node("Hash", content_hashing=True)
        h = cls(content="payload")
        h.rehash()
        assert h.content_hash is not None

    def test_rehash_matches__content_hash(self):
        cls = create_node("Hash", content_hashing=True)
        content = {"key": "value"}
        h = cls(content=content)
        result = h.rehash()
        assert result == _content_hash(content)

    def test_rehash_updates_on_content_change(self):
        cls = create_node("Hash", content_hashing=True)
        h = cls(content="before")
        hash1 = h.rehash()
        h.content = "after"
        hash2 = h.rehash()
        assert hash1 != hash2
        assert hash2 == _content_hash("after")

    def test_rehash_none_content(self):
        cls = create_node("Hash", content_hashing=True)
        h = cls(content=None)
        result = h.rehash()
        assert result == _content_hash(None)

    def test_rehash_returns_none_on_subclass_without_config(self):
        class PlainNode(Node):
            pass

        pn = PlainNode(content="data")
        assert pn.rehash() is None


class TestDeleteRestoreCycle:
    """End-to-end soft_delete -> restore cycle with full audit trail."""

    def test_full_cycle(self):
        cls = create_node(
            "Audited",
            soft_delete=True,
            versioning=True,
            track_updated_at=True,
            content_hashing=True,
        )
        a = cls(content={"status": "active"})

        # --- initial state: real fields at defaults ---
        assert a.version == 0
        assert a.updated_at is None
        assert a.is_deleted is False
        assert a.deleted_at is None
        assert a.content_hash is None

        # --- touch once ---
        a.touch(by="alice")
        assert a.version == 1
        assert a.updated_at is not None
        assert a.metadata["updated_by"] == "alice"
        assert a.content_hash is not None
        hash_v1 = a.content_hash

        # --- soft_delete ---
        a.soft_delete(by="bob")
        assert a.is_deleted is True
        assert a.deleted_at is not None
        assert a.metadata["deleted_by"] == "bob"
        assert a.version == 2  # touch was called inside soft_delete

        # --- restore ---
        a.restore(by="carol")
        assert a.is_deleted is False
        assert a.deleted_at is None
        assert "deleted_by" not in a.metadata
        assert a.version == 3  # touch was called inside restore
        assert a.metadata["updated_by"] == "carol"

        # --- content change + rehash ---
        a.content = {"status": "updated"}
        a.touch(by="dave")
        assert a.version == 4
        hash_v4 = a.content_hash
        assert hash_v4 != hash_v1

    def test_double_delete_is_idempotent(self):
        cls = create_node("Del", soft_delete=True, versioning=True)
        d = cls()
        d.soft_delete()
        assert d.is_deleted is True
        assert d.version == 1
        d.soft_delete()
        assert d.is_deleted is True
        assert d.version == 2  # version still increments

    def test_double_restore_is_idempotent(self):
        cls = create_node("Rst", soft_delete=True, versioning=True)
        r = cls()
        r.soft_delete()
        r.restore()
        assert r.is_deleted is False
        assert r.version == 2
        r.restore()
        assert r.is_deleted is False
        assert r.version == 3

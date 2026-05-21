# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Node backwards compatibility, edge cases, and manual subclass config."""

from __future__ import annotations

import pytest

from lionagi.ln import compute_hash
from lionagi.protocols.graph.node import Node
from lionagi.protocols.graph.node_factory import NodeConfig, create_node


def _content_hash(content):
    """Wrapper for compute_hash matching the old rehash() calling convention."""
    return compute_hash(content, none_as_valid=True)


class TestBackwardsCompatibility:
    """Ensure base Node and unaware subclasses remain unaffected."""

    def test_base_node_has_no_config(self):
        assert Node.node_config is None

    def test_base_node_touch_is_noop(self):
        n = Node(content="safe")
        n.touch()
        assert n.metadata == {}

    def test_base_node_rehash_returns_none(self):
        n = Node(content="safe")
        assert n.rehash() is None
        assert "content_hash" not in n.metadata

    def test_base_node_soft_delete_raises(self):
        n = Node()
        with pytest.raises(RuntimeError):
            n.soft_delete()

    def test_base_node_restore_raises(self):
        n = Node()
        with pytest.raises(RuntimeError):
            n.restore()

    def test_unaware_subclass_has_no_config(self):
        class LegacyNode(Node):
            extra: str = "legacy"

        assert LegacyNode.node_config is None
        ln_ = LegacyNode(extra="hello")
        ln_.touch()  # no-op
        assert ln_.metadata == {}

    def test_unaware_subclass_soft_delete_raises(self):
        class LegacyNode(Node):
            pass

        with pytest.raises(RuntimeError):
            LegacyNode().soft_delete()

    def test_unaware_subclass_restore_raises(self):
        class LegacyNode(Node):
            pass

        with pytest.raises(RuntimeError):
            LegacyNode().restore()

    def test_unaware_subclass_rehash_returns_none(self):
        class LegacyNode(Node):
            pass

        assert LegacyNode(content="x").rehash() is None

    def test_node_config_classvar_does_not_bleed_across_subclasses(self):
        """A config on one subclass must not affect another."""
        Configured = create_node("ConfiguredNode", versioning=True)
        assert Configured.node_config is not None
        assert Configured.node_config.versioning is True
        # Base Node is still unaffected
        assert Node.node_config is None


# ===================================================================
# 7. Edge cases and error conditions
# ===================================================================


class TestEdgeCases:
    """Boundary conditions and unusual inputs."""

    def test_create_node_empty_name(self):
        cls = create_node("")
        assert cls.__name__ == ""
        inst = cls(content="works")
        assert inst.content == "works"

    def test_create_node_with_only_extra_fields(self):
        cls = create_node(
            "FieldsOnly",
            extra_fields={"score": (float, 0.0)},
        )
        f = cls(score=9.5)
        assert f.score == 9.5
        assert f.node_config.table_name is None

    def test_touch_on_freshly_created_node_no_prior_version(self):
        cls = create_node("Fresh", versioning=True)
        f = cls()
        assert f.version == 0
        f.touch()
        assert f.version == 1

    def test_rehash_with_large_content(self):
        cls = create_node("Big", content_hashing=True)
        big_content = "x" * 1_000_000
        b = cls(content=big_content)
        result = b.rehash()
        assert result == _content_hash(big_content)

    def test_create_node_schema_override(self):
        cls = create_node("Custom", schema="private")
        assert cls.node_config.schema == "private"

    def test_multiple_create_node_calls_independent(self):
        A = create_node("A", versioning=True)
        B = create_node("B", soft_delete=True)
        assert A.node_config.versioning is True
        assert A.node_config.soft_delete is False
        assert B.node_config.soft_delete is True
        assert B.node_config.versioning is False

    def test_touch_updates_timestamp_each_call(self):
        cls = create_node("TS", track_updated_at=True)
        t = cls()
        t.touch()
        ts1 = t.updated_at
        # Set updated_at to a known past value to guarantee ts2 differs from ts1
        t.updated_at = "2000-01-01T00:00:00+00:00"
        t.touch()
        ts2 = t.updated_at
        assert ts2 > "2000-01-01T00:00:00+00:00"
        assert ts2 != "2000-01-01T00:00:00+00:00"

    def test_soft_delete_error_message_includes_class_name(self):
        cls = create_node("MySpecialNode", soft_delete=False)
        inst = cls()
        with pytest.raises(RuntimeError, match="MySpecialNode"):
            inst.soft_delete()

    def test_restore_error_message_includes_class_name(self):
        cls = create_node("MySpecialNode", soft_delete=False)
        inst = cls()
        with pytest.raises(RuntimeError, match="MySpecialNode"):
            inst.restore()

    def test_content_hash_empty_dict(self):
        result = _content_hash({})
        assert isinstance(result, str)
        assert len(result) == 64

    def test_content_hash_empty_list(self):
        result = _content_hash([])
        assert isinstance(result, str)
        assert len(result) == 64

    def test_content_hash_boolean(self):
        h_true = _content_hash(True)
        h_false = _content_hash(False)
        assert h_true != h_false

    def test_content_hash_float(self):
        result = _content_hash(3.14)
        assert isinstance(result, str)
        assert len(result) == 64


# ===================================================================
# 8. Manual subclass with node_config ClassVar
# ===================================================================


class TestManualSubclassWithConfig:
    """Test setting node_config as a ClassVar on a hand-written subclass."""

    def test_manual_subclass_lifecycle(self):
        class Article(Node):
            node_config = NodeConfig(
                table_name="articles",
                soft_delete=True,
                versioning=True,
                content_hashing=True,
                track_updated_at=True,
            )

        a = Article(content={"title": "Test"})
        assert Article.node_config.table_name == "articles"

        a.touch(by="editor")
        assert a.metadata["version"] == 1
        assert "updated_at" in a.metadata
        assert a.metadata["updated_by"] == "editor"
        assert "content_hash" in a.metadata

        a.soft_delete(by="admin")
        assert a.metadata["is_deleted"] is True
        assert a.metadata["version"] == 2

        a.restore(by="admin")
        assert a.metadata["is_deleted"] is False
        assert a.metadata["version"] == 3

    def test_manual_subclass_rehash(self):
        class Doc(Node):
            node_config = NodeConfig(content_hashing=True)

        d = Doc(content="hello world")
        h = d.rehash()
        assert h == _content_hash("hello world")
        assert d.metadata["content_hash"] == h

    def test_manual_subclass_no_soft_delete(self):
        class ReadOnly(Node):
            node_config = NodeConfig(versioning=True)

        ro = ReadOnly()
        with pytest.raises(RuntimeError):
            ro.soft_delete()

    def test_manual_subclass_no_restore(self):
        class ReadOnly(Node):
            node_config = NodeConfig(versioning=True)

        ro = ReadOnly()
        with pytest.raises(RuntimeError):
            ro.restore()

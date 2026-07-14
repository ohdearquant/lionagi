# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for NodeConfig defaults, compute_hash, create_node factory, and Node lifecycle methods."""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from lionagi.ln import compute_hash
from lionagi.protocols.generic import element as element_mod
from lionagi.protocols.graph import node as node_mod
from lionagi.protocols.graph.node import Node
from lionagi.protocols.graph.node_factory import NodeConfig, create_node


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


class TestNodeConfigDefaults:
    """NodeConfig default values and basic construction."""

    def test_all_defaults(self):
        cfg = NodeConfig()
        assert cfg.table_name is None
        assert cfg.schema == "public"
        assert cfg.soft_delete is False
        assert cfg.versioning is False
        assert cfg.content_hashing is False
        assert cfg.track_updated_at is False

    def test_explicit_table_name(self):
        cfg = NodeConfig(table_name="jobs")
        assert cfg.table_name == "jobs"

    def test_explicit_schema(self):
        cfg = NodeConfig(schema="analytics")
        assert cfg.schema == "analytics"

    def test_all_flags_enabled(self):
        cfg = NodeConfig(
            table_name="items",
            schema="warehouse",
            soft_delete=True,
            versioning=True,
            content_hashing=True,
            track_updated_at=True,
        )
        assert cfg.table_name == "items"
        assert cfg.schema == "warehouse"
        assert cfg.soft_delete is True
        assert cfg.versioning is True
        assert cfg.content_hashing is True
        assert cfg.track_updated_at is True


class TestNodeConfigProperties:
    """NodeConfig computed properties."""

    def test_is_persisted_when_table_name_set(self):
        cfg = NodeConfig(table_name="events")
        assert cfg.is_persisted is True

    def test_is_persisted_when_table_name_none(self):
        cfg = NodeConfig()
        assert cfg.is_persisted is False

    def test_has_audit_fields_false_when_all_disabled(self):
        cfg = NodeConfig()
        assert cfg.has_audit_fields is False

    def test_has_audit_fields_true_when_content_hashing(self):
        cfg = NodeConfig(content_hashing=True)
        assert cfg.has_audit_fields is True

    def test_has_audit_fields_true_when_soft_delete(self):
        cfg = NodeConfig(soft_delete=True)
        assert cfg.has_audit_fields is True

    def test_has_audit_fields_true_when_versioning(self):
        cfg = NodeConfig(versioning=True)
        assert cfg.has_audit_fields is True

    def test_has_audit_fields_true_when_track_updated_at(self):
        cfg = NodeConfig(track_updated_at=True)
        assert cfg.has_audit_fields is True

    def test_has_audit_fields_true_when_multiple_flags(self):
        cfg = NodeConfig(soft_delete=True, versioning=True)
        assert cfg.has_audit_fields is True


class TestNodeConfigImmutability:
    """NodeConfig frozen=True enforcement."""

    def test_cannot_set_table_name(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.table_name = "oops"

    def test_cannot_set_schema(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.schema = "oops"

    def test_cannot_set_soft_delete(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.soft_delete = True

    def test_cannot_set_versioning(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.versioning = True

    def test_cannot_set_content_hashing(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.content_hashing = True

    def test_cannot_set_track_updated_at(self):
        cfg = NodeConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.track_updated_at = True


class TestComputeContentHash:
    """compute_hash input variants and determinism."""

    def test_none_content(self):
        result = _content_hash(None)
        expected = hashlib.sha256(b"null").hexdigest()
        assert result == expected

    def test_string_content(self):
        result = _content_hash("hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert result == expected

    def test_empty_string_content(self):
        result = _content_hash("")
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected

    def test_bytes_content(self):
        data = b"\x00\x01\x02"
        result = _content_hash(data)
        expected = hashlib.sha256(data).hexdigest()
        assert result == expected

    def test_dict_content_deterministic(self):
        """Dict content produces a valid hash."""
        content = {"b": 2, "a": 1}
        result = _content_hash(content)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_dict_content_sort_keys(self):
        """Dict ordering does not affect the hash due to sort_keys=True."""
        h1 = _content_hash({"z": 1, "a": 2})
        h2 = _content_hash({"a": 2, "z": 1})
        assert h1 == h2

    def test_list_content(self):
        content = [1, 2, 3]
        result = _content_hash(content)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_nested_dict_content(self):
        content = {"outer": {"inner": [1, 2]}}
        result = _content_hash(content)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_integer_content(self):
        """Integers are JSON-serializable."""
        result = _content_hash(42)
        assert isinstance(result, str)
        assert len(result) == 64

    def test_determinism_same_content(self):
        """Same content always produces the same hash."""
        a = _content_hash({"key": "value"})
        b = _content_hash({"key": "value"})
        assert a == b

    def test_different_content_different_hash(self):
        a = _content_hash("alpha")
        b = _content_hash("beta")
        assert a != b

    def test_hash_is_64_char_hex(self):
        """SHA-256 hex digest is always 64 characters."""
        result = _content_hash("anything")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)


class TestCreateNodeBasic:
    """create_node factory: class creation, naming, config wiring."""

    def test_returns_a_type(self):
        cls = create_node("Widget")
        assert isinstance(cls, type)

    def test_class_name(self):
        cls = create_node("Widget")
        assert cls.__name__ == "Widget"

    def test_is_node_subclass(self):
        cls = create_node("Widget")
        assert issubclass(cls, Node)

    def test_node_config_set_on_class(self):
        cls = create_node("Widget", table_name="widgets", versioning=True)
        assert isinstance(cls.node_config, NodeConfig)
        assert cls.node_config.table_name == "widgets"
        assert cls.node_config.versioning is True

    def test_node_config_defaults(self):
        cls = create_node("Widget")
        cfg = cls.node_config
        assert cfg.table_name is None
        assert cfg.schema == "public"
        assert cfg.soft_delete is False
        assert cfg.versioning is False
        assert cfg.content_hashing is False
        assert cfg.track_updated_at is False

    def test_instances_are_nodes(self):
        cls = create_node("Widget")
        w = cls(content="hello")
        assert isinstance(w, Node)
        assert w.content == "hello"

    def test_doc_string_set(self):
        cls = create_node("Widget", doc="A widget node.")
        assert cls.__doc__ == "A widget node."

    def test_doc_string_not_set(self):
        cls = create_node("Widget")
        # When doc is not provided, __doc__ may be None or inherited; either
        # way it should not be "A widget node."
        assert cls.__doc__ != "A widget node."


class TestCreateNodeExtraFields:
    """create_node with extra_fields parameter."""

    def test_extra_field_available_on_instance(self):
        cls = create_node(
            "Job",
            extra_fields={"priority": (int, 0)},
        )
        job = cls(content="build")
        assert job.priority == 0

    def test_extra_field_custom_value(self):
        cls = create_node(
            "Job",
            extra_fields={"priority": (int, 0)},
        )
        job = cls(content="build", priority=5)
        assert job.priority == 5

    def test_multiple_extra_fields(self):
        cls = create_node(
            "Task",
            extra_fields={
                "priority": (int, 0),
                "label": (str, ""),
            },
        )
        t = cls(priority=3, label="urgent")
        assert t.priority == 3
        assert t.label == "urgent"

    def test_extra_field_optional_type(self):
        cls = create_node(
            "Memo",
            extra_fields={"note": (str | None, None)},
        )
        m = cls()
        assert m.note is None

    def test_extra_fields_none_means_no_extras(self):
        cls = create_node("Plain")
        p = cls(content="x")
        assert not hasattr(p, "priority")


class TestCreateNodeConfigPropagation:
    """create_node: config flags propagate to lifecycle methods."""

    def test_touch_increments_version(self):
        cls = create_node("Versioned", versioning=True)
        v = cls(content="data")
        v.touch()
        assert v.version == 1
        v.touch()
        assert v.version == 2

    def test_soft_delete_enabled(self):
        cls = create_node("Deletable", soft_delete=True)
        d = cls(content="bye")
        d.soft_delete()
        assert d.is_deleted is True

    def test_content_hashing_on_touch(self):
        cls = create_node("Hashed", content_hashing=True)
        h = cls(content="payload")
        h.touch()
        assert h.content_hash is not None
        assert len(h.content_hash) == 64

    def test_full_config(self):
        cls = create_node(
            "FullAudit",
            table_name="audits",
            soft_delete=True,
            versioning=True,
            content_hashing=True,
            track_updated_at=True,
        )
        cfg = cls.node_config
        assert cfg.table_name == "audits"
        assert cfg.soft_delete is True
        assert cfg.versioning is True
        assert cfg.content_hashing is True
        assert cfg.track_updated_at is True
        assert cfg.is_persisted is True
        assert cfg.has_audit_fields is True


class TestNodeTouch:
    """Node.touch() behaviour with various configs."""

    def test_touch_noop_on_base_node(self):
        """Base Node has node_config=None; touch is a no-op."""
        n = Node(content="hi")
        n.touch()
        assert "version" not in n.metadata
        assert "updated_at" not in n.metadata

    def test_touch_noop_when_config_none(self):
        """Subclass without config should also be a no-op."""

        class Bare(Node):
            pass

        b = Bare()
        b.touch()
        assert "version" not in b.metadata

    def test_touch_track_updated_at(self, monkeypatch):
        t0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2024, 6, 1, 12, 0, 5, tzinfo=timezone.utc)
        monkeypatch.setattr(element_mod, "now_utc", lambda: t0)
        monkeypatch.setattr(node_mod, "datetime", _frozen_datetime(t0))
        cls = create_node("Tracked", track_updated_at=True)
        t = cls(content="x")
        assert t.created_at == t0.timestamp()
        monkeypatch.setattr(node_mod, "datetime", _frozen_datetime(t1))
        t.touch()
        # updated_at reflects the touch() clock (t1) and orders strictly after the
        # node's actual construction time (created_at), not an arbitrary constant.
        assert t.updated_at == t1.isoformat()
        updated_at = datetime.fromisoformat(t.updated_at)
        assert updated_at == t1
        assert updated_at > datetime.fromtimestamp(t.created_at, tz=timezone.utc)

    def test_touch_versioning_starts_at_one(self):
        cls = create_node("V", versioning=True)
        v = cls()
        v.touch()
        assert v.version == 1

    def test_touch_versioning_increments(self):
        cls = create_node("V", versioning=True)
        v = cls()
        for expected in range(1, 6):
            v.touch()
            assert v.version == expected

    def test_touch_content_hashing_calls_rehash(self):
        cls = create_node("H", content_hashing=True)
        h = cls(content="data")
        h.touch()
        assert h.content_hash is not None
        expected_hash = _content_hash("data")
        assert h.content_hash == expected_hash

    def test_touch_by_param_sets_updated_by(self):
        cls = create_node("ByUser", track_updated_at=True)
        b = cls()
        b.touch(by="alice")
        assert b.metadata["updated_by"] == "alice"

    def test_touch_by_none_does_not_set_updated_by(self):
        cls = create_node("ByUser", track_updated_at=True)
        b = cls()
        b.touch()
        assert "updated_by" not in b.metadata

    def test_touch_by_non_string_coerced(self):
        cls = create_node("ByUser", track_updated_at=True)
        b = cls()
        b.touch(by=42)
        assert b.metadata["updated_by"] == "42"

    def test_touch_all_features_combined(self):
        cls = create_node(
            "All",
            track_updated_at=True,
            versioning=True,
            content_hashing=True,
        )
        a = cls(content="payload")
        a.touch(by="system")
        assert a.version == 1
        assert a.updated_at is not None
        assert a.metadata["updated_by"] == "system"
        assert a.content_hash is not None

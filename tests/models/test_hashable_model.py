# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for HashableModel."""

import orjson
import pytest
from pydantic import Field

from lionagi.models.hashable_model import HashableModel


class _Sample(HashableModel):
    name: str
    count: int = 0
    metadata: dict = Field(default_factory=dict)


class TestToDictModes:
    def test_to_dict_python(self):
        m = _Sample(name="x", count=3)
        d = m.to_dict(mode="python")
        assert d == {"name": "x", "count": 3, "metadata": {}}

    def test_to_dict_json_roundtrip_through_orjson(self):
        m = _Sample(name="x", count=1)
        d = m.to_dict(mode="json")
        assert d["name"] == "x"
        assert d["count"] == 1

    def test_to_dict_db_renames_metadata(self):
        m = _Sample(name="x", metadata={"k": "v"})
        d = m.to_dict(mode="db")
        assert "metadata" not in d
        assert d["node_metadata"] == {"k": "v"}

    def test_to_dict_db_without_metadata_key(self):
        class _Sparse(HashableModel):
            name: str

        m = _Sparse(name="x")
        d = m.to_dict(mode="db")
        assert d == {"name": "x"}

    def test_to_dict_unknown_mode_raises(self):
        m = _Sample(name="x")
        with pytest.raises(ValueError, match="Unsupported mode"):
            m.to_dict(mode="bogus")  # type: ignore[arg-type]


class TestFromDictModes:
    def test_from_dict_python(self):
        m = _Sample.from_dict({"name": "x", "count": 2}, mode="python")
        assert m.name == "x" and m.count == 2

    def test_from_dict_json_dict(self):
        m = _Sample.from_dict({"name": "x"}, mode="json")
        assert m.name == "x"

    def test_from_dict_json_string(self):
        payload = orjson.dumps({"name": "x", "count": 4}).decode()
        m = _Sample.from_dict(payload, mode="json")
        assert m.count == 4

    def test_from_dict_db_restores_metadata(self):
        m = _Sample.from_dict({"name": "x", "node_metadata": {"k": "v"}}, mode="db")
        assert m.metadata == {"k": "v"}

    def test_from_dict_db_without_node_metadata(self):
        m = _Sample.from_dict({"name": "x"}, mode="db")
        assert m.name == "x"

    def test_from_dict_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="Unsupported mode"):
            _Sample.from_dict({"name": "x"}, mode="bogus")  # type: ignore[arg-type]


class TestJsonRoundtrip:
    def test_to_json_decoded_str(self):
        m = _Sample(name="x", count=1)
        s = m.to_json(decode=True)
        assert isinstance(s, str)
        assert '"name":"x"' in s.replace(" ", "")

    def test_to_json_bytes(self):
        m = _Sample(name="x")
        b = m.to_json(decode=False)
        assert isinstance(b, bytes)

    def test_to_json_sorted_keys(self):
        m = _Sample(name="x", count=1)
        s = m.to_json()
        # sort_keys=True → 'count' appears before 'name'
        assert s.index("count") < s.index("name")

    def test_from_json_roundtrip(self):
        original = _Sample(name="x", count=9)
        restored = _Sample.from_json(original.to_json())
        assert restored == original


class TestHashing:
    def test_mutable_model_is_unhashable(self):
        m = _Sample(name="x", count=1)
        with pytest.raises(TypeError, match="unhashable type"):
            hash(m)

    def test_equal_models_remain_value_equal(self):
        a = _Sample(name="x", count=1)
        b = _Sample(name="x", count=1)
        assert a == b

    def test_mutation_changes_equality_without_corrupting_a_container(self):
        a = _Sample(name="x", count=1)
        b = _Sample(name="x", count=1)

        with pytest.raises(TypeError, match="unhashable type"):
            {a}  # pyright: ignore[reportUnhashable]

        b.count = 2
        assert a != b


class TestDefaultSerializer:
    def test_serializer_handles_nested_basemodel(self):
        class _Inner(HashableModel):
            v: int

        class _Outer(HashableModel):
            inner: _Inner

        o = _Outer(inner=_Inner(v=5))
        # Should serialize without raising via the default orjson serializer.
        s = o.to_json()
        assert '"v":5' in s.replace(" ", "")

    def test_serializer_caches_on_subsequent_calls(self):
        from lionagi.models import hashable_model as hm

        hm._DEFAULT_HASHABLE_SERIALIZER = None
        a = hm._get_default_hashable_serializer()
        b = hm._get_default_hashable_serializer()
        assert a is b

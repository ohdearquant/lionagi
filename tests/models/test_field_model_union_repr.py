"""Tests for FieldModel.__repr__ with union types."""

import types

from lionagi.models import FieldModel


class TestFieldModelUnionRepr:
    """Verify __repr__ handles types.UnionType (X | Y syntax) without raising."""

    def test_repr_with_union_type(self):
        """FieldModel with a UnionType base_type must not raise on repr()."""
        union = int | str
        assert isinstance(union, types.UnionType)

        fm = FieldModel(base_type=union)
        result = repr(fm)

        assert "FieldModel(" in result
        # The string representation should contain both type names
        assert "int" in result
        assert "str" in result

    def test_repr_with_multi_union_type(self):
        """Repr works for unions of more than two types."""
        union = int | str | float
        fm = FieldModel(base_type=union)
        result = repr(fm)

        assert "FieldModel(" in result
        assert "int" in result
        assert "str" in result
        assert "float" in result

    def test_repr_with_none_union(self):
        """Repr works for T | None union syntax."""
        union = int | None
        fm = FieldModel(base_type=union)
        result = repr(fm)

        assert "FieldModel(" in result
        assert "int" in result

    def test_repr_with_regular_type(self):
        """Regular types still work after the fix."""
        fm = FieldModel(base_type=int)
        assert repr(fm) == "FieldModel(int)"

    def test_repr_with_no_base_type(self):
        """Sentinel base_type renders as Any."""
        fm = FieldModel()
        assert repr(fm) == "FieldModel(Any)"

    def test_repr_union_with_nullable(self):
        """Union type repr includes nullable attr."""
        union = int | str
        fm = FieldModel(base_type=union, nullable=True)
        result = repr(fm)

        assert "nullable" in result
        assert "int" in result
        assert "str" in result

    def test_repr_generic_alias_type(self):
        """GenericAlias types (like list[int]) also render without error."""
        fm = FieldModel(base_type=list[int])
        result = repr(fm)
        assert "FieldModel(" in result
        # GenericAlias has no __name__, falls back to str()
        assert "list" in result

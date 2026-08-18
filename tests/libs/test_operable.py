"""Tests for lionagi/ln/types/operable.py"""

import dataclasses

import pytest

from lionagi.ln.types import Operable, Spec, Unset


class TestOperable:
    """Test Operable class."""

    def test_basic_creation(self):
        """Test basic Operable creation."""
        spec1 = Spec(str, name="field1")
        spec2 = Spec(int, name="field2")
        operable = Operable((spec1, spec2), name="TestModel")
        assert len(operable.__op_fields__) == 2
        assert operable.name == "TestModel"

    def test_empty_operable(self):
        """Test empty Operable."""
        operable = Operable()
        assert len(operable.__op_fields__) == 0
        assert operable.allowed() == set()

    def test_allowed(self):
        """Test allowed() method."""
        spec1 = Spec(str, name="field1")
        spec2 = Spec(int, name="field2")
        operable = Operable((spec1, spec2))
        allowed = operable.allowed()
        assert allowed == {"field1", "field2"}

    def test_field_names_preserve_declaration_order(self):
        specs = (
            Spec(str, name="delta"),
            Spec(int),
            Spec(float, name=Unset),
            Spec(bool, name="alpha"),
        )

        operable = Operable(specs)

        assert operable.field_names() == ("delta", "alpha")
        assert operable.allowed() == frozenset({"alpha", "delta"})

    def test_multiple_unnamed_specs_are_allowed(self):
        first = Spec(str)
        second = Spec(int)
        unresolved = Spec(float, name=Unset)

        operable = Operable((first, second, unresolved, Spec(bytes, name=Unset)))

        assert operable.__op_fields__[:3] == (first, second, unresolved)

    def test_check_allowed_valid(self):
        """Test check_allowed() with valid fields."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        assert operable.check_allowed("field1") is True

    def test_check_allowed_invalid_raises(self):
        """Test check_allowed() raises on invalid fields."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        with pytest.raises(ValueError, match="not allowed"):
            operable.check_allowed("field2")

    def test_check_allowed_as_boolean(self):
        """Test check_allowed() as_boolean mode."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        assert operable.check_allowed("field1", as_boolean=True) is True
        assert operable.check_allowed("field2", as_boolean=True) is False

    def test_get_existing(self):
        """Test get() with existing field."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        result = operable.get("field1")
        assert result is spec1

    def test_get_missing_returns_unset(self):
        """Test get() with missing field returns Unset."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        result = operable.get("field2")
        assert result is Unset

    def test_get_with_default(self):
        """Test get() with default value."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        result = operable.get("field2", default="custom_default")
        assert result == "custom_default"

    def test_get_specs_no_filter(self):
        """Test get_specs() without filters."""
        spec1 = Spec(str, name="field1")
        spec2 = Spec(int, name="field2")
        operable = Operable((spec1, spec2))
        specs = operable.get_specs()
        assert specs == (spec1, spec2)

    @pytest.mark.parametrize(
        "include",
        [
            ["delta", "alpha", "gamma"],
            ("delta", "alpha", "gamma"),
            {"delta", "alpha", "gamma"},
            frozenset({"delta", "alpha", "gamma"}),
        ],
    )
    def test_get_specs_include_preserves_declaration_order(self, include):
        """Include is a membership filter, never an ordering authority."""
        specs = (
            Spec(str, name="alpha"),
            Spec(int, name="beta"),
            Spec(bool, name="gamma"),
            Spec(bytes, name="delta"),
        )
        operable = Operable(specs)

        selected = operable.get_specs(include=include)

        assert selected == (specs[0], specs[2], specs[3])

    def test_get_specs_empty_include_selects_nothing(self):
        spec = Spec(str, name="field1")

        assert Operable((spec,)).get_specs(include=set()) == ()

    @pytest.mark.parametrize(
        "exclude",
        [
            ["field3", "field1"],
            ("field3", "field1"),
            {"field3", "field1"},
            frozenset({"field3", "field1"}),
        ],
    )
    def test_get_specs_exclude_preserves_declaration_order(self, exclude):
        """Exclude is a membership filter, never an ordering authority."""
        spec1 = Spec(str, name="field1")
        spec2 = Spec(int, name="field2")
        spec3 = Spec(bool, name="field3")
        operable = Operable((spec1, spec2, spec3))

        specs = operable.get_specs(exclude=exclude)

        assert specs == (spec2,)

    def test_get_specs_both_include_exclude_raises(self):
        """Test get_specs() raises when both include and exclude specified."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        with pytest.raises(ValueError, match="Cannot specify both"):
            operable.get_specs(include={"field1"}, exclude={"field2"})

    def test_get_specs_include_invalid_raises(self):
        """Test get_specs() raises when include contains invalid fields."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        with pytest.raises(ValueError) as exc_info:
            operable.get_specs(include={"zeta", "field1", "alpha"})

        assert str(exc_info.value) == ("Some specified fields are not allowed: ['alpha', 'zeta']")

    def test_get_specs_exclude_invalid_raises_deterministically(self):
        operable = Operable((Spec(str, name="field1"),))

        with pytest.raises(ValueError) as exc_info:
            operable.get_specs(exclude={"zeta", "alpha"})

        assert str(exc_info.value) == ("Some specified fields are not allowed: ['alpha', 'zeta']")

    def test_field_ordering_preserved(self):
        specs = [
            Spec(str, name="field_a"),
            Spec(int, name="field_b"),
            Spec(bool, name="field_c"),
        ]
        operable = Operable(tuple(specs))

        # Verify order preserved
        field_names = [s.name for s in operable.__op_fields__]
        assert field_names == ["field_a", "field_b", "field_c"]

        # Verify it's a tuple
        assert isinstance(operable.__op_fields__, tuple)

    def test_create_model_unsupported_adapter(self):
        """Test create_model() raises on unsupported adapter."""
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        with pytest.raises(ValueError, match="Unsupported adapter"):
            operable.create_model(adapter="unsupported")  # type: ignore

    def test_immutability(self):
        spec1 = Spec(str, name="field1")
        operable = Operable((spec1,))
        with pytest.raises(dataclasses.FrozenInstanceError):  # frozen dataclass (not pydantic)
            operable.name = "new_name"

    def test_type_validation(self):
        with pytest.raises(TypeError, match="All specs must be Spec objects"):
            Operable(("not_a_spec",))

    def test_duplicate_name_detection(self):
        spec1 = Spec(str, name="field1")
        spec2 = Spec(int, name="field1")  # Duplicate name
        with pytest.raises(ValueError, match="Duplicate field names found"):
            Operable((spec1, spec2))

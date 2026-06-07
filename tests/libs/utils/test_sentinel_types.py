"""Test suite for sentinel types (Undefined, Unset) - TDD Specification Implementation."""

import copy
import pickle

import pytest

from lionagi.ln.types import (
    MaybeSentinel,
    MaybeUndefined,
    MaybeUnset,
    Undefined,
    UndefinedType,
    Unset,
    UnsetType,
    is_sentinel,
    not_sentinel,
)


class TestSentinelTypesIntegrity:
    """TestSuite: SentinelTypesIntegrity - Singleton integrity, boolean behavior, and immutability."""

    def test_singleton_identity_undefined(self):
        """Test: SingletonIdentity (CRITICAL) for UndefinedType.

        GIVEN the UndefinedType class
        WHEN creating multiple instances (A = UndefinedType(), B = UndefinedType())
        THEN A must be the same object as B (A is B)
        AND A must be the same object as the global 'Undefined' constant.
        """
        a = UndefinedType()
        b = UndefinedType()
        assert a is b, "Multiple UndefinedType instances must be the same object"
        assert a is Undefined, "UndefinedType instance must be the global Undefined"
        assert b is Undefined, "All UndefinedType instances must be the global Undefined"

    def test_singleton_identity_unset(self):
        """Test: SingletonIdentity (CRITICAL) for UnsetType.

        GIVEN the UnsetType class
        WHEN creating multiple instances (A = UnsetType(), B = UnsetType())
        THEN A must be the same object as B (A is B)
        AND A must be the same object as the global 'Unset' constant.
        """
        a = UnsetType()
        b = UnsetType()
        assert a is b, "Multiple UnsetType instances must be the same object"
        assert a is Unset, "UnsetType instance must be the global Unset"
        assert b is Unset, "All UnsetType instances must be the global Unset"

    def test_distinct_identities(self):
        """Test: DistinctIdentities

        THEN Undefined must not be the same object as Unset.
        """
        assert Undefined is not Unset, "Undefined and Unset must be distinct objects"
        assert Undefined != Unset, "Undefined and Unset must not be equal"

    def test_immutability_under_copy_undefined(self):
        """Test: ImmutabilityUnderCopy (CRITICAL: State Safety) for Undefined.

        WHEN performing copy.copy() or copy.deepcopy() on Undefined
        THEN the result must be the original Undefined object (verify using 'is').
        """
        shallow_copy = copy.copy(Undefined)
        assert shallow_copy is Undefined, "copy.copy(Undefined) must return the same object"

        deep_copy = copy.deepcopy(Undefined)
        assert deep_copy is Undefined, "copy.deepcopy(Undefined) must return the same object"

    def test_immutability_under_copy_unset(self):
        """Test: ImmutabilityUnderCopy (CRITICAL: State Safety) for Unset.

        WHEN performing copy.copy() or copy.deepcopy() on Unset
        THEN the result must be the original Unset object (verify using 'is').
        """
        shallow_copy = copy.copy(Unset)
        assert shallow_copy is Unset, "copy.copy(Unset) must return the same object"

        deep_copy = copy.deepcopy(Unset)
        assert deep_copy is Unset, "copy.deepcopy(Unset) must return the same object"

    def test_pickle_preservation(self):
        """Test that sentinels survive pickling/unpickling.

        This is important for distributed systems and caching.
        """
        # Test Undefined
        pickled_undefined = pickle.dumps(Undefined)
        unpickled_undefined = pickle.loads(pickled_undefined)
        assert unpickled_undefined is Undefined, "Unpickled Undefined must be the same object"

        # Test Unset
        pickled_unset = pickle.dumps(Unset)
        unpickled_unset = pickle.loads(pickled_unset)
        assert unpickled_unset is Unset, "Unpickled Unset must be the same object"


class TestSentinelTypesBehavior:
    """TestSuite: SentinelTypesBehavior - Boolean evaluation and helper functions."""

    def test_boolean_evaluation_falsy(self):
        """Test: BooleanEvaluation (Falsy)

        WHEN evaluating bool(Undefined) or bool(Unset)
        THEN the result must be False.
        """
        assert not bool(Undefined), "bool(Undefined) must be False"
        assert not bool(Unset), "bool(Unset) must be False"

        # Also test in conditionals
        if Undefined:
            pytest.fail("Undefined evaluated as truthy in conditional")
        if Unset:
            pytest.fail("Unset evaluated as truthy in conditional")

    def test_helper_function_is_sentinel(self):
        """Test: HelperFunctions - is_sentinel

        GIVEN input=Undefined THEN is_sentinel() is True.
        GIVEN input=None THEN is_sentinel() is False (Crucial distinction).
        GIVEN input=0 THEN is_sentinel() is False.
        """
        # Test with sentinels
        assert is_sentinel(Undefined) is True, "is_sentinel(Undefined) must be True"
        assert is_sentinel(Unset) is True, "is_sentinel(Unset) must be True"

        # Test with non-sentinels (crucial distinctions)
        assert is_sentinel(None) is False, "is_sentinel(None) must be False"
        assert is_sentinel(0) is False, "is_sentinel(0) must be False"
        assert is_sentinel(False) is False, "is_sentinel(False) must be False"
        assert is_sentinel("") is False, "is_sentinel('') must be False"
        assert is_sentinel([]) is False, "is_sentinel([]) must be False"
        assert is_sentinel({}) is False, "is_sentinel({}) must be False"

    def test_helper_function_not_sentinel(self):
        """Test: HelperFunctions - not_sentinel

        Inverse of is_sentinel for filtering operations.
        """
        # Test with sentinels
        assert not_sentinel(Undefined) is False, "not_sentinel(Undefined) must be False"
        assert not_sentinel(Unset) is False, "not_sentinel(Unset) must be False"

        # Test with non-sentinels
        assert not_sentinel(None) is True, "not_sentinel(None) must be True"
        assert not_sentinel(0) is True, "not_sentinel(0) must be True"
        assert not_sentinel("value") is True, "not_sentinel('value') must be True"

    def test_string_representation(self):
        """Test string representations of sentinels."""
        assert repr(Undefined) == "Undefined"
        assert str(Undefined) == "Undefined"
        assert repr(Unset) == "Unset"
        assert str(Unset) == "Unset"

    def test_type_annotations(self):
        """Test that type annotations work correctly with sentinels."""

        # Test MaybeUndefined
        def func_undefined(x: MaybeUndefined[int]) -> bool:
            return x is Undefined

        assert func_undefined(Undefined) is True
        assert func_undefined(5) is False

        # Test MaybeUnset
        def func_unset(x: MaybeUnset[str]) -> bool:
            return x is Unset

        assert func_unset(Unset) is True
        assert func_unset("hello") is False

        # Test MaybeSentinel
        def func_sentinel(x: MaybeSentinel[float]) -> bool:
            return is_sentinel(x)

        assert func_sentinel(Undefined) is True
        assert func_sentinel(Unset) is True
        assert func_sentinel(3.14) is False

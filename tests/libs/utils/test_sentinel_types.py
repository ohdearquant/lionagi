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
    def test_singleton_identity_undefined(self):
        a = UndefinedType()
        b = UndefinedType()
        assert a is b, "Multiple UndefinedType instances must be the same object"
        assert a is Undefined, "UndefinedType instance must be the global Undefined"
        assert b is Undefined, "All UndefinedType instances must be the global Undefined"

    def test_singleton_identity_unset(self):
        a = UnsetType()
        b = UnsetType()
        assert a is b, "Multiple UnsetType instances must be the same object"
        assert a is Unset, "UnsetType instance must be the global Unset"
        assert b is Unset, "All UnsetType instances must be the global Unset"

    def test_distinct_identities(self):
        assert Undefined is not Unset, "Undefined and Unset must be distinct objects"
        assert Undefined != Unset, "Undefined and Unset must not be equal"

    def test_immutability_under_copy_undefined(self):
        shallow_copy = copy.copy(Undefined)
        assert shallow_copy is Undefined, "copy.copy(Undefined) must return the same object"

        deep_copy = copy.deepcopy(Undefined)
        assert deep_copy is Undefined, "copy.deepcopy(Undefined) must return the same object"

    def test_immutability_under_copy_unset(self):
        shallow_copy = copy.copy(Unset)
        assert shallow_copy is Unset, "copy.copy(Unset) must return the same object"

        deep_copy = copy.deepcopy(Unset)
        assert deep_copy is Unset, "copy.deepcopy(Unset) must return the same object"

    def test_pickle_preservation(self):
        # Test Undefined
        pickled_undefined = pickle.dumps(Undefined)
        unpickled_undefined = pickle.loads(pickled_undefined)
        assert unpickled_undefined is Undefined, "Unpickled Undefined must be the same object"

        # Test Unset
        pickled_unset = pickle.dumps(Unset)
        unpickled_unset = pickle.loads(pickled_unset)
        assert unpickled_unset is Unset, "Unpickled Unset must be the same object"


class TestSentinelTypesBehavior:
    def test_type_annotations(self):
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

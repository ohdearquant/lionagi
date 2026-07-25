# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Adaptable/AsyncAdaptable registry inheritance (lionagi/adapters/_base.py)
and Pile.list_adapters (lionagi/protocols/generic/pile.py)."""

from lionagi.adapters._base import Adaptable, AsyncAdaptable
from lionagi.protocols.generic.pile import Pile


class _FakeAdapter:
    obj_key = "fake_sync"

    @classmethod
    def from_obj(cls, subj_cls, obj, /, *, many=False, adapt_meth="model_validate", **kw):
        return None

    @classmethod
    def to_obj(cls, subj, /, *, many=False, adapt_meth="model_dump", **kw):
        return None


class _FakeAsyncAdapter:
    obj_key = "fake_async"

    @classmethod
    async def from_obj(cls, subj_cls, obj, /, *, many=False, adapt_meth="model_validate", **kw):
        return None

    @classmethod
    async def to_obj(cls, subj, /, *, many=False, adapt_meth="model_dump", **kw):
        return None


class TestAdaptableInheritance:
    def test_subclass_sees_base_registrations(self):
        class Base(Adaptable):
            pass

        class Derived(Base):
            pass

        Base.register_adapter(_FakeAdapter)

        assert "fake_sync" in Derived._registry()._reg
        assert Derived._registry() is not Base._registry()

    def test_derived_registration_does_not_leak_to_base_or_sibling(self):
        class Base(Adaptable):
            pass

        class Derived(Base):
            pass

        class Sibling(Base):
            pass

        class _OnlyOnDerived:
            obj_key = "derived_only"

            @classmethod
            def from_obj(cls, subj_cls, obj, /, **kw):
                return None

            @classmethod
            def to_obj(cls, subj, /, **kw):
                return None

        Derived.register_adapter(_OnlyOnDerived)

        assert "derived_only" in Derived._registry()._reg
        assert "derived_only" not in Base._registry()._reg
        assert "derived_only" not in Sibling._registry()._reg


class TestAsyncAdaptableInheritance:
    def test_base_initialized_first_does_not_leak_child_registration(self):
        """Regression: initializing the base registry before a child
        registers its own adapter must not make that adapter visible
        through the base or a sibling subclass."""

        class Base(AsyncAdaptable):
            pass

        class Child(Base):
            pass

        class Sibling(Base):
            pass

        Base._areg()  # force base registry creation first
        Child.register_async_adapter(_FakeAsyncAdapter)

        assert "fake_async" in Child._areg()._reg
        assert "fake_async" not in Base._areg()._reg
        assert "fake_async" not in Sibling._areg()._reg


class TestPileListAdapters:
    def test_list_adapters_does_not_raise(self):
        # Historically raised AttributeError: _adapter_registry when the
        # private registry attribute had never been lazily created yet.
        keys = Pile.list_adapters()
        assert "json" in keys
        assert "csv" in keys

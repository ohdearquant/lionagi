# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from lionagi.adapters._base import Adaptable, AsyncAdaptable


class _SyncBase(Adaptable):
    pass


class _SyncChild(_SyncBase):
    pass


class _SyncSibling(_SyncBase):
    pass


class _AsyncBase(AsyncAdaptable):
    pass


class _AsyncChild(_AsyncBase):
    pass


class _AsyncSibling(_AsyncBase):
    pass


class _FakeAdapter:
    adapter_key = "fake"


class _FakeAsyncAdapter:
    obj_key = "fake_async"


def test_sync_registration_on_base_is_visible_to_derived_class():
    _SyncBase.register_adapter(_FakeAdapter)

    assert "fake" in _SyncBase._registry()._reg
    assert "fake" in _SyncChild._registry()._reg


def test_sync_registration_on_child_does_not_leak_to_base_or_sibling():
    _SyncBase.register_adapter(_FakeAdapter)

    class _OnlyChildAdapter:
        adapter_key = "child_only"

    _SyncChild.register_adapter(_OnlyChildAdapter)

    assert "child_only" in _SyncChild._registry()._reg
    assert "child_only" not in _SyncBase._registry()._reg
    assert "child_only" not in _SyncSibling._registry()._reg


def test_async_registration_on_base_is_visible_to_derived_class():
    _AsyncBase.register_async_adapter(_FakeAsyncAdapter)

    assert "fake_async" in _AsyncBase._areg()._reg
    assert "fake_async" in _AsyncChild._areg()._reg


def test_async_registration_on_child_does_not_leak_to_base_or_sibling():
    _AsyncBase.register_async_adapter(_FakeAsyncAdapter)

    class _OnlyChildAsyncAdapter:
        obj_key = "child_only_async"

    _AsyncChild.register_async_adapter(_OnlyChildAsyncAdapter)

    assert "child_only_async" in _AsyncChild._areg()._reg
    assert "child_only_async" not in _AsyncBase._areg()._reg
    assert "child_only_async" not in _AsyncSibling._areg()._reg


def test_pile_list_adapters_does_not_raise():
    from lionagi.protocols.generic.pile import Pile

    adapters = Pile.list_adapters()
    assert isinstance(adapters, list)
    assert "json" in adapters

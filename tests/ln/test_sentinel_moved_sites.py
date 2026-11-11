# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Legacy sentinel policy lookup for moved lexical owners."""

from __future__ import annotations

import pytest

from lionagi import _moved
from lionagi.ln.types._sentinel import _compat_policy

_OLD_SITE = "lionagi.ln._async_call.AlcallParams._config"
_NEW_SITE = "example.async_call.AlcallParams._config"


@pytest.fixture(autouse=True)
def clear_policy_cache():
    _compat_policy.cache_clear()
    yield
    _compat_policy.cache_clear()


def _install_row(
    monkeypatch,
    old: str = "lionagi.ln._async_call",
    new: str = "example.async_call",
):
    monkeypatch.setattr(
        _moved,
        "MOVED",
        (_moved.Moved(old, new, "0.0.0", "2.0.0"),),
    )


def test_moved_site_uses_the_allowlisted_legacy_owner(monkeypatch):
    _install_row(monkeypatch)

    assert _compat_policy(site=_NEW_SITE, none_as_sentinel=True).none_as_sentinel


def test_moved_site_does_not_authorize_an_unlisted_axis(monkeypatch):
    _install_row(monkeypatch)

    with pytest.raises(ValueError, match="axis 'empty' is not allowlisted"):
        _compat_policy(site=_NEW_SITE, empty_as_sentinel=True)


def test_site_without_a_moved_row_is_refused(monkeypatch):
    monkeypatch.setattr(_moved, "MOVED", ())

    with pytest.raises(ValueError, match="axis 'none' is not allowlisted"):
        _compat_policy(site=_NEW_SITE, none_as_sentinel=True)


def test_moved_site_with_unallowlisted_legacy_owner_is_refused(monkeypatch):
    _install_row(monkeypatch, old="lionagi.casts.pattern.Unlisted")

    with pytest.raises(ValueError, match="axis 'none' is not allowlisted"):
        _compat_policy(site=_NEW_SITE, none_as_sentinel=True)


def test_reverse_lookup_respects_dotted_prefix_boundaries(monkeypatch):
    _install_row(monkeypatch, old="legacy", new="new")

    assert _moved.resolve_legacy("new.pattern.Pattern") == "legacy.pattern.Pattern"
    assert _moved.resolve_legacy("newx.pattern.Pattern") is None


def test_reverse_lookup_uses_the_longest_new_prefix(monkeypatch):
    outer = _moved.Moved("legacy", "example", "0.0.0", "2.0.0")
    inner = _moved.Moved("legacy.special", "example.special", "0.0.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (outer, inner))

    assert _moved.resolve_legacy("example.special.module") == "legacy.special.module"

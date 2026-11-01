# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Public CLI provider errors retain their existing identities."""

from __future__ import annotations

import pytest

from lionagi import _provider_errors as legacy
from lionhub.providers import errors as public

EXPECTED = {
    "ProviderError",
    "ProviderQuotaError",
    "ProviderAuthError",
    "ProviderContextError",
    "ProviderCapacityError",
    "ProviderUnsupportedModelError",
    "ProviderSafetyError",
    "ProviderStreamDisconnectError",
    "ProviderTeardownError",
    "ProviderPermissionError",
    "ProviderAdapterError",
    "WorkerLivenessError",
    "SubagentAdmissionRefusedError",
    "NestedAgentRefusedError",
}


def test_public_error_exports_are_exact_and_exclude_other_private_module_names():
    assert len(public.__all__) == 14
    assert set(public.__all__) == EXPECTED
    for name in ("classify_provider_error", "EmissionError", "_WorkerSpawnAdmissionError"):
        assert name not in public.__all__
        assert not hasattr(public, name)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_public_name_is_the_original_error_class(name):
    cls = getattr(public, name)
    assert cls is getattr(legacy, name)
    assert issubclass(cls, legacy.ProviderError)
    assert issubclass(cls, RuntimeError)


def test_legacy_classified_error_is_catchable_through_public_base_and_specific_type():
    specific = legacy.classify_provider_error("anything", status=429)
    assert type(specific) is public.ProviderQuotaError
    with pytest.raises(public.ProviderQuotaError) as caught:
        raise specific
    assert caught.value is specific

    again = legacy.classify_provider_error("anything", status=429)
    with pytest.raises(public.ProviderError) as caught_base:
        raise again
    assert caught_base.value is again

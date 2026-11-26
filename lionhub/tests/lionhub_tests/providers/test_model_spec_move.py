# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The provider tables moved from ``lionagi.service.providers`` to ``lionhub.providers.model_spec``.

Each old name still resolves through the core module, to the hub's own object. The effort field
each CLI endpoint class names for ``iModel`` agrees with the hub's ``PROVIDER_EFFORT_KWARG``,
which the agent and orchestration layers read, so the two never route one provider apart.
"""

from __future__ import annotations

import warnings

import pytest

import lionagi.service.providers as core
from lionagi.service.connections.registry import EndpointRegistry
from lionhub.providers import model_spec


def test_every_old_name_resolves_to_the_hub_object():
    forwarded = core._MODEL_SPEC_NAMES
    assert set(model_spec.__all__) <= forwarded
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for name in sorted(forwarded):
            assert getattr(core, name) is getattr(model_spec, name), name


def test_the_core_names_are_the_core_objects_the_hub_module_uses():
    assert model_spec.normalize_effort is core.normalize_effort
    assert model_spec.split_effort_suffix is core.split_effort_suffix


def test_an_unknown_name_is_an_attribute_error_without_importing():
    with pytest.raises(AttributeError, match="no attribute 'not_a_table'"):
        core.not_a_table  # noqa: B018 -- the attribute access is the test
    assert not hasattr(core, "also_not_a_table")


def test_each_effort_provider_class_names_the_table_field():
    declared = {}
    for provider in sorted(model_spec.PROVIDER_EFFORT_KWARG):
        endpoint_class = EndpointRegistry.declared_endpoint_class(provider)
        assert endpoint_class is not None, provider
        declared[provider] = getattr(endpoint_class, "effort_kwarg", None)
    assert declared == model_spec.PROVIDER_EFFORT_KWARG


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "gemini_code"])
def test_a_provider_outside_the_table_names_no_field(provider):
    endpoint_class = EndpointRegistry.declared_endpoint_class(provider)
    assert endpoint_class is not None, provider
    assert getattr(endpoint_class, "effort_kwarg", None) is None

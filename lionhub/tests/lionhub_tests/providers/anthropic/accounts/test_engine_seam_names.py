# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The account engine uses the core seam's own objects, never copies of them."""

from lionagi.service.accounts import _request_guard, _types, _usage_shape
from lionhub.providers.anthropic.accounts import _credentials, _pricing, _router


def test_the_engine_reads_the_seam_objects():
    assert _router.validate_routed_request is _request_guard.validate_routed_request
    assert _pricing.cache_write_split is _usage_shape.cache_write_split
    assert _pricing.CACHE_WRITE_LIFETIMES is _usage_shape.CACHE_WRITE_LIFETIMES
    assert _credentials.AccountRoutingError is _types.AccountRoutingError

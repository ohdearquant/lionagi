# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from .endpoint import Endpoint
from .registry import EndpointRegistry

__all__ = ("match_endpoint",)


def match_endpoint(
    provider: str,
    endpoint: str,
    **kwargs,
) -> Endpoint:
    """Match a provider + endpoint to an Endpoint class via EndpointRegistry."""
    return EndpointRegistry.match(provider, endpoint, **kwargs)

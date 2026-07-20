# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from .endpoint import Endpoint
from .registry import EndpointRegistry

__all__ = ("match_endpoint",)


def match_endpoint(
    provider: str,
    endpoint: str,
    *,
    openai_compatible: bool = False,
    **kwargs,
) -> Endpoint:
    """Match a provider + endpoint to an Endpoint class via EndpointRegistry.

    An unrecognized ``provider`` raises ``ProviderNotFoundError`` unless
    ``openai_compatible=True`` explicitly authorizes routing it to the
    generic OpenAI-compatible endpoint (or ``base_url=`` is given, which
    authorizes the same fallback with a deprecation warning).
    """
    return EndpointRegistry.match(provider, endpoint, openai_compatible=openai_compatible, **kwargs)

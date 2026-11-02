# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING

from .endpoint import Endpoint
from .registry import EndpointRegistry

if TYPE_CHECKING:
    from .provider_catalog import ProviderCatalog

__all__ = ("match_endpoint",)


def match_endpoint(
    provider: str,
    endpoint: str,
    *,
    openai_compatible: bool = False,
    provider_catalog: ProviderCatalog | None = None,
    **kwargs,
) -> Endpoint:
    """Match a provider + endpoint to an Endpoint class via EndpointRegistry.

    An unrecognized ``provider`` raises ``ProviderNotFoundError`` unless
    ``openai_compatible=True`` explicitly authorizes routing it to the
    generic OpenAI-compatible endpoint (or ``base_url=`` is given, which
    authorizes the same fallback with a deprecation warning).

    ``provider_catalog``, when given, is the only source of endpoint declarations: the
    bundled catalog, the registered endpoints and the plugin registry are not consulted, an
    empty catalog selects nothing, and a provider it does not declare is rejected with an
    error naming the catalog. Omitting it keeps the default selection.
    """
    return EndpointRegistry.match(
        provider,
        endpoint,
        openai_compatible=openai_compatible,
        provider_catalog=provider_catalog,
        **kwargs,
    )

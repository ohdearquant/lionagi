# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exa findSimilar endpoint (POST /findSimilar)."""

from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import ExaConfigs

__all__ = ("ExaFindSimilarEndpoint",)


@ExaConfigs.FIND_SIMILAR.register
class ExaFindSimilarEndpoint(Endpoint):
    """Exa findSimilar endpoint — discover pages semantically similar to a URL."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

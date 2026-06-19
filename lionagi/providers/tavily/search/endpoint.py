# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import TavilyConfigs

__all__ = ("TavilySearchEndpoint", "TavilyExtractEndpoint")


@TavilyConfigs.SEARCH.register
class TavilySearchEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)


@TavilyConfigs.EXTRACT.register
class TavilyExtractEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exa contents extraction endpoint (POST /contents)."""

from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import ExaConfigs

__all__ = ("ExaContentsEndpoint",)


@ExaConfigs.CONTENTS.register
class ExaContentsEndpoint(Endpoint):
    """Exa contents endpoint — extract page text, highlights, and summaries from URLs."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.EXA_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

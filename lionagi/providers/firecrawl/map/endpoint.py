from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import FirecrawlConfigs


@FirecrawlConfigs.MAP.register
class FirecrawlMapEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.FIRECRAWL_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

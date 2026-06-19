from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import FirecrawlConfigs


@FirecrawlConfigs.SCRAPE.register
class FirecrawlScrapeEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)

from lionagi.service.connections.endpoint import Endpoint

from .._config import OpenAIConfigs


@OpenAIConfigs.EMBED.register
class OpenaiEmbedEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "text-embedding-3-small"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

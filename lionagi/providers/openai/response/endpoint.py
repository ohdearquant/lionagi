from lionagi.service.connections.endpoint import Endpoint

from .._config import OpenAIConfigs


@OpenAIConfigs.RESPONSE.register
class OpenaiResponseEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.OPENAI_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

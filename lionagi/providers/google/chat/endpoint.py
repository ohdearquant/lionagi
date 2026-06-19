from lionagi.service.connections.endpoint import Endpoint

from .._config import GeminiChatConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
}


@GeminiChatConfigs.CHAT.register
class GeminiChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "gemini-2.5-flash"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

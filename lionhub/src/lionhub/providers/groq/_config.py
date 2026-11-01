from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class GroqConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.service.connections._chat_schemas:OpenAIChatCompletionsRequest"),
        "https://api.groq.com/openai/v1",
        "bearer",
    )
    AUDIO_TRANSCRIPTION = (
        "audio/transcriptions",
        ["whisper", "stt"],
        EndpointType.API,
        LazyType("lionhub.providers.openai._audio_schemas:AudioTranscriptionRequest"),
        "https://api.groq.com/openai/v1",
        "bearer",
    )


GroqConfigs._PROVIDER = "groq"
GroqConfigs._PROVIDER_ALIASES = []
GroqConfigs._API_KEY_ENV = "GROQ_API_KEY"


CONTEXT_WINDOWS: dict[str, int] = {
    "llama-3.3-70b-versatile": 128_000,
    "llama-3.1-8b-instant": 128_000,
    "mixtral-8x7b-32768": 32_768,
    "gemma2-9b-it": 8_192,
}

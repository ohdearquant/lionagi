from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class GroqConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.openai._chat_schemas:OpenAIChatCompletionsRequest"),
        "https://api.groq.com/openai/v1",
        "bearer",
    )
    AUDIO_TRANSCRIPTION = (
        "audio/transcriptions",
        ["whisper", "stt"],
        EndpointType.API,
        LazyType("lionagi.providers.openai._audio_schemas:AudioTranscriptionRequest"),
        "https://api.groq.com/openai/v1",
        "bearer",
    )


GroqConfigs._PROVIDER = "groq"
GroqConfigs._PROVIDER_ALIASES = []
GroqConfigs._API_KEY_ENV = "GROQ_API_KEY"

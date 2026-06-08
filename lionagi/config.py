# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import Any, ClassVar

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CacheConfig(BaseModel):
    ttl: int = 300
    key: str | None = None
    namespace: str | None = None
    key_builder: Any = None
    skip_cache_func: Any = lambda _: False
    serializer: dict[str, Any] | None = None
    plugins: Any = None
    alias: str | None = None
    noself: Any = lambda _: False

    def as_kwargs(self) -> dict[str, Any]:
        raw = self.model_dump(exclude_none=True)
        unserialisable_keys = (
            "key_builder",
            "skip_cache_func",
            "noself",
            "serializer",
            "plugins",
        )
        for key in unserialisable_keys:
            raw.pop(key, None)
        return raw


class AppSettings(BaseSettings, frozen=True):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local", ".secrets.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    aiocache_config: CacheConfig = Field(
        default_factory=CacheConfig, description="Cache settings for aiocache"
    )

    OPENAI_API_KEY: SecretStr | None = None
    OPENROUTER_API_KEY: SecretStr | None = None
    OLLAMA_API_KEY: SecretStr | None = None
    EXA_API_KEY: SecretStr | None = None
    TAVILY_API_KEY: SecretStr | None = None
    FIRECRAWL_API_KEY: SecretStr | None = None
    PERPLEXITY_API_KEY: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    NVIDIA_NIM_API_KEY: SecretStr | None = None
    GEMINI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None

    OPENAI_DEFAULT_MODEL: str = "gpt-4.1-mini"

    LIONAGI_EMBEDDING_PROVIDER: str = "openai"
    LIONAGI_EMBEDDING_MODEL: str = "text-embedding-3-small"

    LIONAGI_CHAT_PROVIDER: str = "openai"
    LIONAGI_CHAT_MODEL: str = "gpt-4.1-mini"

    LIONAGI_AUTO_STORE_EVENT: bool = False
    LIONAGI_STORAGE_PROVIDER: str = "async_qdrant"

    LIONAGI_AUTO_EMBED_LOG: bool = False

    LIONAGI_QDRANT_URL: str = "http://localhost:6333"
    LIONAGI_DEFAULT_QDRANT_COLLECTION: str = "event_logs"

    LOG_PERSIST_DIR: str = "./data/logs"
    LOG_SUBFOLDER: str | None = None
    LOG_CAPACITY: int = 50
    LOG_EXTENSION: str = ".json"
    LOG_USE_TIMESTAMP: bool = True
    LOG_HASH_DIGITS: int = 5
    LOG_FILE_PREFIX: str = "log"
    LOG_AUTO_SAVE_ON_EXIT: bool = True
    LOG_CLEAR_AFTER_DUMP: bool = True

    _instance: ClassVar[Any] = None

    def get_secret(self, key_name: str) -> str:
        if not hasattr(self, key_name):
            if "ollama" in key_name.lower():
                return "ollama"
            raise AttributeError(f"Secret key '{key_name}' not found in settings")

        secret = getattr(self, key_name)
        if secret is None:
            if "ollama" in key_name.lower():
                return "ollama"
            raise ValueError(f"Secret key '{key_name}' is not set")

        if isinstance(secret, SecretStr):
            return secret.get_secret_value()

        return str(secret)

    @property
    def LOG_CONFIG(self) -> dict[str, Any]:
        return {
            "persist_dir": self.LOG_PERSIST_DIR,
            "subfolder": self.LOG_SUBFOLDER,
            "capacity": self.LOG_CAPACITY,
            "extension": self.LOG_EXTENSION,
            "use_timestamp": self.LOG_USE_TIMESTAMP,
            "hash_digits": self.LOG_HASH_DIGITS,
            "file_prefix": self.LOG_FILE_PREFIX,
            "auto_save_on_exit": self.LOG_AUTO_SAVE_ON_EXIT,
            "clear_after_dump": self.LOG_CLEAR_AFTER_DUMP,
        }


settings = AppSettings()
AppSettings._instance = settings

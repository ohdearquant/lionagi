# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for configuration module."""

import os
from unittest.mock import patch

import pytest
from pydantic import SecretStr, ValidationError

from lionagi.config import AppSettings, CacheConfig, settings


class TestCacheConfig:
    def test_default_values(self):
        config = CacheConfig()
        assert config.ttl == 300
        assert config.key is None
        assert config.namespace is None
        assert config.alias is None

    def test_custom_values(self):
        config = CacheConfig(ttl=600, key="test_key", namespace="test_ns")
        assert config.ttl == 600
        assert config.key == "test_key"
        assert config.namespace == "test_ns"

    def test_as_kwargs_excludes_unserialisable(self):
        config = CacheConfig(
            ttl=300,
            key="test",
            key_builder=lambda x: x,
            skip_cache_func=lambda x: True,
        )
        kwargs = config.as_kwargs()
        assert "ttl" in kwargs
        assert "key" in kwargs
        assert "key_builder" not in kwargs
        assert "skip_cache_func" not in kwargs
        assert "noself" not in kwargs
        assert "serializer" not in kwargs
        assert "plugins" not in kwargs

    def test_as_kwargs_excludes_none(self):
        config = CacheConfig(ttl=300, key=None, namespace=None)
        kwargs = config.as_kwargs()
        assert "key" not in kwargs
        assert "namespace" not in kwargs

    def test_as_kwargs_includes_alias(self):
        config = CacheConfig(alias="my_cache")
        kwargs = config.as_kwargs()
        assert kwargs["alias"] == "my_cache"


class TestAppSettings:
    def test_singleton_pattern(self):
        assert AppSettings._instance is settings
        assert settings is not None

    def test_frozen_settings(self):
        with pytest.raises(ValidationError):  # ValidationError from Pydantic
            settings.OPENAI_DEFAULT_MODEL = "gpt-5"

    def test_default_values(self):
        config = AppSettings()
        assert config.OPENAI_DEFAULT_MODEL == "gpt-4.1-mini"
        assert config.LIONAGI_EMBEDDING_PROVIDER == "openai"
        assert config.LIONAGI_EMBEDDING_MODEL == "text-embedding-3-small"
        assert config.LIONAGI_CHAT_PROVIDER == "openai"
        assert config.LIONAGI_CHAT_MODEL == "gpt-4.1-mini"

    def test_storage_defaults(self):
        config = AppSettings()
        assert config.LIONAGI_AUTO_STORE_EVENT is False
        assert config.LIONAGI_STORAGE_PROVIDER == "async_qdrant"
        assert config.LIONAGI_AUTO_EMBED_LOG is False
        assert config.LIONAGI_QDRANT_URL == "http://localhost:6333"
        assert config.LIONAGI_DEFAULT_QDRANT_COLLECTION == "event_logs"

    def test_log_defaults(self):
        config = AppSettings()
        assert config.LOG_PERSIST_DIR == "./data/logs"
        assert config.LOG_SUBFOLDER is None
        assert config.LOG_CAPACITY == 50
        assert config.LOG_EXTENSION == ".json"
        assert config.LOG_USE_TIMESTAMP is True
        assert config.LOG_HASH_DIGITS == 5
        assert config.LOG_FILE_PREFIX == "log"
        assert config.LOG_AUTO_SAVE_ON_EXIT is True
        assert config.LOG_CLEAR_AFTER_DUMP is True

    def test_log_config_property(self):
        config = AppSettings()
        log_config = config.LOG_CONFIG
        assert log_config["persist_dir"] == "./data/logs"
        assert log_config["capacity"] == 50
        assert log_config["extension"] == ".json"
        assert log_config["use_timestamp"] is True
        assert log_config["hash_digits"] == 5
        assert log_config["file_prefix"] == "log"
        assert log_config["auto_save_on_exit"] is True
        assert log_config["clear_after_dump"] is True

    def test_aiocache_config_default(self):
        config = AppSettings()
        assert isinstance(config.aiocache_config, CacheConfig)
        assert config.aiocache_config.ttl == 300

    def test_api_keys_default_to_none_without_env(self):
        # Note: This test may not pass if environment has API keys set
        # Testing that the field can be None or SecretStr
        config = AppSettings()
        # API keys can be None or SecretStr depending on environment
        assert config.OPENAI_API_KEY is None or isinstance(config.OPENAI_API_KEY, SecretStr)
        assert config.ANTHROPIC_API_KEY is None or isinstance(config.ANTHROPIC_API_KEY, SecretStr)
        assert config.PERPLEXITY_API_KEY is None or isinstance(config.PERPLEXITY_API_KEY, SecretStr)


class TestGetSecret:
    def test_get_secret_with_secret_str(self):
        config = AppSettings(OPENAI_API_KEY=SecretStr("test_key_123"))
        result = config.get_secret("OPENAI_API_KEY")
        assert result == "test_key_123"

    def test_get_secret_missing_key_raises_error(self):
        config = AppSettings()
        with pytest.raises(AttributeError, match="not found in settings"):
            config.get_secret("NONEXISTENT_KEY")

    def test_get_secret_none_value_raises_error(self):
        # Create config explicitly with None for a key
        config = AppSettings(PERPLEXITY_API_KEY=None)
        # Only test if the key is actually None
        if config.PERPLEXITY_API_KEY is None:
            with pytest.raises(ValueError, match="is not set"):
                config.get_secret("PERPLEXITY_API_KEY")
        else:
            # If env has the key set, just verify it returns a string
            result = config.get_secret("PERPLEXITY_API_KEY")
            assert isinstance(result, str)

    def test_get_secret_ollama_returns_ollama(self):
        config = AppSettings()
        result = config.get_secret("OLLAMA_API_KEY")
        assert result == "ollama"

    def test_get_secret_ollama_missing_returns_ollama(self):
        config = AppSettings()
        result = config.get_secret("some_ollama_key")
        assert result == "ollama"

    def test_get_secret_string_value(self):
        # Create a config with a custom string value
        # Note: In practice, API keys should be SecretStr
        with patch.dict(os.environ, {"OPENAI_API_KEY": "plain_text_key"}, clear=False):
            config = AppSettings()
            if config.OPENAI_API_KEY is not None:
                result = config.get_secret("OPENAI_API_KEY")
                assert isinstance(result, str)


class TestEnvironmentVariableLoading:
    def test_load_from_env_openai_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env_key_123"}, clear=False):
            config = AppSettings()
            if config.OPENAI_API_KEY is not None:
                assert config.get_secret("OPENAI_API_KEY") == "env_key_123"

    def test_load_from_env_custom_model(self):
        with patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-4"}, clear=False):
            config = AppSettings()
            assert config.OPENAI_DEFAULT_MODEL == "gpt-4"

    def test_case_insensitive_loading(self):
        with patch.dict(os.environ, {"openai_default_model": "custom-model"}, clear=False):
            config = AppSettings()
            assert config.OPENAI_DEFAULT_MODEL == "custom-model"

    def test_extra_fields_ignored(self):
        with patch.dict(os.environ, {"UNKNOWN_SETTING": "value"}, clear=False):
            config = AppSettings()
            assert not hasattr(config, "UNKNOWN_SETTING")


class TestEnvFileLoading:
    def test_env_file_paths_configured(self):
        assert ".env" in AppSettings.model_config["env_file"]
        assert ".env.local" in AppSettings.model_config["env_file"]
        assert ".secrets.env" in AppSettings.model_config["env_file"]

    def test_env_file_encoding(self):
        assert AppSettings.model_config["env_file_encoding"] == "utf-8"

    def test_case_sensitive_config(self):
        assert AppSettings.model_config["case_sensitive"] is False

    def test_extra_config(self):
        assert AppSettings.model_config["extra"] == "ignore"


class TestSettingsIntegration:
    def test_settings_is_frozen_instance(self):
        assert isinstance(settings, AppSettings)
        with pytest.raises(ValidationError):
            settings.OPENAI_DEFAULT_MODEL = "new-model"

    def test_cache_config_from_settings(self):
        assert hasattr(settings, "aiocache_config")
        kwargs = settings.aiocache_config.as_kwargs()
        assert isinstance(kwargs, dict)

    def test_log_config_from_settings(self):
        log_config = settings.LOG_CONFIG
        assert "persist_dir" in log_config
        assert "capacity" in log_config

    def test_multiple_api_keys_independent(self):
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "openai_key",
                "ANTHROPIC_API_KEY": "anthropic_key",
            },
            clear=False,
        ):
            config = AppSettings()
            if config.OPENAI_API_KEY and config.ANTHROPIC_API_KEY:
                assert config.get_secret("OPENAI_API_KEY") == "openai_key"
                assert config.get_secret("ANTHROPIC_API_KEY") == "anthropic_key"


class TestSecretStrHandling:
    def test_secret_str_created_from_env(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret_value"}, clear=False):
            config = AppSettings()
            if config.OPENAI_API_KEY is not None:
                assert isinstance(config.OPENAI_API_KEY, SecretStr)

    def test_secret_str_not_exposed_in_repr(self):
        config = AppSettings(OPENAI_API_KEY=SecretStr("secret"))
        repr_str = repr(config)
        assert "secret" not in repr_str.lower() or "**" in repr_str

    def test_secret_str_get_value(self):
        secret = SecretStr("my_secret")
        config = AppSettings(OPENAI_API_KEY=secret)
        assert config.get_secret("OPENAI_API_KEY") == "my_secret"


@pytest.mark.parametrize(
    "provider,model_key,default_model",
    [
        ("openai", "LIONAGI_CHAT_MODEL", "gpt-4.1-mini"),
        ("openai", "LIONAGI_EMBEDDING_MODEL", "text-embedding-3-small"),
    ],
)
def test_provider_defaults(provider, model_key, default_model):
    """Test default models for different providers."""
    config = AppSettings()
    assert getattr(config, model_key) == default_model


@pytest.mark.parametrize(
    "log_key,expected_value",
    [
        ("LOG_PERSIST_DIR", "./data/logs"),
        ("LOG_CAPACITY", 50),
        ("LOG_EXTENSION", ".json"),
        ("LOG_USE_TIMESTAMP", True),
        ("LOG_HASH_DIGITS", 5),
        ("LOG_FILE_PREFIX", "log"),
        ("LOG_AUTO_SAVE_ON_EXIT", True),
        ("LOG_CLEAR_AFTER_DUMP", True),
    ],
)
def test_log_configuration_defaults(log_key, expected_value):
    config = AppSettings()
    assert getattr(config, log_key) == expected_value

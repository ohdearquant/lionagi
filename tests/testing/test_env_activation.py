# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for env-var activation of the scripted provider."""

from __future__ import annotations

import json
import os

import pytest

from lionagi.service.connections import match_endpoint
from lionagi.testing import (
    ENV_PROVIDER,
    ENV_SCRIPT_PATH,
    SCRIPTED_PROVIDER,
    is_scripted_provider_active,
    resolve_script_path,
    scripted_env,
    subprocess_env,
)


@pytest.fixture
def clean_env(monkeypatch):
    """Strip all scripted-provider env vars for the test body."""
    for key in (ENV_PROVIDER, "LIONAGI_CHAT_MODEL", ENV_SCRIPT_PATH):
        monkeypatch.delenv(key, raising=False)


class TestEnvResolution:
    def test_is_scripted_provider_active_false_by_default(self, clean_env):
        assert is_scripted_provider_active() is False

    def test_is_scripted_provider_active_true_with_env(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_PROVIDER, SCRIPTED_PROVIDER)
        assert is_scripted_provider_active() is True

    def test_is_scripted_provider_active_case_insensitive(self, clean_env, monkeypatch):
        monkeypatch.setenv(ENV_PROVIDER, "Scripted")
        assert is_scripted_provider_active() is True

    def test_resolve_script_path_none_when_unset(self, clean_env):
        assert resolve_script_path() is None

    def test_resolve_script_path_returns_path(self, clean_env, monkeypatch, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{}")
        monkeypatch.setenv(ENV_SCRIPT_PATH, str(path))
        assert resolve_script_path() == path


class TestContextManager:
    def test_scripted_env_sets_and_restores(self, clean_env, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{}")

        assert ENV_PROVIDER not in os.environ
        with scripted_env(path):
            assert os.environ[ENV_PROVIDER] == SCRIPTED_PROVIDER
            assert os.environ[ENV_SCRIPT_PATH] == str(path)
        assert ENV_PROVIDER not in os.environ

    def test_scripted_env_restores_prior_value(self, clean_env, monkeypatch, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{}")
        monkeypatch.setenv(ENV_PROVIDER, "openai")

        with scripted_env(path):
            assert os.environ[ENV_PROVIDER] == SCRIPTED_PROVIDER
        assert os.environ[ENV_PROVIDER] == "openai"


class TestSubprocessEnv:
    def test_returns_layered_dict(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{}")
        base = {"SOMETHING": "x"}
        env = subprocess_env(path, base=base)
        assert env["SOMETHING"] == "x"
        assert env[ENV_PROVIDER] == SCRIPTED_PROVIDER
        assert env[ENV_SCRIPT_PATH] == str(path)


class TestEnvDrivenEndpointLoad:
    """End-to-end: with no script= kwarg, the endpoint pulls from env var."""

    async def test_endpoint_picks_up_script_from_env(self, clean_env, monkeypatch, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "responses": [{"type": "text", "content": "from-env"}],
                }
            )
        )
        monkeypatch.setenv(ENV_SCRIPT_PATH, str(path))

        ep = match_endpoint("scripted", "chat")
        out = await ep._call(payload={"model": "m", "messages": []}, headers={})
        assert out["choices"][0]["message"]["content"] == "from-env"

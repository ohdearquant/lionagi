# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A loaded profile's silent/false opt-in is a cached value, not a cache miss."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import lionagi.cli._providers as provider_module
import lionhub.orchestration.engines.engine as engine_module


@pytest.mark.parametrize("value", (None, False), ids=("silent-profile", "explicit-opt-out"))
def test_role_injection_reuses_loaded_falsey_profile_values(monkeypatch, tmp_path, value):
    calls = []

    def load_profile(role):
        calls.append(role)
        # A second read would observe an edit; a cached existing profile keeps
        # its first resolved value under today's cache contract.
        return SimpleNamespace(khive_injection=value if len(calls) == 1 else True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine_module, "_ROLE_INJECTION_CACHE", {})
    monkeypatch.setattr(provider_module, "load_agent_profile", load_profile)

    assert engine_module.role_profile_injection("case-role") is value
    assert engine_module.role_profile_injection("case-role") is value
    assert calls == ["case-role"]


@pytest.mark.parametrize("error_type", (FileNotFoundError, ValueError), ids=("missing", "invalid"))
def test_role_injection_retries_failed_load_then_caches_opt_out(monkeypatch, tmp_path, error_type):
    calls = []

    def load_profile(role):
        calls.append(role)
        if len(calls) == 1:
            raise error_type("test-local profile failure")
        return SimpleNamespace(khive_injection=False)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(engine_module, "_ROLE_INJECTION_CACHE", {})
    monkeypatch.setattr(provider_module, "load_agent_profile", load_profile)

    assert engine_module.role_profile_injection("case-role") is None
    assert engine_module.role_profile_injection("case-role") is False
    assert engine_module.role_profile_injection("case-role") is False
    assert calls == ["case-role", "case-role"]

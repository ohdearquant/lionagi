# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The codex endpoint class carries the unset-effort policy the core reaches by name."""

import inspect

from lionagi.service.connections._provider_compat import default_provider_catalog
from lionhub.providers.model_spec import (
    _CODEX_UNSET_EFFORT_METHOD,
    codex_unset_effort_default,
)
from lionhub.providers.openai.codex import CodexCLIEndpoint


def test_the_codex_endpoint_class_offers_the_policy_under_the_core_s_name():
    raw = inspect.getattr_static(CodexCLIEndpoint, _CODEX_UNSET_EFFORT_METHOD)
    assert isinstance(raw, classmethod)
    parameters = inspect.signature(getattr(CodexCLIEndpoint, _CODEX_UNSET_EFFORT_METHOD)).parameters
    assert [(p.name, p.default) for p in parameters.values()] == [
        ("model", inspect.Parameter.empty),
        ("profile_overrides", None),
        ("env", None),
    ]


def test_the_shipped_catalog_selects_that_class_for_codex():
    declaration = default_provider_catalog().endpoint("codex")
    assert declaration is not None
    assert declaration.factory() is CodexCLIEndpoint


def test_the_policy_reads_the_config_the_child_s_env_names(tmp_path):
    (tmp_path / "config.toml").write_text('model_reasoning_effort = "ultra"\n')
    env = {"CODEX_HOME": str(tmp_path)}

    assert CodexCLIEndpoint.unset_reasoning_effort("gpt-5.3-codex-spark", None, env) == "xhigh"
    assert codex_unset_effort_default("gpt-5.3-codex-spark", None, env) == "xhigh"
    assert CodexCLIEndpoint.unset_reasoning_effort("gpt-5.6-sol", None, env) == "ultra"

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the plugin-agent-profile consumer wiring in lionhub.orchestration.spawn.

An active (trusted + enabled + version-compatible) plugin's declared agent
profiles join the search only after a project/global miss, namespaced as
``<plugin>/<name>``. A bare name resolves only when unambiguous. A user's own
local file always wins over a same-named plugin profile.
"""

from __future__ import annotations

import pytest

from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.trust import trust_plugin
from lionhub.orchestration.spawn import build_agent_profile_catalog, list_agents, load_agent_profile
from tests._spawn_plugins_test_support import MANIFEST as MANIFEST
from tests._spawn_plugins_test_support import _trust as _trust
from tests._spawn_plugins_test_support import _write_plugin_profile as _write_plugin_profile


def test_resolve_worker_spec_resolves_plugin_namespaced_role(write_plugin):
    """A `<plugin>/<name>` role assigned by the orchestrator's planner must resolve to the
    plugin's declared profile, not silently fall back to a bare model spec.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    _write_plugin_profile(
        write_plugin,
        "wr",
        "web-research",
        "researcher",
        "---\nmodel: codex/gpt-5.5\n---\n\nresearch body\n",
    )

    model, profile = resolve_worker_spec("web-research/researcher")

    assert model == "codex/gpt-5.5"
    assert profile is not None
    assert profile.name == "web-research/researcher"


def test_resolve_worker_spec_falls_back_to_raw_model_spec_for_unknown_slash_token(write_plugin):
    """A literal `provider/model` token (no matching plugin profile) still resolves as a raw
    model spec, unaffected by the plugin-namespaced-role fix.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    model, profile = resolve_worker_spec("openai/gpt-4.1")

    assert model == "openai/gpt-4.1"
    assert profile is None


def test_resolve_worker_spec_falls_back_to_raw_model_spec_for_nonexistent_bare_token(write_plugin):
    """A `<namespace>/<name>` token with no dots (a valid profile-name shape) that matches no
    real plugin or local profile misses via `FileNotFoundError`, not `ValueError` — a distinct
    fallback branch from the dotted-model-version case below and must be exercised separately.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    model, profile = resolve_worker_spec("unknown/role")

    assert model == "unknown/role"
    assert profile is None


def test_resolve_worker_spec_falls_back_to_raw_model_spec_for_codex_token(write_plugin):
    """`codex/gpt-5.5` (a dotted model version, matching no plugin profile) is a real-world
    raw-spec token used by the orchestrator's own worker defaults and must resolve as such.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    model, profile = resolve_worker_spec("codex/gpt-5.5")

    assert model == "codex/gpt-5.5"
    assert profile is None


def test_resolve_worker_spec_falls_back_for_dotted_model_version(write_plugin):
    """A dotted model version (invalid as a bare profile-name component) must still fall back
    to a raw model spec instead of raising.
    """
    from lionagi.cli.orchestrate._orchestration import resolve_worker_spec

    _write_plugin_profile(
        write_plugin,
        "wr",
        "web-research",
        "researcher",
        "---\nmodel: codex/gpt-5.5\n---\n\nresearch body\n",
    )

    model, profile = resolve_worker_spec("anthropic/claude-3.7")

    assert model == "anthropic/claude-3.7"
    assert profile is None

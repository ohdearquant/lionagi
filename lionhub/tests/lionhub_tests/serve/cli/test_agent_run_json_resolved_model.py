# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""run.json records the exact agy model name an agy spec resolved to, beside the spec.

agy takes a display name such as "Gemini 4 Neon (High)", and the spec a run is launched
with is mapped onto one. A spec that the mapping sends to another family, or passes
through untouched, would otherwise be visible only in the provider's own stream.
"""

from __future__ import annotations

import json

import pytest

from .test_agent_context_from import _wire_agent_stubs
from .test_agent_run_json_error import _run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spec,effort,resolved",
    [
        ("gemini-cli/gemini-4-neon", None, "Gemini 4 Neon (High)"),
        ("gemini-cli/gemini-4-neon", "xhigh", "Gemini 4 Neon (XHigh)"),
        ("gemini-code/gemini-4-hydrogen-low", None, "Gemini 4 Hydrogen (Low)"),
        ("gemini-cli/gemini-4-xenon", None, "gemini-4-xenon"),
        # The provider prefix is matched case-insensitively, as the launch itself matches it.
        ("Gemini-CLI/gemini-4-neon", "xhigh", "Gemini 4 Neon (XHigh)"),
    ],
)
async def test_an_agy_run_records_the_model_its_spec_resolved_to(
    monkeypatch, tmp_path, spec, effort, resolved
):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)

    from lionhub.serve.cli.agent import _run_agent

    await _run_agent(spec, "go", effort=effort)

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    # `model` keeps the spec's provenance form, which drops a tier suffix
    # ("gemini-code/gemini-4-hydrogen" for "...-hydrogen-low"); the tier agy was asked
    # for is in `resolved_model`.
    assert manifest["model"].split("/", 1)[0] == spec.split("/", 1)[0]
    assert manifest["resolved_model"] == resolved


@pytest.mark.asyncio
async def test_a_run_on_another_provider_records_no_resolved_model(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)

    from lionhub.serve.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert "resolved_model" not in manifest

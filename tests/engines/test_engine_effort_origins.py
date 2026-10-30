# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Engine effort defaults retain their origin through agent construction."""

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.engines import engine as engine_mod
from lionhub.orchestration.engines import hypothesis as hypothesis_mod
from lionhub.orchestration.engines.hypothesis import FindingPosted, HypothesisEngine


@pytest.fixture(autouse=True)
def no_local_profiles(monkeypatch):
    monkeypatch.setattr(engine_mod, "role_profile_route", lambda _role: (None, None))
    monkeypatch.setattr(hypothesis_mod, "role_profile_route", lambda _role: (None, None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")


async def _extract_agent(engine):
    """Execute the actual extraction stage while stopping at its provider boundary."""
    run = engine.new_run()
    captured = []

    async def capture_agent(agent, *_args, **_kwargs):
        captured.append(agent)

    run.operate_with_repair = capture_agent
    await engine._extract(run, FindingPosted(description="a finding", eid="F-1"))
    assert len(captured) == 1
    return captured[0]


@pytest.mark.asyncio
async def test_scripted_stage_default_is_not_sent_as_explicit_effort():
    engine = HypothesisEngine(
        model="scripted/scripted-test", judge_model=None, khive_injection=False
    )
    branch = await _extract_agent(engine)
    config = branch.chat_model.endpoint.config.kwargs
    assert "effort" not in config
    assert "reasoning_effort" not in config
    assert "output_config" not in config


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["stage", "engine", "direct"])
async def test_scripted_explicit_effort_is_refused(source):
    kwargs = {"model": "scripted/scripted-test", "judge_model": None, "khive_injection": False}
    if source == "stage":
        kwargs["efforts"] = {"extract": "high"}
    elif source == "engine":
        kwargs["effort"] = "high"
    engine = HypothesisEngine(**kwargs)
    with pytest.raises(ConfigurationError, match="scripted.*high"):
        if source == "direct":
            await engine.new_run().make_agent("implementer", effort="high")
        else:
            await _extract_agent(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field", "expected"),
    [
        ("anthropic/claude-haiku-5-5", "output_config", {"effort": "high"}),
        ("openai/gpt-6-luna", "reasoning_effort", "high"),
    ],
)
async def test_supported_api_receives_hypothesis_stage_default(model, field, expected):
    engine = HypothesisEngine(model=model, judge_model=None, khive_injection=False)
    branch = await _extract_agent(engine)
    payload, _ = branch.chat_model.endpoint.create_payload(
        {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert payload[field] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "field", "expected"),
    [
        ("scripted/scripted-test", None, None),
        ("anthropic/claude-haiku-5-5", "output_config", {"effort": "low"}),
        ("openai/gpt-6-luna", "reasoning_effort", "low"),
    ],
)
async def test_make_agent_role_profile_fallback_is_not_explicit(
    monkeypatch, model, field, expected
):
    monkeypatch.setattr(engine_mod, "role_profile_route", lambda _role: (None, "low"))
    engine = HypothesisEngine(model=model, judge_model=None, khive_injection=False)
    branch = await engine.new_run().make_agent("implementer")
    config = branch.chat_model.endpoint.config.kwargs
    if field is None:
        assert "effort" not in config
        assert "reasoning_effort" not in config
        assert "output_config" not in config
    else:
        assert config[field] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stage", "direct"])
async def test_profile_model_unsupported_suffix_is_dropped_before_parsing(monkeypatch, route):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    profile_route = lambda _role: ("gemini/example-model-high", None)  # noqa: E731
    monkeypatch.setattr(engine_mod, "role_profile_route", profile_route)
    monkeypatch.setattr(hypothesis_mod, "role_profile_route", profile_route)
    engine = HypothesisEngine(judge_model=None, khive_injection=False)
    branch = (
        await _extract_agent(engine)
        if route == "stage"
        else await engine.new_run().make_agent("implementer")
    )
    endpoint = branch.chat_model.endpoint
    payload, _ = endpoint.create_payload({"messages": [{"role": "user", "content": "hi"}]})
    assert payload["model"] == "example-model"
    assert "reasoning_effort" not in payload
    assert "effort" not in endpoint.config.kwargs
    assert "reasoning_effort" not in endpoint.config.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["stage_model", "engine_model", "direct_model"])
async def test_explicit_model_unsupported_suffix_is_rejected(route):
    kwargs = {"judge_model": None, "khive_injection": False}
    if route == "stage_model":
        kwargs["models"] = {"extract": "gemini/example-model-high"}
    elif route == "engine_model":
        kwargs["model"] = "gemini/example-model-high"
    engine = HypothesisEngine(**kwargs)
    with pytest.raises(ConfigurationError, match="gemini.*example-model.*high"):
        if route == "direct_model":
            await engine.new_run().make_agent("implementer", model="gemini/example-model-high")
        else:
            await _extract_agent(engine)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_effort", "override", "expected"),
    [
        (None, "high", "profile"),
        (None, "low", "explicit"),
        ("low", "low", "profile"),
        ("low", "high", "explicit"),
    ],
)
async def test_overridden_effort_for_value_is_classified_through_actual_stage(
    monkeypatch, profile_effort, override, expected
):
    class OverrideEngine(HypothesisEngine):
        def effort_for(self, _stage):
            return override

    profile_route = lambda _role: (None, profile_effort)  # noqa: E731
    monkeypatch.setattr(engine_mod, "role_profile_route", profile_route)
    monkeypatch.setattr(hypothesis_mod, "role_profile_route", profile_route)
    engine = OverrideEngine(model="scripted/scripted-test", judge_model=None, khive_injection=False)
    if expected == "explicit":
        with pytest.raises(ConfigurationError, match=f"scripted.*{override}"):
            await _extract_agent(engine)
    else:
        branch = await _extract_agent(engine)
        config = branch.chat_model.endpoint.config.kwargs
        assert "effort" not in config
        assert "reasoning_effort" not in config
        assert "output_config" not in config

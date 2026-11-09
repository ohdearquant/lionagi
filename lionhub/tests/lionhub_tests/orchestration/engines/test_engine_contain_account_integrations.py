# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine(contain_account_integrations=True): every codex agent the engine
spawns runs without the signed-in account's apps and plugins."""

import pytest

from lionhub.orchestration.engines import Engine

pytestmark = pytest.mark.asyncio


def _features_switched(branch) -> list[tuple[str, str]]:
    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hi"})
    args = payload["request"].as_cmd_args()
    return [
        (args[i], args[i + 1])
        for i in range(len(args) - 1)
        if args[i] in ("--enable", "--disable") and args[i + 1] in ("apps", "plugins")
    ]


async def test_engine_wide_containment_reaches_every_codex_agent():
    run = Engine(contain_account_integrations=True).new_run()
    for name in ("a", "b"):
        branch = await run.make_agent("researcher", name=name, model="codex/gpt-5.5")
        assert _features_switched(branch) == [("--disable", "apps"), ("--disable", "plugins")]


async def test_an_engine_without_it_leaves_codex_agents_as_the_account_sets_them():
    """Control: the default engine changes nothing."""
    run = Engine().new_run()
    branch = await run.make_agent("researcher", name="r1", model="codex/gpt-5.5")
    assert _features_switched(branch) == []


async def test_a_contained_engine_still_builds_agents_of_other_providers():
    """An engine whose agents span providers applies it where it exists."""
    run = Engine(contain_account_integrations=True).new_run()
    branch = await run.make_agent("researcher", name="r1", model="claude_code/haiku")
    assert _features_switched(branch) == []


async def test_a_contained_engine_agent_refuses_a_call_naming_a_profile():
    """run_dag does not read profile; the request refuses it when it builds the command line."""
    run = Engine(contain_account_integrations=True).new_run()
    branch = await run.make_agent("researcher", name="r1", model="codex/gpt-5.5")
    payload, _ = branch.chat_model.endpoint.create_payload({"prompt": "hi", "profile": "work"})
    with pytest.raises(ValueError, match="profile='work'"):
        payload["request"].as_cmd_args()

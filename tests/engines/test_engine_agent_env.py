# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine(agent_env=...) and make_agent(env=...): the child environment overlay.

A per-agent value cannot ride ``os.environ`` when one process runs several
agents at once, since the environment is process-global. These assert the
resolution order and that the value reaches the endpoint the child is spawned
from.
"""

import os

import pytest

from lionagi.engines import Engine
from lionagi.providers._cli_subprocess import reveal_redacted

pytestmark = pytest.mark.asyncio

_CLI_MODEL = "codex/gpt-5.5"


def _child_env(branch):
    return reveal_redacted(branch.chat_model.endpoint._runtime_state["env"])


async def test_engine_wide_agent_env_reaches_the_child():
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert _child_env(branch)["LEG_ID"] == "leg-7"


async def test_per_call_env_outranks_the_engine_default():
    """Same order as cwd, extra_prompt and mcp_config_path: explicit call first."""
    run = Engine(agent_env={"LEG_ID": "engine-wide"}).new_run()
    branch = await run.make_agent(
        "researcher", name="r1", model=_CLI_MODEL, env={"LEG_ID": "per-call"}
    )
    assert _child_env(branch)["LEG_ID"] == "per-call"


async def test_two_agents_in_one_run_get_different_environments():
    run = Engine().new_run()
    a = await run.make_agent("researcher", name="a", model=_CLI_MODEL, env={"LEG_ID": "a"})
    b = await run.make_agent("researcher", name="b", model=_CLI_MODEL, env={"LEG_ID": "b"})
    assert _child_env(a)["LEG_ID"] == "a"
    assert _child_env(b)["LEG_ID"] == "b"


async def test_no_agent_env_leaves_the_child_inheriting():
    """Control: without the overlay nothing is placed."""
    run = Engine().new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert "env" not in branch.chat_model.endpoint._runtime_state


async def test_an_engine_agent_env_that_is_not_a_mapping_raises():
    from lionagi._errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="mapping"):
        Engine(agent_env=[("LEG_ID", "leg-7")])


async def test_engine_agent_env_is_copied_not_aliased():
    """A caller's mapping changed after construction does not change later agents."""
    supplied = {"LEG_ID": "leg-7"}
    run = Engine(agent_env=supplied).new_run()
    supplied["LEG_ID"] = "changed-after-construction"
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert _child_env(branch)["LEG_ID"] == "leg-7"


async def test_the_overlay_keeps_the_inherited_environment():
    run = Engine(agent_env={"LEG_ID": "leg-7"}).new_run()
    branch = await run.make_agent("researcher", name="r1", model=_CLI_MODEL)
    assert _child_env(branch)["PATH"] == os.environ["PATH"]

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""AgentConfig composing role + modes into the system message."""

from __future__ import annotations

import pytest

from lionagi.agent.config import AgentConfig
from lionagi.casts.pattern import Mode, Role


def test_build_system_message_passthrough():
    # No role/modes → system_prompt unchanged (backward compatible)
    cfg = AgentConfig(system_prompt="hello")
    assert cfg.build_system_message() == "hello"


def test_build_system_message_empty():
    assert AgentConfig().build_system_message() == ""


def test_build_system_message_role_by_name():
    cfg = AgentConfig(role="implementer")
    msg = cfg.build_system_message()
    role = Role.load("implementer")
    assert role.body in msg
    assert msg == role.body  # only the role, no modes/preamble


def test_build_system_message_role_and_modes():
    cfg = AgentConfig(
        role="implementer",
        modes=["systematic", "evidential"],
        system_prompt="Extra house rule.",
    )
    msg = cfg.build_system_message()
    assert Role.load("implementer").body in msg
    assert Mode.load("systematic").behaviors in msg
    assert Mode.load("evidential").behaviors in msg
    assert "Extra house rule." in msg
    # order: role, then modes, then preamble
    assert msg.index(Role.load("implementer").body) < msg.index(Mode.load("systematic").behaviors)
    assert msg.rindex("Extra house rule.") == len(msg) - len("Extra house rule.")


def test_build_system_message_accepts_objects():
    cfg = AgentConfig(role=Role.load("critic"), modes=[Mode.load("slow")])
    msg = cfg.build_system_message()
    assert Role.load("critic").body in msg
    assert Mode.load("slow").behaviors in msg


def test_unknown_role_raises():
    cfg = AgentConfig(role="no-such-role")
    with pytest.raises(ValueError, match="Unknown role"):
        cfg.build_system_message()


async def test_create_agent_uses_composed_prompt():
    from lionagi.agent.factory import create_agent

    cfg = AgentConfig(role="researcher", modes=["evidential"])
    branch = await create_agent(cfg, load_settings=False)
    system_text = branch.msgs.system.rendered
    assert Role.load("researcher").body in system_text
    assert Mode.load("evidential").behaviors in system_text

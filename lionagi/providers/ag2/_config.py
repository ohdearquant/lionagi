# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class AG2Configs(ProviderConfig, Enum):
    GROUP_CHAT = (
        "group_chat",
        ["groupchat", "chat"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.ag2.groupchat:AG2GroupChatRequest"),
    )

    AGENT = (
        "agent",
        ["beta", "ask"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.ag2.agent:AG2AgentRequest"),
    )

    NLIP = (
        "nlip",
        ["nlip_remote", "remote"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.ag2.nlip:AG2NlipRequest"),
    )


AG2Configs._PROVIDER = "ag2"
AG2Configs._PROVIDER_ALIASES = ["autogen"]

__all__ = ("AG2Configs",)

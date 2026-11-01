# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln._lazy_init import lazy_import

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "AG2AgentRequest": ("agent", None),
    "AG2BetaEndpoint": ("agent", None),
    "AG2GroupChatEndpoint": ("groupchat", None),
    "AG2GroupChatRequest": ("groupchat", None),
    "AG2NlipEndpoint": ("nlip", None),
    "AG2NlipRequest": ("nlip", None),
    "AgentConfig": ("agent", None),
    "AgentSpec": ("groupchat", None),
    "GroupChatSpec": ("groupchat", None),
    "HandoffCondition": ("groupchat", None),
    "ResearchPlan": ("groupchat", None),
    "_assert_nlip_url_safe": ("nlip", None),
    "build_group_chat": ("groupchat", None),
    "call_nlip_remote": ("nlip", None),
    "run_beta_agent": ("agent", None),
    "stream_group_chat": ("groupchat", None),
}


def __getattr__(name: str):
    return lazy_import(name, _LAZY_MAP, __name__, globals())


def __dir__():
    return sorted(_LAZY_MAP)


__all__ = tuple(sorted(_LAZY_MAP))

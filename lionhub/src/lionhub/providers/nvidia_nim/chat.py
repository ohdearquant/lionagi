# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.service.connections.endpoint import Endpoint

from ._config import NvidiaNimConfigs

__all__ = ("NvidiaNimChatEndpoint",)

CONTEXT_WINDOWS: dict[str, int] = {
    "nemotron-ultra": 131_072,
    "nemotron-super": 131_072,
    "llama-4-scout": 10_485_760,
    "llama-4-maverick": 1_048_576,
    "llama-3": 128_000,
}


@NvidiaNimConfigs.CHAT.register
class NvidiaNimChatEndpoint(Endpoint):
    """NVIDIA NIM chat completion endpoint."""

    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "meta/llama3-8b-instruct"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

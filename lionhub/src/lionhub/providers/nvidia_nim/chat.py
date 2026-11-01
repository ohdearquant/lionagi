# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.service.connections.endpoint import Endpoint

# CONTEXT_WINDOWS is defined in ._config and re-exported here.
from ._config import CONTEXT_WINDOWS, NvidiaNimConfigs  # noqa: F401

__all__ = ("NvidiaNimChatEndpoint",)


@NvidiaNimConfigs.CHAT.register
class NvidiaNimChatEndpoint(Endpoint):
    """NVIDIA NIM chat completion endpoint."""

    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "meta/llama3-8b-instruct"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

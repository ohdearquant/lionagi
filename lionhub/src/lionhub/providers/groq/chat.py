# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.service.connections.endpoint import Endpoint

# CONTEXT_WINDOWS is defined in ._config and re-exported here.
from ._config import CONTEXT_WINDOWS, GroqConfigs  # noqa: F401

__all__ = ("GroqChatEndpoint",)


@GroqConfigs.CHAT.register
class GroqChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "llama-3.3-70b-versatile"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

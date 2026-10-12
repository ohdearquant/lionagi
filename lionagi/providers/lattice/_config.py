# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType

# Where `lattice serve` listens unless started with other --host/--port values.
LATTICE_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"


class LatticeConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.lattice.chat:LatticeChatCompletionsRequest"),
        LATTICE_DEFAULT_BASE_URL,
        "none",
    )
    EMBED = (
        "embeddings",
        ["embed"],
        EndpointType.API,
        LazyType("lionagi.providers.lattice.embed:LatticeEmbedRequest"),
        LATTICE_DEFAULT_BASE_URL,
        "none",
    )


LatticeConfigs._PROVIDER = "lattice"
LatticeConfigs._PROVIDER_ALIASES = []


def _setup_lattice_endpoint(kwargs: dict) -> None:
    """Shared init for Lattice endpoints. The server listens on loopback by default, which
    the endpoint's address guard refuses unless local addresses are allowed."""
    kwargs.setdefault("allow_local_network", True)


__all__ = ("LATTICE_DEFAULT_BASE_URL", "LatticeConfigs", "_setup_lattice_endpoint")

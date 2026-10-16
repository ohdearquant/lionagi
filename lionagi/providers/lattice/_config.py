# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType

# The address `lattice serve` binds when it is started without --host or --port: the CLI
# defaults to host 127.0.0.1 and port 8080.
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


def _setup_lattice_endpoint(config, kwargs: dict) -> None:
    """Shared init for Lattice endpoints: never carry a credential, and default the address
    guard and payload serialization to what a keyless, loopback, OpenAI-shaped server needs.

    The server listens on loopback, which the guard refuses unless local addresses are allowed,
    so each endpoint opts in. A value the caller already stated -- in a config dict or on an
    ``EndpointConfig`` -- is left as it is; only an unstated one takes the Lattice default. The
    defaults go through ``kwargs``, never on top of a config that already names the field, so
    rebuilding an endpoint from its own serialized config does not pass the field twice.
    """
    if isinstance(config, dict):
        stated = frozenset(config)
    elif config is not None:
        stated = frozenset(getattr(config, "model_fields_set", ()) or ())
    else:
        stated = frozenset()

    if "allow_local_network" not in stated:
        kwargs.setdefault("allow_local_network", True)
    if "serialize_by_alias" not in stated:
        kwargs.setdefault("serialize_by_alias", True)
    kwargs.pop("api_key", None)


__all__ = ("LATTICE_DEFAULT_BASE_URL", "LatticeConfigs", "_setup_lattice_endpoint")

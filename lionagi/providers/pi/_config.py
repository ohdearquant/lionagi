# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class PiConfigs(ProviderConfig, Enum):
    CLI = (
        "query_cli",
        ["cli", "code"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.pi.cli:PiCodeRequest"),
    )


PiConfigs._PROVIDER = "pi"
PiConfigs._PROVIDER_ALIASES = ["pi-code", "pi_code"]

__all__ = ("PiConfigs",)

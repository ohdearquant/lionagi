# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum

from lionagi.service.connections.endpoint_config import EndpointConfig
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
        LazyType("lionhub.providers.lattice.chat:LatticeChatCompletionsRequest"),
        LATTICE_DEFAULT_BASE_URL,
        "none",
    )
    EMBED = (
        "embeddings",
        ["embed"],
        EndpointType.API,
        LazyType("lionhub.providers.lattice.embed:LatticeEmbedRequest"),
        LATTICE_DEFAULT_BASE_URL,
        "none",
    )


LatticeConfigs._PROVIDER = "lattice"
LatticeConfigs._PROVIDER_ALIASES = []


class LatticeRedirectsRefused:
    """Mixin: a Lattice request never follows a redirect.

    The address guard checks one URL, before the first request, so a redirect the client
    followed could reach a host the guard never admitted. Refusing redirects keeps the host it
    admitted the only host a request can reach.
    """

    async def _call_http(self, payload, headers, **kwargs):
        kwargs["follow_redirects"] = False
        return await super()._call_http(payload, headers, **kwargs)

    async def _stream_http(self, payload, headers, **kwargs):
        kwargs["follow_redirects"] = False
        async for chunk in super()._stream_http(payload, headers, **kwargs):
            yield chunk


def _serialized_or_absent(request_model) -> bool:
    """Whether a request model is one to put back rather than keep.

    A request model survives serialization as a JSON schema (or its text), and the model the
    loader rebuilds from that schema holds `const` and `enum` fields as Enum members that
    `json` cannot encode; an absent one leaves the payload unvalidated.
    """
    if request_model is None or isinstance(request_model, (dict, str)):
        return True

    from lionagi.libs.schema.load_pydantic_model_from_schema import SCHEMA_MODEL_MODULE

    return getattr(request_model, "__module__", None) == SCHEMA_MODEL_MODULE


def _setup_lattice_endpoint(config, kwargs: dict, request_model: type):
    """Shared init for Lattice endpoints: never carry a credential, default the address guard
    and payload serialization to what a keyless, loopback, OpenAI-shaped server needs, and
    restore the provider's declared request model.

    A value the caller states -- in a config dict, on an ``EndpointConfig``, or as a keyword --
    is left as it is; only an unstated one takes the Lattice default. The defaults go through
    ``kwargs``, never on top of a config that already names the field, so rebuilding an endpoint
    from its own serialized config does not pass the field twice. The request model is the one
    exception: a serialized one is replaced by ``request_model``, since neither form it is left
    in -- a rebuilt stand-in holding Enum members, or nothing at all -- can send a request.

    Returns the config to hand ``super().__init__``: a dict config is rebuilt without the keys
    whose values are replaced, so no field is passed twice. A config object carrying a key is
    copied before both key fields are cleared, leaving the caller's object unchanged.
    """
    if isinstance(config, dict):
        if _serialized_or_absent(config.get("request_options")):
            config = {k: v for k, v in config.items() if k != "request_options"}
        if "api_key" in config:
            config = {k: v for k, v in config.items() if k != "api_key"}
        stated = frozenset(config)
    elif isinstance(config, EndpointConfig):
        if config.api_key is not None or config._api_key is not None:
            config = config.model_copy(update={"api_key": None}, deep=True)
            config._api_key = None
        stated = frozenset(config.model_fields_set)
        if _serialized_or_absent(config.request_options):
            stated -= {"request_options"}
    else:
        stated = frozenset()

    if "request_options" not in stated and _serialized_or_absent(kwargs.get("request_options")):
        kwargs["request_options"] = request_model
    if "allow_local_network" not in stated:
        kwargs.setdefault("allow_local_network", True)
    if "serialize_by_alias" not in stated:
        kwargs.setdefault("serialize_by_alias", True)
    kwargs.pop("api_key", None)
    return config


__all__ = (
    "LATTICE_DEFAULT_BASE_URL",
    "LatticeConfigs",
    "LatticeRedirectsRefused",
    "_setup_lattice_endpoint",
)

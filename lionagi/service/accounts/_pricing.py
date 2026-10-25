# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Dated Anthropic price revisions, request ceilings and usage settlement (ADR-0126 D5).

Rates are exact fractions of a nanodollar per token. A request is priced only when every
part of it is something the revision prices; anything else refuses before it is sent.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Any

from ._types import (
    INT64_MAX,
    AccountRoutingError,
    CompleteUsage,
    Quote,
    RouterErrorCode,
    UnknownOutcome,
)

__all__ = (
    "CACHE_WRITE_LIFETIMES",
    "CURRENT_REVISION",
    "ModelPrice",
    "PriceRevision",
    "PriceTier",
    "cache_write_split",
    "freeze_request",
    "price_usage",
    "quote_ceiling",
    "request_ttls",
    "revision_by_id",
    "usage_outcome",
)

_NANODOLLARS_PER_TOKEN_PER_USD_PER_MTOK = 1000


def _per_mtok(usd: str) -> Fraction:
    return Fraction(Decimal(usd)) * _NANODOLLARS_PER_TOKEN_PER_USD_PER_MTOK


@dataclass(frozen=True, slots=True)
class PriceTier:
    """Rates for prompts of at most ``max_prompt_tokens`` (None: no upper bound)."""

    max_prompt_tokens: int | None
    input: Fraction
    write_5m: Fraction
    write_1h: Fraction
    read: Fraction
    output: Fraction


@dataclass(frozen=True, slots=True)
class ModelPrice:
    max_input_tokens: int
    max_output_tokens: int
    # Ascending by max_prompt_tokens; the last tier is unbounded.
    tiers: tuple[PriceTier, ...]

    def tier_for(self, prompt_tokens: int) -> PriceTier:
        for tier in self.tiers:
            if tier.max_prompt_tokens is None or prompt_tokens <= tier.max_prompt_tokens:
                return tier
        raise AssertionError("the last tier is unbounded")


@dataclass(frozen=True, slots=True)
class PriceRevision:
    id: str
    read_on: str
    sources: tuple[str, ...]
    models: Mapping[str, ModelPrice]
    # Multiplier per reported inference region. A request that names no region runs in the
    # workspace default, so ceilings use the largest multiplier here.
    geo_multipliers: Mapping[str | None, Fraction]


def _flat(
    input_: str, write_5m: str, write_1h: str, read: str, output: str
) -> tuple[PriceTier, ...]:
    return (
        PriceTier(
            None,
            _per_mtok(input_),
            _per_mtok(write_5m),
            _per_mtok(write_1h),
            _per_mtok(read),
            _per_mtok(output),
        ),
    )


_ONE_MILLION = 1_000_000
_MAX_OUTPUT = 128_000

CURRENT_REVISION = PriceRevision(
    id="anthropic-20261012",
    read_on="2026-10-12",
    sources=(
        "https://platform.claude.com/docs/en/about-claude/pricing",
        "https://platform.claude.com/docs/en/about-claude/models/overview",
        "https://platform.claude.com/docs/en/api/messages/create",
    ),
    models=MappingProxyType(
        {
            "claude-fable-5-1": ModelPrice(
                _ONE_MILLION, _MAX_OUTPUT, _flat("10", "12.50", "20", "0.25", "50")
            ),
            "claude-opus-5-5": ModelPrice(
                _ONE_MILLION, _MAX_OUTPUT, _flat("4", "5", "8", "0.20", "20")
            ),
            "claude-sonnet-5-5": ModelPrice(
                _ONE_MILLION, _MAX_OUTPUT, _flat("2", "2.50", "4", "0.10", "10")
            ),
            # Priced by prompt length, counting fresh, cache-read and cache-written input.
            "claude-haiku-5-5": ModelPrice(
                _ONE_MILLION,
                _MAX_OUTPUT,
                (
                    PriceTier(
                        100_000,
                        _per_mtok("0.10"),
                        _per_mtok("0.125"),
                        _per_mtok("0.20"),
                        _per_mtok("0.01"),
                        _per_mtok("0.50"),
                    ),
                    PriceTier(
                        None,
                        _per_mtok("0.50"),
                        _per_mtok("0.625"),
                        _per_mtok("1"),
                        _per_mtok("0.05"),
                        _per_mtok("2.50"),
                    ),
                ),
            ),
        }
    ),
    geo_multipliers=MappingProxyType(
        {None: Fraction(1), "global": Fraction(1), "us": Fraction(11, 10)}
    ),
)

_REVISIONS: Mapping[str, PriceRevision] = MappingProxyType({CURRENT_REVISION.id: CURRENT_REVISION})


def revision_by_id(revision_id: str) -> PriceRevision:
    try:
        return _REVISIONS[revision_id]
    except (KeyError, TypeError):
        raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING) from None


# ---------------------------------------------------------------------------
# The accepted request shape. Every key outside these sets refuses: an unknown key
# can be a feature with its own tariff.

_TOP_LEVEL = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "system",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "stream",
        "metadata",
        "tools",
        "tool_choice",
        "cache_control",
        "thinking",
        "output_config",
        "service_tier",
    }
)
_CACHE_CONTROL_KEYS = frozenset({"type", "ttl"})
_TTLS = frozenset({"5m", "1h"})
_BLOCK_KEYS = {
    "text": frozenset({"type", "text", "cache_control"}),
    "image": frozenset({"type", "source", "cache_control"}),
    "tool_use": frozenset({"type", "id", "name", "input", "cache_control"}),
    "tool_result": frozenset({"type", "tool_use_id", "content", "is_error", "cache_control"}),
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
}
_TOOL_RESULT_BLOCK_TYPES = frozenset({"text", "image"})
_IMAGE_SOURCE_KEYS = frozenset({"type", "media_type", "data"})
_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_TOOL_KEYS = frozenset({"type", "name", "description", "input_schema", "cache_control", "strict"})
_TOOL_CHOICE_KEYS = frozenset({"type", "name", "disable_parallel_tool_use"})
_TOOL_CHOICE_TYPES = frozenset({"auto", "any", "tool", "none"})
_THINKING_TYPES = frozenset({"adaptive", "disabled"})


def _refuse() -> AccountRoutingError:
    return AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _cache_ttl(control: Any, ttls: set[str]) -> None:
    if control is None:
        return
    if not isinstance(control, Mapping) or not set(control) <= _CACHE_CONTROL_KEYS:
        raise _refuse()
    if control.get("type") != "ephemeral":
        raise _refuse()
    ttl = control.get("ttl", "5m")
    if ttl not in _TTLS:
        raise _refuse()
    ttls.add(ttl)


def _block(block: Any, ttls: set[str], *, allowed: frozenset[str] | None = None) -> None:
    if isinstance(block, str):
        return
    if not isinstance(block, Mapping):
        raise _refuse()
    kind = block.get("type")
    keys = _BLOCK_KEYS.get(kind) if isinstance(kind, str) else None
    if keys is None or (allowed is not None and kind not in allowed) or not set(block) <= keys:
        raise _refuse()
    _cache_ttl(block.get("cache_control"), ttls)
    if kind == "image":
        source = block.get("source")
        if (
            not isinstance(source, Mapping)
            or not set(source) <= _IMAGE_SOURCE_KEYS
            or source.get("type") != "base64"
            or source.get("media_type") not in _IMAGE_MEDIA_TYPES
        ):
            raise _refuse()
    elif kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            for item in content:
                _block(item, ttls, allowed=_TOOL_RESULT_BLOCK_TYPES)
        elif content is not None and not isinstance(content, str):
            raise _refuse()


def _content(content: Any, ttls: set[str]) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise _refuse()
    for block in content:
        _block(block, ttls)


def request_ttls(payload: Mapping[str, Any]) -> frozenset[str]:
    """Validate a final request against the accepted shape; return its cache TTLs.

    Refuses ``unsupported_pricing`` for anything the revision cannot price: server tools,
    documents and file references, priority or fast modes, unknown keys at any level.
    """
    if not isinstance(payload, Mapping) or not set(payload) <= _TOP_LEVEL:
        raise _refuse()
    # A request that omits the tier may be served on priority capacity, which is not priced.
    if payload.get("service_tier") != "standard_only":
        raise _refuse()
    ttls: set[str] = set()
    _cache_ttl(payload.get("cache_control"), ttls)

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _refuse()
    for message in messages:
        if not isinstance(message, Mapping) or not set(message) <= {"role", "content"}:
            raise _refuse()
        if message.get("role") not in ("user", "assistant"):
            raise _refuse()
        _content(message.get("content"), ttls)

    system = payload.get("system")
    if system is not None:
        if isinstance(system, list):
            for block in system:
                _block(block, ttls, allowed=frozenset({"text"}))
        elif not isinstance(system, str):
            raise _refuse()

    tools = payload.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise _refuse()
        for tool in tools:
            # A typed tool other than "custom" is a server or built-in tool with its own charges.
            if (
                not isinstance(tool, Mapping)
                or not set(tool) <= _TOOL_KEYS
                or tool.get("type", "custom") != "custom"
                or not isinstance(tool.get("name"), str)
            ):
                raise _refuse()
            _cache_ttl(tool.get("cache_control"), ttls)

    choice = payload.get("tool_choice")
    if choice is not None and (
        not isinstance(choice, Mapping)
        or not set(choice) <= _TOOL_CHOICE_KEYS
        or choice.get("type") not in _TOOL_CHOICE_TYPES
    ):
        raise _refuse()

    thinking = payload.get("thinking")
    if thinking is not None and (
        not isinstance(thinking, Mapping)
        or set(thinking) != {"type"}
        or thinking["type"] not in _THINKING_TYPES
    ):
        raise _refuse()

    output_config = payload.get("output_config")
    if output_config is not None and (
        not isinstance(output_config, Mapping)
        or not set(output_config) <= {"effort"}
        or not isinstance(output_config.get("effort", ""), str)
    ):
        raise _refuse()

    metadata = payload.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, Mapping) or not set(metadata) <= {"user_id"}
    ):
        raise _refuse()
    return frozenset(ttls)


def freeze_request(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes to send: quote these, and send nothing else."""
    try:
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise _refuse() from None


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        # Two parsers can keep different copies of a repeated key; quote nothing ambiguous.
        raise ValueError("duplicate key")
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite number")


def _parse(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(
            body, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except (TypeError, ValueError, UnicodeDecodeError):
        raise _refuse() from None
    if not isinstance(payload, dict):
        raise _refuse()
    return payload


def quote_ceiling(revision: PriceRevision, body: bytes) -> Quote:
    """Price the most the request in ``body`` can cost: C = ceil(Imax*Pin + Omax*Pout).

    ``Pin`` is the largest input rate the request can incur in any tier (fresh, cache read,
    and the cache writes for the TTLs it asks for), ``Pout`` the largest output rate, both at
    the largest regional multiplier.
    """
    payload = _parse(body)
    ttls = request_ttls(payload)
    model = payload.get("model")
    price = revision.models.get(model) if isinstance(model, str) else None
    if price is None:
        raise _refuse()
    max_tokens = payload.get("max_tokens")
    if not _is_count(max_tokens) or not 1 <= max_tokens <= price.max_output_tokens:
        raise _refuse()

    geo = max(revision.geo_multipliers.values())
    p_in = Fraction(0)
    p_out = Fraction(0)
    for tier in price.tiers:
        rates = [tier.input, tier.read]
        if "5m" in ttls:
            rates.append(tier.write_5m)
        if "1h" in ttls:
            rates.append(tier.write_1h)
        p_in = max(p_in, *rates)
        p_out = max(p_out, tier.output)
    ceiling = math.ceil((price.max_input_tokens * p_in + max_tokens * p_out) * geo)
    if not 1 <= ceiling <= INT64_MAX:
        raise _refuse()
    return Quote(
        requested_model=model,
        ceiling_nusd=ceiling,
        price_revision=revision.id,
        request_digest=hashlib.sha256(body).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Settlement

_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_creation",
        "inference_geo",
        "service_tier",
        "server_tool_use",
        # The speed the request was served at; only standard speed is priced here.
        "speed",
        # A breakdown of output_tokens, which already bills it; never added again.
        "output_tokens_details",
    }
)
# The two cache-write lifetimes a provider report may split by, in the order
# the split is returned as ``(five_minute, one_hour)``.
CACHE_WRITE_LIFETIMES = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
_SPLIT_KEYS = frozenset(CACHE_WRITE_LIFETIMES)


def cache_write_split(usage: Mapping[str, Any]) -> tuple[int, int] | None:
    """Read one provider report's cache-write lifetime split.

    Returns ``(five_minute, one_hour)`` as ints, or None when the report
    states a split that cannot be certified. Keys must be a subset of
    ``CACHE_WRITE_LIFETIMES``, each present value must be a non-negative int,
    an absent lifetime reads 0, and the two must sum exactly to the report's
    ``cache_creation_input_tokens``. A report with no ``cache_creation`` at all
    claims no split and returns ``(0, 0)``. See the cache-write lifetime rule
    in docs/internals/core.md (``session/``) for the consumer-facing meaning.
    """
    total = usage.get("cache_creation_input_tokens")
    split = usage.get("cache_creation")
    if split is None:
        return 0, 0
    if not isinstance(split, Mapping) or not set(split) <= _SPLIT_KEYS:
        return None
    if not _is_count(total):
        return None
    five_minute = split.get(CACHE_WRITE_LIFETIMES[0], 0)
    one_hour = split.get(CACHE_WRITE_LIFETIMES[1], 0)
    if not (_is_count(five_minute) and _is_count(one_hour)):
        return None
    if five_minute + one_hour != total:
        return None
    return five_minute, one_hour


def _cache_writes(usage: Mapping[str, Any], ttls: frozenset[str]) -> tuple[int, int] | None:
    total = usage.get("cache_creation_input_tokens")
    split = usage.get("cache_creation")
    if total is None:
        # Without a cache marker nothing can be written, so an omitted count is zero.
        if ttls or split is not None:
            return None
        return 0, 0
    if not _is_count(total):
        return None
    if split is not None:
        return cache_write_split(usage)
    if total == 0:
        return 0, 0
    # No split: derivable only when the request asked for exactly one TTL.
    if ttls == {"5m"}:
        return total, 0
    if ttls == {"1h"}:
        return 0, total
    return None


def price_usage(
    revision: PriceRevision,
    ttls: frozenset[str],
    served_model: Any,
    usage: Any,
    request_id: str | None = None,
) -> CompleteUsage | None:
    """Price complete final usage at ``revision`` for the model that served it.

    Returns None, leaving the cost unknown and the ceiling held, for an unpriced model, a
    missing or inconsistent count, a tier or region the revision does not price, or any
    usage field it does not know. ``usage_outcome`` says which.
    """
    outcome = usage_outcome(revision, ttls, served_model, usage, request_id)
    return outcome if isinstance(outcome, CompleteUsage) else None


# A reason code is at most 64 characters (the ledger's grammar), so a usage field it names
# is at most 46: 64 less the prefix and its underscore.
_UNKNOWN_KEY = "unknown_usage_key"
_NAMEABLE_KEY = re.compile(rf"[a-z0-9_]{{1,{63 - len(_UNKNOWN_KEY)}}}\Z")


def usage_outcome(
    revision: PriceRevision,
    ttls: frozenset[str],
    served_model: Any,
    usage: Any,
    request_id: str | None = None,
) -> CompleteUsage | UnknownOutcome:
    """Price usage as ``price_usage`` does, or name the first thing that left it unpriced.

    The reason is ``unpriced_model``, ``unknown_usage_key_<field>`` (the alphabetically
    first unknown field the reason can carry, else ``unknown_usage_key``),
    ``unpriced_tier``, ``unpriced_speed``, ``unpriced_region`` or
    ``unpriced_server_tool``. A report that cannot be read (a missing or inconsistent
    count, a malformed field, a cost past the ledger's range) is ``usage_incomplete``.
    """
    incomplete = UnknownOutcome("usage_incomplete")
    if not isinstance(served_model, str) or not isinstance(usage, Mapping):
        return incomplete
    price = revision.models.get(served_model)
    if price is None:
        return UnknownOutcome("unpriced_model")
    unknown = set(usage) - _USAGE_KEYS
    if unknown:
        named = sorted(k for k in unknown if isinstance(k, str) and _NAMEABLE_KEY.match(k))
        return UnknownOutcome(f"{_UNKNOWN_KEY}_{named[0]}" if named else _UNKNOWN_KEY)
    fresh = usage.get("input_tokens")
    output = usage.get("output_tokens")
    if not (_is_count(fresh) and _is_count(output)):
        return incomplete
    read = usage.get("cache_read_input_tokens")
    if read is None:
        if ttls:
            return incomplete
        read = 0
    elif not _is_count(read):
        return incomplete
    writes = _cache_writes(usage, ttls)
    if writes is None:
        return incomplete
    w5, w1 = writes
    if usage.get("service_tier") not in (None, "standard"):
        return UnknownOutcome("unpriced_tier")
    if usage.get("speed") not in (None, "standard"):
        return UnknownOutcome("unpriced_speed")
    geo = usage.get("inference_geo")
    if not (geo is None or isinstance(geo, str)):
        return incomplete
    if geo not in revision.geo_multipliers:
        return UnknownOutcome("unpriced_region")
    server_tools = usage.get("server_tool_use")
    if server_tools is not None:
        if not isinstance(server_tools, Mapping) or not all(
            _is_count(v) for v in server_tools.values()
        ):
            return incomplete
        if any(server_tools.values()):
            return UnknownOutcome("unpriced_server_tool")

    tier = price.tier_for(fresh + read + w5 + w1)
    cost = math.ceil(
        (
            fresh * tier.input
            + read * tier.read
            + w5 * tier.write_5m
            + w1 * tier.write_1h
            + output * tier.output
        )
        * revision.geo_multipliers[geo]
    )
    if cost > INT64_MAX:
        return incomplete
    return CompleteUsage(
        served_model=served_model,
        input_tokens=fresh,
        cache_read_input_tokens=read,
        cache_write_5m_tokens=w5,
        cache_write_1h_tokens=w1,
        output_tokens=output,
        cost_nusd=cost,
        request_id=request_id,
    )

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0047 built-in logging handlers.

The five state-store persistence handlers (``persist_session_start``, ``persist_session_end``,
``persist_branch_provenance``, ``persist_branch_end`` and ``persist_message``) live in
``lionhub.orchestration.run_persistence``. Their old names here keep working until lionagi 2.0.0:
the first access emits a ``DeprecationWarning`` at the caller's line and returns the handler from
its new module, the same object under both paths.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

logger = logging.getLogger("lionagi.hooks.builtins")

__all__ = (
    "log_api_metrics",
    "log_tool_call",
    "log_tool_use",
)

# Served from their new module on attribute access until 2.0.0, through the moved-module table
# row for this private old name; the finder emits the warning and resolves the new module.
_PERSISTENCE_HANDLER_NAMES = frozenset(
    {
        "persist_session_start",
        "persist_session_end",
        "persist_branch_provenance",
        "persist_branch_end",
        "persist_message",
    }
)
_PERSISTENCE_HANDLERS_OLD_MODULE = "lionagi.hooks._persistence_handlers"


def __getattr__(name: str) -> Any:
    if name in _PERSISTENCE_HANDLER_NAMES:
        import importlib

        return getattr(importlib.import_module(_PERSISTENCE_HANDLERS_OLD_MODULE), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def log_api_metrics(
    *,
    model: str | None = None,
    provider: str | None = None,
    tokens: dict[str, int] | None = None,
    latency_ms: float | None = None,
    **_unused: Any,
) -> None:
    """Structured log line for API call observability."""
    if tokens:
        logger.info(
            "api.post_call model=%s provider=%s input_tokens=%s output_tokens=%s latency_ms=%s",
            model,
            provider,
            tokens.get("input_tokens"),
            tokens.get("output_tokens"),
            latency_ms,
        )
    else:
        logger.info(
            "api.post_call model=%s provider=%s latency_ms=%s",
            model,
            provider,
            latency_ms,
        )


async def log_tool_call(
    *,
    tool_name: str,
    action: str | None = None,
    args: dict[str, Any] | None = None,
    **_unused: Any,
) -> None:
    """Structured log for tool dispatch — readable but not metric-y."""
    logger.info(
        "tool.pre tool=%s action=%s args=%s",
        tool_name,
        action,
        list(args.keys()) if args else [],
    )


async def log_tool_use(
    *,
    tool_name: str,
    action: str | None = None,
    args: dict[str, Any] | None = None,
    **_unused: Any,
) -> None:
    """Deprecated: use log_tool_call instead."""
    warnings.warn(
        "log_tool_use is deprecated and will be removed in a future minor release. "
        "Use log_tool_call instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    await log_tool_call(tool_name=tool_name, action=action, args=args)

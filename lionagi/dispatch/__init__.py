# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable dispatch outbox (ADR-0092 slice 1): producer-driven at-least-once delivery."""

from __future__ import annotations

from .outbox import (
    DEFAULT_MAX_ATTEMPTS,
    ack_dispatch,
    backoff_seconds,
    deliver_due_dispatches,
    enqueue_dispatch,
    get_dispatch,
    list_dispatches,
    purge_dispatch,
    purge_dispatches,
    resolve_notify_template,
    retry_dispatch,
)

__all__ = (
    "DEFAULT_MAX_ATTEMPTS",
    "ack_dispatch",
    "backoff_seconds",
    "deliver_due_dispatches",
    "enqueue_dispatch",
    "get_dispatch",
    "list_dispatches",
    "purge_dispatch",
    "purge_dispatches",
    "resolve_notify_template",
    "retry_dispatch",
)

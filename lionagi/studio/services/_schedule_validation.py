# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared validation for schedule declarations and service requests."""

from __future__ import annotations

import math
from typing import Any

from lionagi._flow_spec import normalize_flow_spec_keys, validate_flow_spec_fields

__all__ = (
    "_GITHUB_FILTER_ALLOWED_EVENTS",
    "_GITHUB_FILTER_ALLOWED_KEYS",
    "_svc_validate_budget_tokens",
    "_svc_validate_budget_usd",
    "_svc_validate_command_args",
    "_svc_validate_github_filter",
    "_svc_validate_github_repo",
    "_validate_flow_yaml_spec",
)


def _svc_validate_command_args(args: list | None) -> None:
    """Service-boundary check: action_command_args must be a list. Elements are
    ``{{var}}`` templates rendered against trigger_context at fire time, not here."""
    if args is None:
        return
    if not isinstance(args, list):
        raise ValueError(f"action_command_args must be a list of strings, got {args!r}")


def _svc_validate_budget_usd(budget_usd: Any) -> None:
    """Service-boundary check: reject a non-positive budget_usd. None (unlimited) is always accepted."""
    if budget_usd is None:
        return
    if (
        isinstance(budget_usd, bool)
        or not isinstance(budget_usd, int | float)
        or not math.isfinite(budget_usd)
        or budget_usd <= 0
    ):
        raise ValueError(f"budget_usd must be a finite positive number, got {budget_usd!r}")


def _svc_validate_budget_tokens(budget_tokens: Any) -> None:
    """Service-boundary check: reject a non-positive budget_tokens. None (unlimited) is always accepted."""
    if budget_tokens is None:
        return
    if isinstance(budget_tokens, bool) or not isinstance(budget_tokens, int) or budget_tokens <= 0:
        raise ValueError(f"budget_tokens must be a positive integer, got {budget_tokens!r}")


def _svc_validate_github_repo(repo: str | None) -> None:
    """Service-boundary check: reject github_repo values that would manipulate
    the API path (CWE-918). None is a no-op; an empty string is rejected."""
    if repo is None:
        return
    from lionagi.studio.scheduler.github import _validate_github_repo

    _validate_github_repo(repo)


# github_filter's known keys. Only "pr_merged" has real dispatch semantics in
# github_poll() today; "pr_opened"/"pr_updated"/"pr_closed" are accepted (the
# frontend ships all four) but currently inert server-side.
_GITHUB_FILTER_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"state", "base", "draft", "event", "same_repo_only", "merged_page_limit"}
)
_GITHUB_FILTER_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {"pr_merged", "pr_opened", "pr_updated", "pr_closed"}
)


def _svc_validate_github_filter(github_filter: Any) -> None:
    """Service-boundary check: reject unknown github_filter keys/values — a typo'd
    key would otherwise match everything and fire on every poll instead of
    failing loudly at create/update time."""
    if github_filter is None:
        return
    if not isinstance(github_filter, dict):
        raise ValueError(f"github_filter must be an object, got {type(github_filter).__name__!r}")
    unknown = set(github_filter) - _GITHUB_FILTER_ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"github_filter has unknown key(s) {sorted(unknown)}; allowed keys are "
            f"{sorted(_GITHUB_FILTER_ALLOWED_KEYS)}"
        )
    if "merged_page_limit" in github_filter:
        from lionagi.studio.scheduler.github import merged_page_limit

        merged_page_limit(github_filter["merged_page_limit"])
    event = github_filter.get("event")
    if event is not None and event not in _GITHUB_FILTER_ALLOWED_EVENTS:
        raise ValueError(
            f"github_filter.event {event!r} is not a supported value; allowed values "
            f"are {sorted(_GITHUB_FILTER_ALLOWED_EVENTS)} (or omit the key)"
        )
    if "same_repo_only" in github_filter:
        same_repo_only = github_filter["same_repo_only"]
        if not isinstance(same_repo_only, bool):
            raise ValueError(
                "github_filter.same_repo_only must be a boolean, got "
                f"{type(same_repo_only).__name__!r}"
            )


def _validate_flow_yaml_spec(yaml_text: str) -> str | None:
    """Parse and validate an inline YAML flow spec; returns an error message on
    failure or None on success. Mirrors _validate_spec_fields() in the CLI —
    that's the authoritative source for field rules."""
    import yaml  # lazy — not needed on every import of this module

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return f"flow_yaml spec is not valid YAML: {exc}"

    if not isinstance(data, dict):
        return f"flow_yaml spec must be a YAML mapping (dict), got {type(data).__name__}"

    for key in data:
        if not isinstance(key, str):
            return f"flow_yaml spec keys must be strings, got {type(key).__name__}"

    return validate_flow_spec_fields(normalize_flow_spec_keys(data))

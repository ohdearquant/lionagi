# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared validation for schedule declarations and service requests."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from lionagi._flow_spec import normalize_flow_spec_keys, validate_flow_spec_fields


def validate_rate_limit(value: Any) -> tuple[int, int] | None:
    """Validate and normalize a schedule rolling-window fire cap.

    ``None`` means unlimited. A configured cap must contain exactly the two
    positive integer fields ``max_fires`` and ``window_sec``. Returning the
    normalized pair keeps service-boundary validation and engine admission on
    one definition of the persisted JSON shape.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("rate_limit must be an object") from exc
    if not isinstance(value, Mapping):
        raise ValueError("rate_limit must be an object")
    expected = {"max_fires", "window_sec"}
    if set(value) != expected:
        raise ValueError("rate_limit must contain exactly max_fires and window_sec")
    max_fires = value["max_fires"]
    window_sec = value["window_sec"]
    if isinstance(max_fires, bool) or not isinstance(max_fires, int) or max_fires <= 0:
        raise ValueError("rate_limit.max_fires must be a positive integer")
    if isinstance(window_sec, bool) or not isinstance(window_sec, int) or window_sec <= 0:
        raise ValueError("rate_limit.window_sec must be a positive integer")
    return max_fires, window_sec


_MERGED_MODE_MAX_PAGES = 5


def merged_page_limit(value: object) -> int:
    """Resolve a bounded page limit for a merged pull-request scan."""
    if value is None:
        return _MERGED_MODE_MAX_PAGES
    if type(value) is not int or not 5 <= value <= 100:
        raise ValueError("github_filter.merged_page_limit must be an integer from 5 through 100")
    return value


_GITHUB_OWNER_MAX = 39
_GITHUB_REPO_MAX = 100
_GITHUB_OWNER_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")
_GITHUB_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GITHUB_REPO_TRAVERSAL = frozenset({".", ".."})


def _validate_github_repo(repo: str) -> None:
    """Raise ValueError if *repo* is not a safe ``owner/name`` pair (CWE-918).

    Defense-in-depth at URL-construction time; the service write boundary
    applies the same check via ``services/schedules._svc_validate_github_repo``.
    """
    if not repo or "/" not in repo:
        raise ValueError(
            f"github_repo {repo!r} is not a valid owner/name identifier. "
            "Expected format: 'owner/repo' with exactly one '/' separator."
        )
    parts = repo.split("/")
    if len(parts) != 2:
        raise ValueError(
            f"github_repo {repo!r} must contain exactly one '/' (got {len(parts) - 1})."
        )
    owner, name = parts

    # --- Owner validation ---
    if not owner:
        raise ValueError(f"github_repo {repo!r}: owner segment is empty.")
    if len(owner) > _GITHUB_OWNER_MAX:
        raise ValueError(
            f"github_repo {repo!r}: owner segment is {len(owner)} chars (max {_GITHUB_OWNER_MAX})."
        )
    if not _GITHUB_OWNER_RE.match(owner):
        raise ValueError(
            f"github_repo {repo!r}: owner {owner!r} is not a valid GitHub owner "
            "identifier (alphanumeric start/end, alphanumeric or '-' interior, "
            "no leading/trailing hyphen)."
        )

    # --- Repo name validation ---
    if not name:
        raise ValueError(f"github_repo {repo!r}: repo name segment is empty.")
    if len(name) > _GITHUB_REPO_MAX:
        raise ValueError(
            f"github_repo {repo!r}: repo name segment is {len(name)} chars "
            f"(max {_GITHUB_REPO_MAX})."
        )
    if not _GITHUB_REPO_NAME_RE.match(name):
        raise ValueError(
            f"github_repo {repo!r}: repo name {name!r} contains characters not "
            "allowed in a GitHub repository name (use letters, digits, '-', '_', '.')."
        )
    if name in _GITHUB_REPO_TRAVERSAL:
        raise ValueError(
            f"github_repo {repo!r}: repo name {name!r} is a path-traversal "
            "singleton and is not a valid repository name."
        )


__all__ = (
    "_GITHUB_OWNER_MAX",
    "_GITHUB_OWNER_RE",
    "_GITHUB_REPO_MAX",
    "_GITHUB_REPO_NAME_RE",
    "_GITHUB_REPO_TRAVERSAL",
    "_GITHUB_FILTER_ALLOWED_EVENTS",
    "_GITHUB_FILTER_ALLOWED_KEYS",
    "_MERGED_MODE_MAX_PAGES",
    "_validate_github_repo",
    "_svc_validate_budget_tokens",
    "_svc_validate_budget_usd",
    "_svc_validate_command_args",
    "_svc_validate_github_filter",
    "_svc_validate_github_repo",
    "_validate_flow_yaml_spec",
    "merged_page_limit",
    "validate_rate_limit",
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

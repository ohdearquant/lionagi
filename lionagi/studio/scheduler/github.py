# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 GitHub polling for event-triggered schedules."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from lionagi.state.db import StateDB

_log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# CWE-918 defense-in-depth: github_repo must be exactly "owner/name" -- one
# slash, no path traversal sequences, no URL-special chars.
#
# Owner and repo segments have DIFFERENT rules (empirically verified against
# the GitHub API -- e.g. https://api.github.com/repos/github/.github returns
# 200, so a repo CAN start with '.'):
#
#   Owner: alphanumeric start, alphanumeric or '-' interior, alphanumeric end
#          (GitHub's user/org naming rule), max 39 chars.
#          Single-char owners (e.g. "a") are valid -- inner group is optional.
#
#   Repo:  letters/digits/'-'/'_'/'.' allowed, may start with '.' (e.g.
#          .github), but must NOT be the traversal singletons '.' or '..'.
#          Max 100 chars.
#
# These are the single sources of truth; services/schedules.py imports and
# delegates to _validate_github_repo rather than duplicating them.
_GITHUB_OWNER_MAX = 39
_GITHUB_REPO_MAX = 100

# Owner: alphanumeric start/end, alphanumeric or hyphen interior.
_GITHUB_OWNER_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")
# Repo name: letters/digits/'-'/'_'/'.' only (leading '.' is valid).
_GITHUB_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Traversal singletons that are structurally valid but semantically forbidden.
_GITHUB_REPO_TRAVERSAL = frozenset({".", ".."})


def _validate_github_repo(repo: str) -> None:
    """Raise ValueError if *repo* is not a safe GitHub owner/name pair (CWE-918).

    Defense-in-depth at URL-construction time; service write boundary applies the
    same check via services/schedules._svc_validate_github_repo.  Rules: exactly
    one '/' separator, valid owner/repo segments per the regex constants above.
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


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def _get_gh_token() -> str | None:
    """Get GitHub token from gh CLI or environment."""
    import os

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            return stdout.decode().strip()
    except Exception:
        _log.debug("gh CLI not available for token retrieval")
    return None


async def github_poll(schedule: dict) -> list[dict[str, Any]]:
    """Poll GitHub for new/updated PRs. Returns list of new PR dicts."""
    repo = schedule.get("github_repo")
    if not repo:
        return []

    # Defense-in-depth: validate format before interpolating into the API URL.
    # The service write boundary applies the same check via _svc_validate_github_repo
    # in services/schedules.py, but we re-check here so that any schedule dict
    # that reaches this function (regardless of origin) cannot retarget the path.
    try:
        _validate_github_repo(repo)
    except ValueError:
        _log.error(
            "github_poll: schedule %r (%r) has invalid github_repo %r -- "
            "must be 'owner/name'; skipping poll",
            schedule.get("id"),
            schedule.get("name"),
            repo,
        )
        return []

    token = await _get_gh_token()
    if not token:
        _log.warning("No GitHub token available for polling %s", repo)
        return []

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Use stored ETag for conditional requests
    node_meta = schedule.get("node_metadata") or {}
    etag = node_meta.get("github_etag") if isinstance(node_meta, dict) else None
    if etag:
        headers["If-None-Match"] = etag

    github_filter = schedule.get("github_filter") or {}
    params: dict[str, str] = {
        "state": github_filter.get("state", "open"),
        "sort": "updated",
        "direction": "desc",
        "per_page": "20",
    }
    if "base" in github_filter:
        params["base"] = github_filter["base"]

    client = _get_client()
    try:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/pulls",
            headers=headers,
            params=params,
        )
    except httpx.HTTPError:
        _log.exception("GitHub API request failed for %s", repo)
        return []

    if resp.status_code == 304:
        return []

    if resp.status_code != 200:
        _log.warning("GitHub API returned %d for %s", resp.status_code, repo)
        return []

    remaining = int(resp.headers.get("x-ratelimit-remaining", "60"))
    if remaining < 10:
        _log.warning("GitHub rate limit low: %d remaining for %s", remaining, repo)

    new_etag = resp.headers.get("etag")
    cursor = schedule.get("github_cursor")
    prs = resp.json()

    new_prs = []
    max_updated = cursor
    for pr in prs:
        updated = pr.get("updated_at", "")
        if not cursor or updated > cursor:
            new_prs.append(
                {
                    "pr_number": pr.get("number"),
                    "pr_title": pr.get("title"),
                    "pr_url": pr.get("html_url"),
                    "pr_author": (pr.get("user") or {}).get("login"),
                    "updated_at": updated,
                }
            )
            if not max_updated or updated > max_updated:
                max_updated = updated

    # Update cursor on the schedule when new PRs found or etag refreshed
    update_fields: dict[str, Any] = {}
    if max_updated and max_updated != cursor:
        update_fields["github_cursor"] = max_updated
    if new_etag and not update_fields:
        # No new PRs but etag changed -- update cursor to avoid redundant refetches
        # We don't have a dedicated etag column; skip persisting etag alone
        pass
    if update_fields:
        async with StateDB() as db:
            await db.update_schedule(schedule["id"], **update_fields)

    return new_prs

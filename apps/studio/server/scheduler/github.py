# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 GitHub polling for event-triggered schedules."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from lionagi.state.db import StateDB

_log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


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
            "gh", "auth", "token",
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

    # Check rate limit
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
            new_prs.append({
                "pr_number": pr.get("number"),
                "pr_title": pr.get("title"),
                "pr_url": pr.get("html_url"),
                "pr_author": (pr.get("user") or {}).get("login"),
                "updated_at": updated,
            })
            if not max_updated or updated > max_updated:
                max_updated = updated

    # Update cursor on the schedule when new PRs found or etag refreshed
    update_fields: dict[str, Any] = {}
    if max_updated and max_updated != cursor:
        update_fields["github_cursor"] = max_updated
    if new_etag and not update_fields:
        # No new PRs but etag changed — update cursor to avoid redundant refetches
        # We don't have a dedicated etag column; skip persisting etag alone
        pass
    if update_fields:
        async with StateDB() as db:
            await db.update_schedule(schedule["id"], **update_fields)

    return new_prs

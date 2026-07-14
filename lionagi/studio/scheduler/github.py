# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0070 GitHub polling for event-triggered schedules."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import httpx

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GithubPollItem:
    """One PR observed by a poll, past the previously stored cursor.

    ``event`` is always populated, even when ``dispatchable`` is False, so a
    caller can log which PR was seen without firing it. ``updated_at`` is the
    cursor high-water-mark field for this item -- the PR's raw GitHub
    ``updated_at``, except under ``github_filter={"event": "pr_merged"}``
    where it holds ``merged_at`` instead (the event dict's own ``updated_at``
    key still carries the PR's real ``updated_at`` for template rendering).
    ``dispatchable`` is False when ``github_filter`` (e.g. a draft filter)
    excludes the PR from firing -- it is still returned so the caller can
    advance the cursor past it without re-listing it every poll.
    """

    event: dict[str, Any]
    updated_at: str
    dispatchable: bool


class GithubPollResult(NamedTuple):
    """Return shape of ``github_poll()``.

    ``scan_complete`` is False only when merged-mode pagination stopped for an
    UNSAFE reason (the ``_MERGED_MODE_MAX_PAGES`` cap hit, or a pagination
    fetch/status error) rather than a safe boundary (short page, no
    ``rel="next"``, or the stored cursor reached). ``items`` already has any
    event that couldn't be proven complete filtered out in that case -- a
    caller does not need to re-derive that from item counts; the flag exists
    for observability (e.g. logging that boundary events are held for a
    later poll).

    ``poll_status`` distinguishes a healthy-but-empty poll from one that
    couldn't see GitHub -- ``items == []`` alone is ambiguous between
    "nothing new" and "blind" (a 401 or network failure also returns no
    items). ``"ok"`` = any 2xx or 304; ``"auth_error"`` = a 401 that survived
    the gh-CLI-token fallback retry; ``"error"`` = anything else that
    prevented a real poll. ``SchedulerEngine._tick_github`` uses this to
    stamp the schedule's observer-self-health columns. Defaults to ``"ok"``
    so direct-construction call sites (tests, mocks) don't need updating.
    """

    items: list[GithubPollItem]
    scan_complete: bool
    poll_status: Literal["ok", "auth_error", "error"] = "ok"


_client: httpx.AsyncClient | None = None

# Last token known to have authenticated (set after responses other than
# 401/403).
# Checked before _get_gh_token() so a healthy poll skips GITHUB_TOKEN /
# `gh auth token`; a fresh 401 or 403 clears it to force re-resolution.
_cached_token: str | None = None

# Bounds how many pages github_poll() will fetch hunting for an older,
# still-undispatched merged PR (merged_at can trail the API's updated_at
# sort key -- see GithubPollResult.scan_complete). Worst case
# _MERGED_MODE_MAX_PAGES * per_page (100) PRs inspected per poll; anything
# buried deeper is picked up on a later poll once the noise ages out.
# Bounded latency, not bounded correctness.
_MERGED_MODE_MAX_PAGES = 5

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

# CWE-918 defense-in-depth: github_repo must be exactly "owner/name" (one
# slash, no traversal/URL-special chars). Owner and repo segments have
# different rules (verified against the GitHub API -- a repo may start with
# '.', e.g. github/.github). Single source of truth: services/schedules.py
# delegates to _validate_github_repo rather than duplicating these.
_GITHUB_OWNER_MAX = 39
_GITHUB_REPO_MAX = 100

# Owner: alphanumeric start/end, alphanumeric or hyphen interior.
_GITHUB_OWNER_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$")
# Repo name: letters/digits/'-'/'_'/'.' only (leading '.' is valid).
_GITHUB_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Traversal singletons that are structurally valid but semantically forbidden.
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


def _next_page_url(resp: httpx.Response) -> str | None:
    """Extract the RFC 5988 ``rel="next"`` URL from the response's ``Link``
    header, or ``None`` on the last page. Regex-parsed (not
    ``httpx.Response.links``) so a bare-dict test double works too.
    """
    link_header = resp.headers.get("link")
    if not link_header:
        return None
    m = _LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def _gh_cli_token() -> str | None:
    """Fetch a token from the gh CLI (`gh auth token`), or None if unavailable."""
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


async def _get_gh_token(prefer_cli: bool = False) -> str | None:
    """Get a GitHub token from the environment or the gh CLI.

    ``GITHUB_TOKEN`` wins by default. ``prefer_cli=True`` skips it and reads a
    fresh token from ``gh auth token`` instead -- used to recover from a
    ``GITHUB_TOKEN`` that was valid at daemon launch but has since expired.
    """
    import os

    if not prefer_cli:
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            return token
    return await _gh_cli_token()


async def github_poll(schedule: dict) -> GithubPollResult:
    """Poll GitHub for PRs newer than the stored cursor.

    Returns items ordered oldest-``updated_at``-first (the API itself returns
    newest-first) so a caller advancing the persisted cursor incrementally
    stays monotone.

    Does NOT persist ``github_cursor`` -- that is the caller's job
    (``SchedulerEngine._tick_github``). Fire-per-event budget gating means
    some dispatchable items may not actually get fired this poll
    (max_runs/global-slot exhaustion) and must be re-listed on the next poll
    rather than silently skipped, so only the caller -- who knows what it
    actually dispatched -- can decide how far the cursor is safe to advance.
    """
    repo = schedule.get("github_repo")
    if not repo:
        return GithubPollResult(items=[], scan_complete=True, poll_status="error")

    # Defense-in-depth: re-validate here (services/schedules.py checks this too)
    # so any schedule dict reaching this function, regardless of origin,
    # cannot retarget the URL.
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
        return GithubPollResult(items=[], scan_complete=True, poll_status="error")

    global _cached_token
    token = _cached_token or await _get_gh_token()
    if not token:
        _log.warning("No GitHub token available for polling %s", repo)
        return GithubPollResult(items=[], scan_complete=True, poll_status="error")

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    node_meta = schedule.get("node_metadata") or {}
    etag = node_meta.get("github_etag") if isinstance(node_meta, dict) else None
    if etag:
        headers["If-None-Match"] = etag

    github_filter = schedule.get("github_filter") or {}
    merged_mode = github_filter.get("event") == "pr_merged"
    params: dict[str, str] = {
        # pr_merged is only ever true on a closed PR, so merged mode always
        # polls closed PRs regardless of any (nonsensical) explicit state.
        "state": "closed" if merged_mode else github_filter.get("state", "open"),
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
        return GithubPollResult(items=[], scan_complete=True, poll_status="error")

    # 401 means the cached/env token has expired. Retry once with a freshly
    # fetched gh-CLI token so a stale credential doesn't pin the poller blind.
    if resp.status_code == 401:
        cli_token = await _get_gh_token(prefer_cli=True)
        if cli_token and cli_token != token:
            token = cli_token
            headers["Authorization"] = f"Bearer {cli_token}"
            try:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/pulls",
                    headers=headers,
                    params=params,
                )
            except httpx.HTTPError:
                _log.exception("GitHub API request failed for %s", repo)
                _cached_token = None
                return GithubPollResult(items=[], scan_complete=True, poll_status="error")

    if resp.status_code == 401:
        _cached_token = None
        _log.error(
            "GitHub API returned 401 (unauthorized) for %s polling schedule %s (%s) "
            "even after falling back to a gh-CLI token; the poller cannot see new "
            "events until valid credentials are available",
            repo,
            schedule.get("id"),
            schedule.get("name"),
        )
        return GithubPollResult(items=[], scan_complete=True, poll_status="auth_error")

    # A forbidden response does not prove the token is reusable: it may reflect
    # revoked permissions or an installation whose access changed. Re-resolve
    # on the next poll instead of pinning the cache to it.
    _cached_token = None if resp.status_code == 403 else token

    if resp.status_code == 304:
        return GithubPollResult(items=[], scan_complete=True, poll_status="ok")

    if resp.status_code != 200:
        _log.warning("GitHub API returned %d for %s", resp.status_code, repo)
        return GithubPollResult(items=[], scan_complete=True, poll_status="error")

    remaining = int(resp.headers.get("x-ratelimit-remaining", "60"))
    if remaining < 10:
        _log.warning("GitHub rate limit low: %d remaining for %s", remaining, repo)

    cursor = schedule.get("github_cursor")
    per_page = int(params["per_page"])
    page = resp.json()
    prs = list(page)

    # True once the scan reached a boundary that proves no unfetched page
    # could hold an event this poll needs (see GithubPollResult.scan_complete).
    scan_complete = True

    if merged_mode:
        # Page forward while the last page was full and its oldest PR (API
        # sorts updated_at desc) is still newer than the cursor -- past that
        # point every remaining PR is already-seen ground.
        pages_fetched = 1
        while True:
            is_short_page = len(page) < per_page
            oldest_updated = page[-1].get("updated_at", "") if page else ""
            cursor_reached = bool(cursor) and oldest_updated <= cursor
            next_url = _next_page_url(resp)
            if is_short_page or cursor_reached or not next_url:
                # Boundary proven safe regardless of how many pages were fetched.
                break
            if pages_fetched >= _MERGED_MODE_MAX_PAGES:
                # Full page, more to fetch, cursor not reached: unproven data
                # may remain beyond the cap.
                scan_complete = False
                break
            try:
                resp = await client.get(next_url, headers=headers)
            except httpx.HTTPError:
                _log.warning(
                    "GitHub API pagination request failed for %s while paging "
                    "for merged PRs; using %d PR(s) fetched so far -- events "
                    "too close to the unproven boundary are held for a later poll",
                    repo,
                    len(prs),
                )
                scan_complete = False
                break
            if resp.status_code != 200:
                if resp.status_code in (401, 403):
                    # Rejected mid-pagination; drop the token so the next poll re-resolves.
                    _cached_token = None
                _log.warning(
                    "GitHub API returned %d for %s during merged-PR pagination; "
                    "using %d PR(s) fetched so far -- events too close to the "
                    "unproven boundary are held for a later poll",
                    resp.status_code,
                    repo,
                    len(prs),
                )
                scan_complete = False
                break
            page = resp.json()
            prs.extend(page)
            pages_fetched += 1

    # Once the scan is truncated, any PR whose cursor field sits at or past
    # the oldest fetched updated_at can't be proven safe to dispatch -- drop
    # it entirely (not just mark non-dispatchable) so it's re-fetched and
    # reconsidered on a later poll instead of risking a skipped merge.
    unsafe_floor: str | None = None
    if merged_mode and not scan_complete and prs:
        unsafe_floor = min(pr.get("updated_at", "") for pr in prs)

    draft_filter = github_filter.get("draft")
    same_repo_filter = github_filter.get("same_repo_only")
    items: list[GithubPollItem] = []
    for pr in prs:
        updated = pr.get("updated_at", "")
        if merged_mode:
            merged_at = pr.get("merged_at")
            if not merged_at:
                # Closed but never merged -- not a "PR merged" event; drops
                # off the API's top-N-by-updated window on its own.
                continue
            cursor_at = merged_at
        else:
            cursor_at = updated

        if cursor and cursor_at <= cursor:
            continue

        if unsafe_floor is not None and cursor_at >= unsafe_floor:
            continue

        is_draft = bool(pr.get("draft", False))
        # Only a real JSON boolean narrows the fire set. A malformed non-bool
        # draft filter is ignored (fail open to no filtering) rather than
        # silently matching the wrong side — the string "false" is truthy.
        dispatchable = not (isinstance(draft_filter, bool) and is_draft != draft_filter)

        # head.repo is null for a PR whose fork source was deleted -- fail
        # closed (never same-repo) rather than fail open, since this feeds a
        # trust decision: fork diffs are attacker-controlled input.
        head_repo_obj = (pr.get("head") or {}).get("repo")
        base_repo_obj = (pr.get("base") or {}).get("repo")
        head_repo = head_repo_obj.get("full_name") if head_repo_obj else None
        head_repo_is_fork = bool(head_repo_obj.get("fork", False)) if head_repo_obj else False
        # Repository ids are stable and case-independent; the configured
        # github_repo string and the API's returned full_name may differ
        # only in case (GitHub repo paths are case-insensitive), which would
        # false-negative a plain string ``==``. Prefer comparing the PR's own
        # head/base repo ids -- both come from the same API response, so no
        # external casing assumption is needed -- falling back to a
        # casefolded full_name comparison when either id is unavailable, and
        # failing closed (never same-repo) only when head.repo is missing
        # entirely.
        head_repo_id = head_repo_obj.get("id") if head_repo_obj else None
        base_repo_id = base_repo_obj.get("id") if base_repo_obj else None
        if head_repo_id is not None and base_repo_id is not None:
            is_same_repo = head_repo_id == base_repo_id
        elif head_repo is not None:
            is_same_repo = head_repo.casefold() == repo.casefold()
        else:
            is_same_repo = False
        # Same fail-open-on-malformed-filter-value semantics as draft_filter
        # above: only a real JSON boolean narrows the fire set.
        if isinstance(same_repo_filter, bool) and same_repo_filter and not is_same_repo:
            dispatchable = False

        event = {
            "pr_number": pr.get("number"),
            "pr_title": pr.get("title"),
            "pr_url": pr.get("html_url"),
            "pr_author": (pr.get("user") or {}).get("login"),
            "updated_at": updated,
            "head_sha": (pr.get("head") or {}).get("sha"),
            "draft": is_draft,
            "head_repo": head_repo,
            "head_repo_is_fork": head_repo_is_fork,
            "is_same_repo": is_same_repo,
        }
        if merged_mode:
            event["pr_merged_at"] = merged_at
        items.append(GithubPollItem(event=event, updated_at=cursor_at, dispatchable=dispatchable))

    # API order (updated_at desc) isn't contractually identical to the
    # cursor field in merged mode; sort explicitly so cursor advance stays
    # monotone in both modes.
    items.sort(key=lambda it: it.updated_at)
    return GithubPollResult(items=items, scan_complete=scan_complete, poll_status="ok")

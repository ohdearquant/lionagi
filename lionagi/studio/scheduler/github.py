# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 GitHub polling for event-triggered schedules."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, NamedTuple

import httpx

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GithubPollItem:
    """One PR observed by a poll, past the previously stored cursor.

    ``event`` is always populated (even when ``dispatchable`` is False) so a
    caller can log which PR was seen without firing it. ``updated_at`` is the
    cursor high-water-mark field for this item -- normally the PR's raw
    GitHub ``updated_at``, but under ``github_filter={"event": "pr_merged"}``
    it holds ``merged_at`` instead, since that's what the merged-PR mode
    compares against the persisted cursor (the event dict's own
    ``updated_at`` key still carries the PR's real ``updated_at`` either way,
    for template rendering). ``dispatchable`` is False when ``github_filter``
    (e.g. a draft filter) excludes the PR from firing -- it is still
    returned, not silently dropped, so the caller can advance the cursor
    past it without the PR being re-listed on every subsequent poll.
    """

    event: dict[str, Any]
    updated_at: str
    dispatchable: bool


class GithubPollResult(NamedTuple):
    """Return shape of ``github_poll()``.

    ``scan_complete`` is False only when merged-mode pagination stopped for
    an UNSAFE reason -- the ``_MERGED_MODE_MAX_PAGES`` cap was hit, or a
    pagination fetch/status error truncated the scan -- rather than a safe
    boundary (a short page, no ``rel="next"`` link, or the stored cursor was
    reached). ``items`` has already had any event that could not be proven
    complete filtered out in that case (see the truncation-safety filter in
    ``github_poll``), so a caller does not need to re-derive that from item
    counts; ``scan_complete`` exists for observability -- e.g. logging that
    some events near the cursor boundary are being held for a later poll,
    when a deeper or safely-bounded scan may resolve them.
    """

    items: list[GithubPollItem]
    scan_complete: bool


_client: httpx.AsyncClient | None = None

# Merged-PR mode's dispatch key (merged_at) differs from the API's sort key
# (updated_at): a page full of closed-but-never-merged PRs produces no item
# at all (see the pr_merged branch in github_poll), so it can push an older,
# still-undispatched merged PR out of the first page and, without
# pagination, past the poller's reach permanently -- the cursor would never
# learn the merge happened. _MERGED_MODE_MAX_PAGES bounds how far
# github_poll() will page forward hunting for it: worst case
# _MERGED_MODE_MAX_PAGES * per_page (100) closed PRs inspected in one poll.
# A merge event buried deeper than that in a single burst is simply not
# found *this* poll -- the cursor never advances past anything unseen, so
# it is found on a later poll once the shallower unmerged noise ages out of
# GitHub's "recently updated" ordering. Bounded latency, not bounded
# correctness.
#
# That "cursor never advances past anything unseen" guarantee only holds
# when the scan reaches a SAFE boundary (a short page, no next link, or the
# stored cursor was reached): pages are sorted by updated_at desc, so
# stopping there proves every unfetched PR has an older updated_at, and
# merged_at <= updated_at, than everything already scanned. Stopping for an
# UNSAFE reason instead -- the page cap above, or a pagination fetch/status
# error -- proves nothing about what lies beyond the last fetched page.
# github_poll() tracks this as GithubPollResult.scan_complete and drops any
# item too close to that unproven boundary (see the truncation-safety
# filter below) rather than risk advancing the cursor past an event an
# unfetched page might still hold.
_MERGED_MODE_MAX_PAGES = 5

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

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


def _next_page_url(resp: httpx.Response) -> str | None:
    """Extract the RFC 5988 ``rel="next"`` URL from a GitHub API response's
    ``Link`` header, or ``None`` on the last page.

    Parsed with a plain regex rather than ``httpx.Response.links`` so a
    lightweight test double (a bare ``headers`` dict) works the same as a
    real ``httpx.Response``.
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


async def github_poll(schedule: dict) -> GithubPollResult:
    """Poll GitHub for PRs newer than the stored cursor.

    Returns items ordered oldest-``updated_at``-first (the GitHub API itself
    returns them newest-first) so a caller advancing the persisted cursor
    incrementally, one dispatched item at a time, stays monotone.

    Does NOT persist ``github_cursor`` -- that is the caller's job now
    (``SchedulerEngine._tick_github``). Fire-per-event budget gating means
    some of the dispatchable items returned here may not actually get fired
    this poll (max_runs/global-slot exhaustion), and those must be re-listed
    on the next poll rather than silently skipped, so only the caller -- who
    knows what it actually dispatched -- can decide how far the cursor is
    safe to advance. See ``GithubPollResult.scan_complete`` for the
    equivalent truncation-safety concern in merged mode.
    """
    repo = schedule.get("github_repo")
    if not repo:
        return GithubPollResult(items=[], scan_complete=True)

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
        return GithubPollResult(items=[], scan_complete=True)

    token = await _get_gh_token()
    if not token:
        _log.warning("No GitHub token available for polling %s", repo)
        return GithubPollResult(items=[], scan_complete=True)

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
        return GithubPollResult(items=[], scan_complete=True)

    if resp.status_code == 304:
        return GithubPollResult(items=[], scan_complete=True)

    if resp.status_code != 200:
        _log.warning("GitHub API returned %d for %s", resp.status_code, repo)
        return GithubPollResult(items=[], scan_complete=True)

    remaining = int(resp.headers.get("x-ratelimit-remaining", "60"))
    if remaining < 10:
        _log.warning("GitHub rate limit low: %d remaining for %s", remaining, repo)

    cursor = schedule.get("github_cursor")
    per_page = int(params["per_page"])
    page = resp.json()
    prs = list(page)

    # True once the scan has reached a boundary that PROVES no unfetched
    # page could hold an event this poll needs to worry about (see the
    # scan_complete docstring on GithubPollResult). Flipped to False below
    # only when the loop stops for a reason that does NOT prove that.
    scan_complete = True

    if merged_mode:
        # Page forward while the most recently fetched page was full (a
        # short page means there's nothing more) AND its oldest PR (last in
        # updated_at-desc order) is still newer than the cursor -- once an
        # item's updated_at has fallen to or below the cursor, every PR
        # further back is too (the API is sorted by updated_at desc), and
        # merged_at <= updated_at always holds for a merged PR, so nothing
        # beyond that point could be an undispatched merge either.
        for _ in range(_MERGED_MODE_MAX_PAGES - 1):
            if len(page) < per_page:
                break
            oldest_updated = page[-1].get("updated_at", "") if page else ""
            if cursor and oldest_updated <= cursor:
                break
            next_url = _next_page_url(resp)
            if not next_url:
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
        else:
            # The for-loop ran out of pages to fetch (_MERGED_MODE_MAX_PAGES
            # reached) without ever hitting one of the safe breaks above --
            # there may still be more pages beyond this one.
            scan_complete = False

    # In merged mode, once the scan is truncated (unsafe boundary), any
    # fetched PR whose cursor field (merged_at) sits at or past the oldest
    # updated_at we actually fetched cannot be safely dispatched: we can't
    # prove an unfetched page doesn't hold an older, still-undispatched
    # merge, and the cursor can't advance past this PR without risking that
    # merge being skipped forever. Deferring it (dropping it from this
    # poll's items entirely, not just marking it non-dispatchable) also
    # avoids a duplicate fire -- since the cursor stays behind it, it is
    # re-fetched and reconsidered on a later poll instead.
    unsafe_floor: str | None = None
    if merged_mode and not scan_complete and prs:
        unsafe_floor = min(pr.get("updated_at", "") for pr in prs)

    draft_filter = github_filter.get("draft")
    items: list[GithubPollItem] = []
    for pr in prs:
        updated = pr.get("updated_at", "")
        if merged_mode:
            merged_at = pr.get("merged_at")
            if not merged_at:
                # Closed but never merged -- not a "PR merged" event under
                # this filter. It never fires and has no merge time to
                # compare against the cursor, so it's simply not an item;
                # it naturally drops off the API's top-N-by-updated window
                # once nothing about it changes further.
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
        event = {
            "pr_number": pr.get("number"),
            "pr_title": pr.get("title"),
            "pr_url": pr.get("html_url"),
            "pr_author": (pr.get("user") or {}).get("login"),
            "updated_at": updated,
            "head_sha": (pr.get("head") or {}).get("sha"),
            "draft": is_draft,
        }
        if merged_mode:
            event["pr_merged_at"] = merged_at
        items.append(GithubPollItem(event=event, updated_at=cursor_at, dispatchable=dispatchable))

    # The API returns PRs sorted by updated_at desc, which is the cursor
    # field itself in the default mode but only a close correlate of it in
    # merged mode (merging a PR bumps updated_at, but the two aren't
    # contractually identical) -- sort explicitly by the cursor field so the
    # caller's incremental cursor advance stays monotone in both modes,
    # rather than relying on a bare reversal of API order.
    items.sort(key=lambda it: it.updated_at)
    return GithubPollResult(items=items, scan_complete=scan_complete)

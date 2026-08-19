# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0070 GitHub polling for event-triggered schedules."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import httpx

_log = logging.getLogger(__name__)

_CURSOR_SEP = "#"
_CURSOR_NUMBER_WIDTH = 10
# The number a cursor carrying no PR of its own compares as: every event in its second
# is behind it. That is what a bare timestamp meant before this, and reading it that way
# is what keeps an upgrade from re-offering a batch already dispatched.
_WHOLE_SECOND = sys.maxsize


# The one spelling of a cursor, so the writer below and the API validator that has to
# accept what it writes cannot drift apart. The number is fixed-width for the same reason
# it is zero-padded: a shorter one would order wrongly against a longer one.
CURSOR_RE = re.compile(
    r"^(?P<instant>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
    rf"(?:{re.escape(_CURSOR_SEP)}\d{{{_CURSOR_NUMBER_WIDTH}}})?$"
)

CURSOR_FORM = f"YYYY-MM-DDTHH:MM:SSZ, optionally followed by {_CURSOR_SEP} and a "
CURSOR_FORM += f"{_CURSOR_NUMBER_WIDTH}-digit zero-padded pull request number"


def _cursor_for(cursor_at: str, pr_number: Any) -> str:
    """The cursor value for one event: its timestamp, then the PR it belongs to.

    GitHub timestamps have one-second resolution and a merge queue lands batches inside
    one second, so several events routinely share a timestamp and the timestamp alone
    cannot say which of them a schedule has dispatched. The number is zero-padded because
    this value is ordered lexically everywhere it is compared, in SQL included. An event
    without a number cannot be placed within its second and keeps the older meaning.
    """
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 0:
        return cursor_at
    return f"{cursor_at}{_CURSOR_SEP}{pr_number:0{_CURSOR_NUMBER_WIDTH}d}"


def _cursor_bound(cursor: str | None) -> tuple[str, int] | None:
    """What a stored cursor claims, as a (timestamp, PR number) pair, or None if unset."""
    if not cursor:
        return None
    timestamp, sep, number = cursor.partition(_CURSOR_SEP)
    if not sep or not number.isdigit():
        return (timestamp, _WHOLE_SECOND)
    return (timestamp, int(number))


def _event_position(cursor_at: str, pr_number: Any) -> tuple[str, int]:
    """Where one event sits in the same order a stored cursor is read in."""
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 0:
        return (cursor_at, _WHOLE_SECOND)
    return (cursor_at, pr_number)


@dataclass(frozen=True, slots=True)
class GithubPollItem:
    """One PR observed by a poll, past the previously stored cursor.

    ``event`` is always populated, even when ``dispatchable`` is False, so a
    caller can log which PR was seen without firing it. ``updated_at`` is the
    cursor field for this item -- the PR's raw ``updated_at``, except under
    ``github_filter={"event": "pr_merged"}`` where it holds ``merged_at``
    instead. ``cursor`` is that field plus this PR's number, and is the value a
    caller persists: the timestamp alone cannot separate two events that share a
    second. ``dispatchable`` is False when ``github_filter`` excludes the PR from
    firing, but it's still returned so the cursor can advance past it.
    """

    event: dict[str, Any]
    updated_at: str
    dispatchable: bool
    cursor: str


class GithubPollResult(NamedTuple):
    """Return shape of ``github_poll()``.

    ``scan_complete`` is False only when merged-mode pagination stopped for
    an unsafe reason (the ``_MERGED_MODE_MAX_PAGES`` cap, or a fetch/status
    error) rather than a safe boundary; ``items`` already has any
    can't-prove-complete event filtered out, so the flag is for observability
    only. ``poll_status`` distinguishes healthy-but-empty from blind (a 401 or
    network failure also returns no items): ``"ok"`` = 2xx/304,
    ``"auth_error"`` = 401 surviving the gh-CLI-token fallback, ``"error"`` =
    anything else. Used by ``SchedulerEngine._tick_github`` to stamp the
    schedule's observer-self-health columns.
    """

    items: list[GithubPollItem]
    scan_complete: bool
    poll_status: Literal["ok", "auth_error", "error"] = "ok"


_client: httpx.AsyncClient | None = None

# Last token known to have authenticated. Checked before _get_gh_token() so a
# healthy poll skips GITHUB_TOKEN / `gh auth token`; a fresh 401/403 clears it.
_cached_token: str | None = None

# Bounds pages fetched hunting an older, still-undispatched merged PR
# (merged_at can trail the API's updated_at sort key). Bounded latency, not
# bounded correctness: a backlog deeper than this reach is picked up on a
# later poll, but does not recover on its own if it falls behind.
_MERGED_MODE_MAX_PAGES = 5

# Page size requested (GitHub's maximum); the page budget above is stated in
# terms of this.
_PER_PAGE = 100

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

# CWE-918 defense-in-depth: github_repo must be exactly "owner/name" (one
# slash, no traversal/URL-special chars). services/schedules.py delegates to
# _validate_github_repo rather than duplicating these.
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
        "per_page": str(_PER_PAGE),
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
    bound = _cursor_bound(cursor)
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
            # A cursor naming an event within its second leaves that second's other
            # events unclaimed, so paging stops only once a page is strictly older
            # than it; a whole-second cursor has nothing left to find there.
            if bound is None:
                cursor_reached = False
            elif bound[1] == _WHOLE_SECOND:
                cursor_reached = oldest_updated <= bound[0]
            else:
                cursor_reached = oldest_updated < bound[0]
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
    # (Dispatching one advances the cursor to ITS merged_at, which would step
    # over any unfetched PR merged earlier -- losing it permanently.)
    unsafe_floor: str | None = None
    if merged_mode and not scan_complete and prs:
        unsafe_floor = min(pr.get("updated_at", "") for pr in prs)

    draft_filter = github_filter.get("draft")
    same_repo_filter = github_filter.get("same_repo_only")
    items: list[GithubPollItem] = []
    held_back: list[Any] = []
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

        if bound is not None and _event_position(cursor_at, pr.get("number")) <= bound:
            continue

        if unsafe_floor is not None and cursor_at >= unsafe_floor:
            held_back.append(pr.get("number"))
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
        # Prefer comparing repo ids (stable, case-independent) over full_name,
        # since GitHub repo paths are case-insensitive and a plain string `==`
        # would false-negative; fall back to casefolded full_name, failing
        # closed (never same-repo) only when head.repo is missing entirely.
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
        items.append(
            GithubPollItem(
                event=event,
                updated_at=cursor_at,
                dispatchable=dispatchable,
                cursor=_cursor_for(cursor_at, pr.get("number")),
            )
        )

    # Holding back some events is ordinary (the cursor still advances past
    # what was returned). Holding back everything is the stuck shape: no
    # cursor advance, and the next scan repeats this one indistinguishably
    # from a quiet repo -- that's the only case worth warning about.
    if held_back and not items:
        _log.warning(
            "%s: merged-PR scan found %d event(s) past the cursor and dispatched "
            "none -- all sit at or above the unproven boundary %s of a truncated "
            "scan (PR numbers: %s). No cursor advances, so the next poll repeats "
            "this one. If this persists, the backlog above the stored cursor is "
            "deeper than the scan can reach.",
            repo,
            len(held_back),
            unsafe_floor,
            ", ".join(str(n) for n in held_back[:10]) + ("..." if len(held_back) > 10 else ""),
        )

    # API order (updated_at desc) isn't contractually identical to the
    # cursor field in merged mode; sort explicitly so cursor advance stays
    # monotone in both modes. By the stored value rather than the timestamp,
    # so events sharing a second advance in a defined order too.
    items.sort(key=lambda it: it.cursor)
    return GithubPollResult(items=items, scan_complete=scan_complete, poll_status="ok")

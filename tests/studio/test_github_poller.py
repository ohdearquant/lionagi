# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the github_poll poller: emitted fields (head_sha, draft), the
draft github_filter, ordering, and the cursor high-water-mark behavior.

github_poll() no longer persists github_cursor itself (that moved to the
caller, SchedulerEngine._tick_github, so per-event dispatch can gate how far
the cursor actually advances) -- these tests assert on the returned
GithubPollItem list instead of a StateDB write.
"""

from __future__ import annotations

import asyncio

from lionagi.studio.scheduler import github as gh_mod


def _pr(number, updated, *, draft=False, sha=None, state="open", merged_at=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/owner/name/pull/{number}",
        "user": {"login": "octocat"},
        "updated_at": updated,
        "draft": draft,
        "head": {"sha": sha or f"sha{number}"},
        "state": state,
        "merged_at": merged_at,
    }


class _FakeResp:
    def __init__(self, prs, link=None):
        self._prs = prs
        self.status_code = 200
        self.headers = {"x-ratelimit-remaining": "100", "etag": '"abc"'}
        if link:
            self.headers["link"] = link

    def json(self):
        return self._prs


class _FakeClient:
    def __init__(self, prs):
        self._prs = prs
        self.requests: list[dict] = []

    async def get(self, url, headers=None, params=None):
        self.requests.append({"url": url, "params": params})
        return _FakeResp(self._prs)


class _FakePaginatedClient:
    """Fake client serving a fixed sequence of pages. Every response but the
    last carries a ``Link: rel="next"`` header (the real GitHub API shape),
    so github_poll's merged-mode pagination loop follows it exactly the way
    it would follow a real Link header."""

    def __init__(self, pages: list[list[dict]]):
        self._pages = pages
        self.requests: list[dict] = []

    async def get(self, url, headers=None, params=None):
        page_index = len(self.requests)
        self.requests.append({"url": url, "params": params})
        prs = self._pages[page_index] if page_index < len(self._pages) else []
        has_next = page_index + 1 < len(self._pages)
        link = None
        if has_next:
            next_url = f"https://api.github.com/repos/owner/name/pulls?page={page_index + 2}"
            link = f'<{next_url}>; rel="next"'
        return _FakeResp(prs, link=link)


def _install(monkeypatch, prs):
    """Wire the poller's token and HTTP client to fakes."""

    async def _fake_token():
        return "faketoken"

    client = _FakeClient(prs)
    monkeypatch.setattr(gh_mod, "_get_gh_token", _fake_token)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: client)
    return client


def _install_paginated(monkeypatch, pages):
    """Wire the poller's token and HTTP client to a multi-page fake."""

    async def _fake_token():
        return "faketoken"

    client = _FakePaginatedClient(pages)
    monkeypatch.setattr(gh_mod, "_get_gh_token", _fake_token)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: client)
    return client


def _poll(schedule):
    return asyncio.run(gh_mod.github_poll(schedule))


def test_github_poll_emits_head_sha_and_draft(monkeypatch):
    """A polled PR surfaces head_sha and draft alongside the existing fields."""
    _install(monkeypatch, [_pr(7, "2026-07-07T10:00:00Z", draft=False, sha="deadbeef")])
    items = _poll({"id": "s1", "github_repo": "owner/name"})
    assert len(items) == 1
    item = items[0]
    assert item.dispatchable is True
    assert item.updated_at == "2026-07-07T10:00:00Z"
    ev = item.event
    assert ev["pr_number"] == 7
    assert ev["head_sha"] == "deadbeef"
    assert ev["draft"] is False
    assert ev["pr_author"] == "octocat"


def test_github_poll_draft_filter_true_keeps_only_drafts_dispatchable(monkeypatch):
    """github_filter={'draft': true} marks only draft PRs dispatchable; the
    non-draft PR is still returned (for cursor bookkeeping) but flagged off."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    items = _poll({"id": "s1", "github_repo": "owner/name", "github_filter": {"draft": True}})
    by_number = {i.event["pr_number"]: i for i in items}
    assert by_number[1].dispatchable is False
    assert by_number[2].dispatchable is True


def test_github_poll_draft_filter_false_excludes_drafts(monkeypatch):
    """github_filter={'draft': false} marks only non-draft PRs dispatchable."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    items = _poll({"id": "s1", "github_repo": "owner/name", "github_filter": {"draft": False}})
    by_number = {i.event["pr_number"]: i for i in items}
    assert by_number[1].dispatchable is True
    assert by_number[2].dispatchable is False


def test_github_poll_no_draft_filter_emits_all_dispatchable(monkeypatch):
    """Without a draft key, both draft and ready PRs are dispatchable."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    items = _poll({"id": "s1", "github_repo": "owner/name"})
    assert all(i.dispatchable for i in items)
    assert sorted(i.event["pr_number"] for i in items) == [1, 2]


def test_github_poll_orders_oldest_first(monkeypatch):
    """The API returns PRs newest-first; github_poll reverses them so a caller
    advancing the cursor incrementally, oldest event first, stays monotone."""
    _install(
        monkeypatch,
        [
            _pr(3, "2026-07-07T12:00:00Z"),
            _pr(2, "2026-07-07T11:00:00Z"),
            _pr(1, "2026-07-07T10:00:00Z"),
        ],
    )
    items = _poll({"id": "s1", "github_repo": "owner/name"})
    assert [i.event["pr_number"] for i in items] == [1, 2, 3]
    assert [i.updated_at for i in items] == [
        "2026-07-07T10:00:00Z",
        "2026-07-07T11:00:00Z",
        "2026-07-07T12:00:00Z",
    ]


def test_github_poll_filtered_pr_still_returned_for_cursor_advance(monkeypatch):
    """A draft-filtered PR that is the newest is still returned (dispatchable
    False) rather than dropped, so the caller can advance its cursor past it
    and avoid re-listing it forever."""
    _install(
        monkeypatch,
        [
            # Newest is a draft; the filter wants non-drafts only.
            _pr(2, "2026-07-07T12:00:00Z", draft=True),
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
        ],
    )
    items = _poll(
        {
            "id": "s1",
            "github_repo": "owner/name",
            "github_filter": {"draft": False},
            "github_cursor": "2026-07-07T09:00:00Z",
        }
    )
    assert [i.event["pr_number"] for i in items] == [1, 2]
    assert [i.dispatchable for i in items] == [True, False]
    assert items[-1].updated_at == "2026-07-07T12:00:00Z"


def test_github_poll_non_bool_draft_filter_ignored(monkeypatch):
    """A malformed non-bool draft filter (e.g. the string 'false') is ignored —
    fail open to no filtering rather than silently matching the wrong side."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    items = _poll({"id": "s1", "github_repo": "owner/name", "github_filter": {"draft": "false"}})
    assert all(i.dispatchable for i in items)
    assert sorted(i.event["pr_number"] for i in items) == [1, 2]


def test_github_poll_respects_cursor_high_water_mark(monkeypatch):
    """PRs at or below the stored cursor are not returned at all."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T09:00:00Z"),
            _pr(2, "2026-07-07T10:00:00Z"),
        ],
    )
    items = _poll(
        {"id": "s1", "github_repo": "owner/name", "github_cursor": "2026-07-07T09:00:00Z"}
    )
    assert [i.event["pr_number"] for i in items] == [2]


# ---------------------------------------------------------------------------
# github_filter={"event": "pr_merged"} -- merged-PR mode
# ---------------------------------------------------------------------------


def test_github_poll_merged_mode_polls_closed_state(monkeypatch):
    """github_filter={'event': 'pr_merged'} polls state=closed, overriding
    any explicit (nonsensical) open/other state in the filter."""
    client = _install(monkeypatch, [])
    _poll(
        {
            "id": "s1",
            "github_repo": "owner/name",
            "github_filter": {"event": "pr_merged", "state": "open"},
        }
    )
    assert client.requests[-1]["params"]["state"] == "closed"


def test_github_poll_merged_mode_fires_only_on_merged_prs(monkeypatch):
    """A merged PR is dispatchable; a closed-but-unmerged PR in the same
    response never fires."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", state="closed", merged_at="2026-07-07T10:00:00Z"),
            _pr(2, "2026-07-07T11:00:00Z", state="closed", merged_at=None),
        ],
    )
    items = _poll(
        {"id": "s1", "github_repo": "owner/name", "github_filter": {"event": "pr_merged"}}
    )
    assert [i.event["pr_number"] for i in items] == [1]
    assert items[0].dispatchable is True


def test_github_poll_merged_mode_threads_pr_merged_at_into_event(monkeypatch):
    """The merged event dict carries pr_merged_at for {{pr_merged_at}}
    template rendering, alongside the PR's own updated_at."""
    _install(
        monkeypatch,
        [
            _pr(
                9,
                "2026-07-07T10:05:00Z",
                state="closed",
                merged_at="2026-07-07T10:00:00Z",
            )
        ],
    )
    items = _poll(
        {"id": "s1", "github_repo": "owner/name", "github_filter": {"event": "pr_merged"}}
    )
    assert len(items) == 1
    ev = items[0].event
    assert ev["pr_merged_at"] == "2026-07-07T10:00:00Z"
    assert ev["updated_at"] == "2026-07-07T10:05:00Z"


def test_github_poll_merged_mode_cursor_uses_merged_at(monkeypatch):
    """The cursor high-water-mark field (GithubPollItem.updated_at) holds
    merged_at, not the PR's raw updated_at, in merged mode -- a PR merged
    before the stored cursor is excluded even if its updated_at is newer."""
    _install(
        monkeypatch,
        [
            _pr(
                1,
                "2026-07-07T12:00:00Z",  # updated_at is AFTER the cursor...
                state="closed",
                merged_at="2026-07-07T09:00:00Z",  # ...but merged_at is BEFORE it.
            ),
            _pr(
                2,
                "2026-07-07T11:00:00Z",
                state="closed",
                merged_at="2026-07-07T10:30:00Z",  # merged_at is AFTER the cursor.
            ),
        ],
    )
    items = _poll(
        {
            "id": "s1",
            "github_repo": "owner/name",
            "github_filter": {"event": "pr_merged"},
            "github_cursor": "2026-07-07T10:00:00Z",
        }
    )
    assert [i.event["pr_number"] for i in items] == [2]
    assert items[0].updated_at == "2026-07-07T10:30:00Z"


def test_github_poll_merged_mode_cursor_stays_monotone_when_api_order_diverges(monkeypatch):
    """Items come back sorted by the cursor field (merged_at in this mode),
    not by raw API order, even when the two orderings diverge."""
    _install(
        monkeypatch,
        [
            # API order (updated_at desc): PR 3 first, then PR 1, then PR 2 --
            # but merged_at order is 1, 2, 3, a different sequence entirely.
            _pr(3, "2026-07-07T13:00:00Z", state="closed", merged_at="2026-07-07T13:00:00Z"),
            _pr(1, "2026-07-07T12:00:00Z", state="closed", merged_at="2026-07-07T09:00:00Z"),
            _pr(2, "2026-07-07T11:00:00Z", state="closed", merged_at="2026-07-07T10:00:00Z"),
        ],
    )
    items = _poll(
        {"id": "s1", "github_repo": "owner/name", "github_filter": {"event": "pr_merged"}}
    )
    assert [i.event["pr_number"] for i in items] == [1, 2, 3]
    assert [i.updated_at for i in items] == [
        "2026-07-07T09:00:00Z",
        "2026-07-07T10:00:00Z",
        "2026-07-07T13:00:00Z",
    ]


def test_github_poll_open_pr_mode_untouched_by_merged_mode_changes(monkeypatch):
    """The default (no event filter) open-PR mode is unaffected: it still
    polls state=open and uses updated_at as the cursor field."""
    client = _install(
        monkeypatch,
        [
            _pr(2, "2026-07-07T11:00:00Z"),
            _pr(1, "2026-07-07T10:00:00Z"),
        ],
    )
    items = _poll({"id": "s1", "github_repo": "owner/name"})
    assert client.requests[-1]["params"]["state"] == "open"
    assert [i.event["pr_number"] for i in items] == [1, 2]
    assert [i.updated_at for i in items] == ["2026-07-07T10:00:00Z", "2026-07-07T11:00:00Z"]
    assert "pr_merged_at" not in items[0].event


def test_github_poll_merged_mode_pages_past_closed_unmerged_noise(monkeypatch):
    """Starvation shape: a full first page of closed-but-never-merged PRs (all
    newer than the cursor) would, without pagination, push an older but still
    undispatched merged PR on page 2 out of reach forever -- the merged PR's
    updated_at is older than every unmerged PR on page 1, but its merged_at is
    still after the cursor, so it must be found and dispatched.

    Page 1 is exactly per_page (20) items long -- the poller's own signal
    that there may be more -- and its oldest item's updated_at is still newer
    than the cursor, so github_poll must follow the Link: rel="next" header
    onto page 2 rather than stopping at page 1.
    """
    cursor = "2026-06-01T00:00:00Z"
    page1 = [
        _pr(
            100 + n,
            f"2026-07-06T{10 - n // 10:02d}:{59 - (n % 10) * 5:02d}:00Z",
            state="closed",
            merged_at=None,
        )
        for n in range(20)
    ]
    # Sanity: page1 is a full page, strictly newer than the cursor throughout.
    assert len(page1) == 20
    assert all(pr["updated_at"] > cursor for pr in page1)

    merged_pr = _pr(
        50,
        "2026-07-06T09:00:00Z",  # older than every page-1 item's updated_at...
        state="closed",
        merged_at="2026-06-15T00:00:00Z",  # ...but merged after the cursor.
    )
    page2 = [merged_pr]

    client = _install_paginated(monkeypatch, [page1, page2])
    items = _poll(
        {
            "id": "s1",
            "github_repo": "owner/name",
            "github_filter": {"event": "pr_merged"},
            "github_cursor": cursor,
        }
    )

    assert [i.event["pr_number"] for i in items] == [50]
    assert items[0].dispatchable is True
    # One initial fetch plus exactly one pagination fetch -- page 2 was short
    # (1 < per_page), so the loop stops there rather than paging further.
    assert len(client.requests) == 2


def test_github_poll_merged_mode_stops_paging_once_cursor_reached(monkeypatch):
    """Once a fetched page's oldest item has fallen to/below the cursor,
    github_poll stops paging even if that page is full -- everything past
    that point is already-seen ground, merged or not."""
    cursor = "2026-07-06T09:30:00Z"
    page1 = [
        _pr(
            100 + n,
            f"2026-07-06T{10 - n // 10:02d}:{59 - (n % 10) * 5:02d}:00Z",
            state="closed",
            merged_at=None,
        )
        for n in range(20)
    ]
    # Oldest item on page1 must already be at/under the cursor.
    assert page1[-1]["updated_at"] <= cursor

    page2 = [_pr(50, "2026-07-06T08:00:00Z", state="closed", merged_at="2026-06-15T00:00:00Z")]

    client = _install_paginated(monkeypatch, [page1, page2])
    items = _poll(
        {
            "id": "s1",
            "github_repo": "owner/name",
            "github_filter": {"event": "pr_merged"},
            "github_cursor": cursor,
        }
    )

    assert items == []
    # Only the initial fetch -- page1's oldest item already reached the
    # cursor, so the pagination loop never follows Link: rel="next".
    assert len(client.requests) == 1

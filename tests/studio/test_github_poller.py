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


def _pr(number, updated, *, draft=False, sha=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/owner/name/pull/{number}",
        "user": {"login": "octocat"},
        "updated_at": updated,
        "draft": draft,
        "head": {"sha": sha or f"sha{number}"},
    }


class _FakeResp:
    def __init__(self, prs):
        self._prs = prs
        self.status_code = 200
        self.headers = {"x-ratelimit-remaining": "100", "etag": '"abc"'}

    def json(self):
        return self._prs


class _FakeClient:
    def __init__(self, prs):
        self._prs = prs

    async def get(self, url, headers=None, params=None):
        return _FakeResp(self._prs)


def _install(monkeypatch, prs):
    """Wire the poller's token and HTTP client to fakes."""

    async def _fake_token():
        return "faketoken"

    monkeypatch.setattr(gh_mod, "_get_gh_token", _fake_token)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: _FakeClient(prs))


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

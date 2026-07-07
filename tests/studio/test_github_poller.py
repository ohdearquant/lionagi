# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the github_poll poller: emitted fields (head_sha, draft) and
the draft github_filter, with the cursor high-water-mark behavior."""

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
    """Wire the poller's token, HTTP client, and StateDB write to fakes.

    Returns a list that captures (schedule_id, update_fields) for each cursor
    write so a test can assert the high-water mark that was persisted."""
    cursor_writes: list = []

    async def _fake_token():
        return "faketoken"

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def update_schedule(self, schedule_id, **fields):
            cursor_writes.append((schedule_id, fields))

    monkeypatch.setattr(gh_mod, "_get_gh_token", _fake_token)
    monkeypatch.setattr(gh_mod, "_get_client", lambda: _FakeClient(prs))
    monkeypatch.setattr(gh_mod, "StateDB", _FakeDB)
    return cursor_writes


def _poll(schedule):
    return asyncio.run(gh_mod.github_poll(schedule))


def test_github_poll_emits_head_sha_and_draft(monkeypatch):
    """A polled PR surfaces head_sha and draft alongside the existing fields."""
    _install(monkeypatch, [_pr(7, "2026-07-07T10:00:00Z", draft=False, sha="deadbeef")])
    events = _poll({"id": "s1", "github_repo": "owner/name"})
    assert len(events) == 1
    ev = events[0]
    assert ev["pr_number"] == 7
    assert ev["head_sha"] == "deadbeef"
    assert ev["draft"] is False
    assert ev["pr_author"] == "octocat"


def test_github_poll_draft_filter_true_keeps_only_drafts(monkeypatch):
    """github_filter={'draft': true} emits only draft PRs."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    events = _poll({"id": "s1", "github_repo": "owner/name", "github_filter": {"draft": True}})
    assert [e["pr_number"] for e in events] == [2]
    assert events[0]["draft"] is True


def test_github_poll_draft_filter_false_excludes_drafts(monkeypatch):
    """github_filter={'draft': false} emits only non-draft PRs."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    events = _poll({"id": "s1", "github_repo": "owner/name", "github_filter": {"draft": False}})
    assert [e["pr_number"] for e in events] == [1]
    assert events[0]["draft"] is False


def test_github_poll_no_draft_filter_emits_all(monkeypatch):
    """Without a draft key, both draft and ready PRs are emitted."""
    _install(
        monkeypatch,
        [
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
            _pr(2, "2026-07-07T09:00:00Z", draft=True),
        ],
    )
    events = _poll({"id": "s1", "github_repo": "owner/name"})
    assert sorted(e["pr_number"] for e in events) == [1, 2]


def test_github_poll_cursor_advances_past_filtered_pr(monkeypatch):
    """A draft-filtered PR that is the newest still advances the persisted cursor,
    so it is not re-listed on every poll."""
    writes = _install(
        monkeypatch,
        [
            # Newest is a draft; the filter wants non-drafts only.
            _pr(2, "2026-07-07T12:00:00Z", draft=True),
            _pr(1, "2026-07-07T10:00:00Z", draft=False),
        ],
    )
    events = _poll(
        {
            "id": "s1",
            "github_repo": "owner/name",
            "github_filter": {"draft": False},
            "github_cursor": "2026-07-07T09:00:00Z",
        }
    )
    # Only the non-draft PR is emitted...
    assert [e["pr_number"] for e in events] == [1]
    # ...but the cursor advanced to the newest updated_at seen (the filtered draft).
    assert writes, "expected a cursor write"
    _sid, fields = writes[-1]
    assert fields.get("github_cursor") == "2026-07-07T12:00:00Z"


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
    events = _poll({"id": "s1", "github_repo": "owner/name", "github_filter": {"draft": "false"}})
    assert sorted(e["pr_number"] for e in events) == [1, 2]

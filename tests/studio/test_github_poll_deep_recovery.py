# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Raised merged-poll limits retain their scan boundary across polls."""

import pytest

from lionagi.state.db import StateDB
from lionagi.studio.scheduler import github


class _Response:
    def __init__(self, page, next_page):
        self.page = page
        self.status_code = 200
        self.headers = {"x-ratelimit-remaining": "100"}
        if next_page is not None:
            self.headers["link"] = (
                f'<https://api.github.com/repos/owner/repo/pulls?page={next_page}>; rel="next"'
            )

    def json(self):
        return self.page


class _Pages:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []
        self.index = 0

    async def get(self, url, headers=None, params=None):
        if params is not None:
            self.index = 0
        index = self.index
        self.index += 1
        self.requests.append(url)
        return _Response(self.pages[index], index + 2 if index + 1 < len(self.pages) else None)


def _pr(number, updated, merged=None):
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "user": {"login": "author"},
        "state": "closed",
        "updated_at": updated,
        "merged_at": merged,
        "head": {"sha": "synthetic", "repo": None},
    }


def _install(monkeypatch, pages):
    client = _Pages(pages)

    async def token(**_kwargs):
        return "synthetic"

    monkeypatch.setattr(github, "_cached_token", None)
    monkeypatch.setattr(github, "_get_gh_token", token)
    monkeypatch.setattr(github, "_get_client", lambda: client)
    monkeypatch.setattr(github, "_PER_PAGE", 2)
    return client


@pytest.mark.asyncio
async def test_unmerged_deep_scan_repeats_after_store_reopen(tmp_path, monkeypatch):
    pages = [
        [_pr(100 + 2 * i, "2026-10-24T12:00:00Z"), _pr(101 + 2 * i, "2026-10-24T12:00:00Z")]
        for i in range(6)
    ]
    pages.append([_pr(999, "2026-10-24T11:00:00Z")])
    client = _install(monkeypatch, pages)
    path = tmp_path / "state.db"
    cursor = github._cursor_for("2026-10-24T10:00:00Z", 1)
    async with StateDB(path) as db:
        await db.create_schedule(
            {
                "id": "poll",
                "name": "poll",
                "trigger_type": "github_poll",
                "github_repo": "owner/repo",
                "github_cursor": cursor,
                "github_filter": {"event": "pr_merged", "merged_page_limit": 7},
                "action_kind": "agent",
            }
        )
        for _ in range(2):
            row = await db.get_schedule("poll")
            result = await github.github_poll(row)
            assert result.scan_complete and result.items == []
            assert row["github_cursor"] == cursor
        assert len(client.requests) == 14

    async with StateDB(path) as reopened:
        row = await reopened.get_schedule("poll")
        assert row["github_filter"]["merged_page_limit"] == 7
        assert row["github_cursor"] == cursor
        result = await github.github_poll(row)
        assert result.scan_complete and result.items == []
        assert len(client.requests) == 21
        assert (await reopened.get_schedule("poll"))["github_cursor"] == cursor


@pytest.mark.asyncio
async def test_raised_limit_boundary_page_keeps_same_second_ties(monkeypatch):
    timestamp = "2026-10-24T10:00:00Z"
    pages = [
        [_pr(100 + 2 * i, "2026-10-24T12:00:00Z"), _pr(101 + 2 * i, "2026-10-24T12:00:00Z")]
        for i in range(5)
    ]
    pages.append([_pr(12, timestamp, timestamp), _pr(9, timestamp, timestamp)])
    pages.append([_pr(11, timestamp, timestamp), _pr(8, "2026-10-24T09:00:00Z")])
    # The seventh full page crosses the cursor; the advertised eighth page
    # must not be fetched after that complete boundary.
    pages.append([_pr(7, "2026-10-24T08:00:00Z")])
    client = _install(monkeypatch, pages)
    schedule = {
        "id": "poll",
        "github_repo": "owner/repo",
        "github_filter": {"event": "pr_merged", "merged_page_limit": 6},
        "github_cursor": github._cursor_for(timestamp, 10),
    }
    capped = await github.github_poll(schedule)
    assert not capped.scan_complete and capped.items == []
    assert len(client.requests) == 6

    schedule["github_filter"]["merged_page_limit"] = 7
    complete = await github.github_poll(schedule)
    assert complete.scan_complete
    assert [item.event["pr_number"] for item in complete.items] == [11, 12]
    assert len(client.requests) == 13
    assert complete.items[0].cursor == github._cursor_for(timestamp, 11)
    assert complete.items[1].cursor == github._cursor_for(timestamp, 12)

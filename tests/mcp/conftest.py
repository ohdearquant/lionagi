# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import pytest

from lionagi.mcp import jobs


@pytest.fixture(autouse=True)
def short_startup_watch(monkeypatch):
    """Shorten, but do not disable, submit()'s startup-refusal watch.

    submit() watches a fresh child for a few seconds so a run that dies on its
    own arguments comes back as a refused submit rather than as a run id. Most
    tests here spawn a double that never exits, so each one would otherwise sit
    out the full window.

    Shortened rather than switched off on purpose: at zero the watch would be
    skipped entirely and every one of these tests would keep passing with the
    mechanism removed. At a few milliseconds it still runs, and a test that
    needs the real timing sets its own value.
    """
    monkeypatch.setattr(jobs, "_EARLY_EXIT_WATCH_SECONDS", 0.01)

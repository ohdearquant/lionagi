# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The two test roots share one RSS observation per test."""

import json
import sys
from types import SimpleNamespace

import pytest

from tests import _shared_conftest as shared

resource = pytest.importorskip("resource")


def test_rss_hook_records_once_when_both_roots_register_it(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "_RSS_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(shared, "_resource", resource, raising=False)
    monkeypatch.setattr(shared, "_RSS_DIV", 1024 if sys.platform == "darwin" else 1, raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    item = SimpleNamespace(nodeid="tests/example.py::test_example")

    for _ in range(2):
        outer = shared._trace_rss(item, None)
        inner = shared._trace_rss(item, None)
        next(outer)
        next(inner)
        with pytest.raises(StopIteration):
            next(inner)
        with pytest.raises(StopIteration):
            next(outer)

    records = [json.loads(line) for line in (tmp_path / "rss-main.jsonl").read_text().splitlines()]
    assert [record["phase"] for record in records] == ["start", "end", "start", "end"]
    assert all(record["test"] == item.nodeid for record in records)
    assert not hasattr(item, "_lionagi_rss_recording")

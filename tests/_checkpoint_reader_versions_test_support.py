# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Setup shared by module and adapter contracts."""

from __future__ import annotations

_RUN_ID = "synthetic-run"


def _base(version):
    return {
        "version": version,
        "generation": "0123456789abcdef0123456789abcdef",
        "journal_seq": 0,
        "session_id": "synthetic-session",
        "prompt": "synthetic",
        "plan": [],
        "config": {},
        "flow_context": {"nested": {"entries": []}},
        "ops": {},
        "spawned": [{"node_id": "base-child", "parent_id": "base-parent"}],
    }


def _write_fixture(root, text):
    directory = root / _RUN_ID
    directory.mkdir()
    checkpoint = directory / "checkpoint.json"
    checkpoint.write_text(text, encoding="utf-8")
    (directory / "checkpoint.json.journal").write_text(
        '{"seq":1,"entry":{"node_id":"journal-child","parent_id":"journal-parent"}}\n',
        encoding="utf-8",
    )
    return checkpoint

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The shared team JSON reader handles valid, absent, and corrupt files."""

from __future__ import annotations

import json

from lionhub.orchestration.team import read_team_json


class TestReadTeamJson:
    def test_reads_valid_file(self, tmp_path):
        path = tmp_path / "r1.json"
        path.write_text(
            json.dumps(
                {"id": "r1", "name": "t", "members": ["orchestrator", "alice"], "messages": []}
            )
        )
        data = read_team_json(path)
        assert data["id"] == "r1"

    def test_missing_file_returns_none(self, tmp_path):
        assert read_team_json(tmp_path / "nope.json") is None

    def test_corrupt_json_returns_none(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{not json")
        assert read_team_json(path) is None

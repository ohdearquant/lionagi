# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the cited status API to its caller-supplied provenance fields."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/adr/ADR-0109-mirrored-session-idleness-is-not-completion.md"


def test_status_update_citation_points_to_source_and_actor_parameters() -> None:
    record = RECORD.read_text(encoding="utf-8")
    source = (ROOT / "lionagi/state/db.py").read_text(encoding="utf-8").splitlines()
    assert "lionagi/state/db.py:4999-5010" in record
    assert "async def update_status(" in source[4999 - 1]
    assert 'source: str = "executor"' in source[5008 - 1]
    assert "actor: str | None = None" in source[5009 - 1]
    assert "metadata: dict[str, Any] | None = None" in source[5010 - 1]
    actor = (ROOT / "lionagi/state/lifecycle/models.py").read_text(encoding="utf-8").splitlines()
    assert "lionagi/state/lifecycle/models.py:49-52" in record
    assert "class ActorRecord:" in actor[50 - 1]
    assert "id: str" in actor[52 - 1]

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the cited status API to its caller-supplied provenance fields."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/adr/ADR-0109-mirrored-session-idleness-is-not-completion.md"


def _source_of(relpath: str, qualname: str) -> str:
    """Source of the def or class that a record cites as ``relpath::qualname``."""
    text = (ROOT / relpath).read_text(encoding="utf-8")
    node: ast.AST = ast.parse(text)
    for part in qualname.split("."):
        node = next(
            child
            for child in ast.iter_child_nodes(node)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and child.name == part
        )
    segment = ast.get_source_segment(text, node)
    assert segment is not None
    return segment


def test_status_update_citation_points_to_source_and_actor_parameters() -> None:
    record = RECORD.read_text(encoding="utf-8")
    assert "lionagi/state/db.py::StateDB.update_status" in record
    update_status = _source_of("lionagi/state/db.py", "StateDB.update_status")
    assert update_status.startswith("async def update_status(")
    assert 'source: str = "executor"' in update_status
    assert "actor: str | None = None" in update_status
    assert "metadata: dict[str, Any] | None = None" in update_status
    assert "lionagi/state/lifecycle/models.py::ActorRecord" in record
    actor = _source_of("lionagi/state/lifecycle/models.py", "ActorRecord")
    assert actor.startswith("class ActorRecord:")
    assert "id: str" in actor

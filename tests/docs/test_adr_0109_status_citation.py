# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Verify the status API and terminal readers cited by ADR-0109."""

import ast
import re
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
    assert "lionhub/src/lionhub/state/db.py::StateDB.update_status" in record
    update_status = _source_of("lionhub/src/lionhub/state/db.py", "StateDB.update_status")
    assert update_status.startswith("async def update_status(")
    assert 'source: str = "executor"' in update_status
    assert "actor: str | None = None" in update_status
    assert "metadata: dict[str, Any] | None = None" in update_status
    assert "lionhub/src/lionhub/state/lifecycle/models.py::ActorRecord" in record
    actor = _source_of("lionhub/src/lionhub/state/lifecycle/models.py", "ActorRecord")
    assert actor.startswith("class ActorRecord:")
    assert "id: str" in actor


def test_terminal_status_consumers_name_the_reading_functions() -> None:
    record = RECORD.read_text(encoding="utf-8")
    _, heading, section = record.partition("\n## Consumers keying on session terminal status\n")
    assert heading
    table = section.partition("\n## ")[0]
    sites = [line.split("|", 2)[1] for line in table.splitlines() if line.startswith("| `")]
    paths = [
        (path, name)
        for site in sites
        for path, name in re.findall(r"`([^ `]+\.py)::([^ `]+)`", site)
    ]
    cli = "lionhub/src/lionhub/serve/cli"
    assert [name for path, name in paths if path == f"{cli}/monitor.py"] == [
        "_effective_session_status",
        "_poll_pending_sessions_once",
    ]
    assert [name for path, name in paths if path == f"{cli}/agent.py"] == [
        "_tombstone_pending_steers",
        "_run_agent",
    ]
    for path, name in paths:
        tree = ast.parse(_source_of(path, name))
        assert any(
            isinstance(node, ast.Name)
            and node.id == "SESSION_TERMINAL_STATUSES"
            and isinstance(node.ctx, ast.Load)
            for node in ast.walk(tree)
        ), f"{path}::{name} does not read SESSION_TERMINAL_STATUSES"

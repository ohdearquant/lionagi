from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def agent_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal .lionagi/agents/reviewer.md discoverable via cwd; see
    test_schedule_declaration.py for the identical fixture rationale."""
    import lionhub.orchestration.spawn as providers_mod

    agents_dir = tmp_path / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(providers_mod, "_find_lionagi_dirs", lambda cwd=None: [agents_dir.parent])
    return tmp_path


def _legacy_row(schedule_id: str, name: str, *, cwd: Path, **overrides) -> dict:
    row = {
        "id": schedule_id,
        "name": name,
        "trigger_type": "cron",
        "cron_expr": "0 2 * * *",
        # No stored zone: the zone the scheduler recorded the row firing in.
        "effective_timezone": "America/New_York",
        "action_kind": "agent",
        "action_agent": "reviewer",
        "action_prompt": "check things",
        "action_cwd": str(cwd),
        "enabled": 1,
    }
    row.update(overrides)
    return row

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
    """A minimal .lionagi/agents/reviewer.md discoverable via cwd.

    lionhub.orchestration.spawn imports find_lionagi_dirs by value at module load
    time, so a global home directory that happens to declare its own
    same-named profiles (e.g. a real ~/.lionagi/agents/reviewer.md) can
    shadow-merge with this fixture's directory. Pin the module's resolved
    dirs list directly so this test only ever sees its own tmp_path.
    """
    import lionhub.orchestration.spawn as providers_mod

    agents_dir = tmp_path / ".lionagi" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reviewer.md").write_text("---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n")
    (agents_dir / "no-model.md").write_text("Body only, no frontmatter model.\n")
    monkeypatch.chdir(tmp_path)

    def find_dirs(cwd=None):
        # The nearest .lionagi at or above the directory searched from, as the
        # real finder walks, kept inside tmp_path so no real directory is read.
        start = Path.cwd() if cwd is None else Path(cwd)
        if not start.is_relative_to(tmp_path):
            return [agents_dir.parent]
        for directory in (start, *start.parents):
            if (directory / ".lionagi").is_dir():
                return [directory / ".lionagi"]
            if directory == tmp_path:
                break
        return []

    monkeypatch.setattr(providers_mod, "_find_lionagi_dirs", find_dirs)
    return tmp_path


@pytest.fixture
def pinned_scheduler_tz(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("lionhub.orchestration.schedule.timing.SCHEDULER_TZ", "America/New_York")
    return "America/New_York"


def _agent_manifest(name: str, *, cwd: Path, extra_member: str = "") -> str:
    return f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  {name}:
    trigger:
      cron:
        expression: "0 2 * * *"
        timezone: America/New_York
    target:
      kind: agent
      profile: reviewer
      prompt: "check things"
    execution:
      cwd: {cwd}
{extra_member}
"""


def _command_manifest(args_yaml: str, cwd: Path) -> str:
    return f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  m:
    trigger:
      every: 1h
    target:
      kind: command
      executable: refresh-index
      args:
{args_yaml}
    execution:
      cwd: {cwd}
"""


_AGENT_TARGET_BLOCK = (
    '    target:\n      kind: agent\n      profile: reviewer\n      prompt: "check things"\n'
)


def _manifest_with_target(name: str, *, cwd: Path, target: str, extra: str = "") -> str:
    """The agent member's manifest with its target block replaced by *target*."""
    return _agent_manifest(name, cwd=cwd, extra_member=extra).replace(_AGENT_TARGET_BLOCK, target)


def _table_kind_cases() -> list[tuple[str, str]]:
    """(column, kind) for every column the table gives an action-kind set."""
    from lionhub.orchestration.schedule.declaration import _LAUNCH_READERS

    return [
        (column, kind)
        for column, (kinds, _triggers) in sorted(_LAUNCH_READERS.items())
        if kinds is not None and column in _DIFFERING_ROW_OVERRIDES
        for kind in _TABLE_KINDS
    ]


def _table_trigger_cases() -> list[tuple[str, str]]:
    """(column, trigger) for every column the table gives a trigger set."""
    from lionhub.orchestration.schedule.declaration import _LAUNCH_READERS

    return [
        (column, trigger)
        for column, (_kinds, triggers) in sorted(_LAUNCH_READERS.items())
        if triggers is not None and column in _DIFFERING_TRIGGER_ROW_OVERRIDES
        for trigger in _TABLE_TRIGGERS
    ]


_DIFFERING_ROW_OVERRIDES = {
    "action_project": {"action_project": "other-project"},
    # A profile the row's own execution root resolves, so the launch accepts it.
    "action_agent": {"action_agent": "other"},
    "action_model": {"action_model": "anthropic/claude-opus-5"},
    "action_prompt": {"action_prompt": "other things"},
    "action_playbook": {"action_playbook": "other-pb"},
}


_DIFFERING_TRIGGER_ROW_OVERRIDES = {
    "cron_expr": {"cron_expr": "0 3 * * *"},
    # A row with no stored zone is read in the zone the scheduler recorded on it.
    "resolved_timezone": {"effective_timezone": "Europe/Berlin"},
    "interval_sec": {"interval_sec": 7200},
    "github_repo": {"github_repo": "acme/other"},
    "github_filter": {"github_filter": {"state": "open"}},
    "poll_interval_sec": {"poll_interval_sec": 60},
    "missed_fire_policy": {"missed_fire_policy": "run_once"},
    "overlap_policy": {"overlap_policy": "allow"},
}


_TABLE_KINDS = {"agent": "agent", "flow_yaml": "flow", "play": "play", "command": "command"}


_TABLE_TRIGGERS = {
    "cron": ("cron", "cron"),
    "interval": ("interval", "interval"),
    "at": ("at", "at_skip"),
    "github_poll": ("github", "github"),
}

_TABLE_KIND_CASES = _table_kind_cases()
_TABLE_TRIGGER_CASES = _table_trigger_cases()

"""Deterministic seed data for the seeded-daemon e2e harness.

Every id/name here is content the Playwright smoke specs assert against, so
values are fixed strings (never randomly generated) while primary keys stay
unique per run via a short uuid suffix.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# Stable, greppable names the Playwright specs assert on by accessible text.
SMOKE_SCHEDULE_NAME = "e2e-smoke-nightly-report"
SMOKE_FAILING_SCHEDULE_NAME = "e2e-smoke-flaky-sync"
SMOKE_SESSION_NAME = "e2e-smoke-completed-run"
SMOKE_AGENT_NAME = "e2e-smoke-reviewer"
SMOKE_PLAYBOOK_NAME = "e2e-smoke-release-checklist"
SMOKE_PROJECT_NAME = "e2e-smoke-project"

SESSION_STATUSES = (
    "running",
    "completed",
    "failed",
    "timed_out",
    "aborted",
    "cancelled",
)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def seed_state_db(db: StateDB) -> dict[str, Any]:
    """Populate *db* with fixtures covering every session status, a stale
    phantom "running" session, failing schedules/invocations, and a project.

    Returns a manifest of created ids, mostly useful for debugging the
    harness itself -- the Playwright specs assert on the stable names above,
    not on these ids.
    """
    now = time.time()
    manifest: dict[str, Any] = {"sessions": {}, "invocations": {}, "schedules": {}}

    for status in SESSION_STATUSES:
        session_id = _uid(f"session-{status}")
        progression_id = _uid("prog")
        await db.create_progression(progression_id)
        name = SMOKE_SESSION_NAME if status == "completed" else f"e2e-smoke-session-{status}"
        await db.create_session(
            {
                "id": session_id,
                "progression_id": progression_id,
                "name": name,
                "status": status,
                "invocation_kind": "agent",
                "source_kind": "live",
                "started_at": now - 3600,
                "ended_at": None if status == "running" else now - 60,
                "agent_name": SMOKE_AGENT_NAME,
                "project": SMOKE_PROJECT_NAME,
                "project_source": "studio",
            }
        )
        manifest["sessions"][status] = session_id

    # A "running" session that is actually long-stale with no live process
    # behind it. The daemon's own startup reconciliation
    # (studio.services.lifecycle.reap_phantom_sessions) transitions this to
    # "failed" before /health ever responds -- seeded anyway so that real
    # reconciliation code path runs for real instead of being mocked out.
    # Its updated_at is set 2h in the past, past the 1h PHANTOM_STALE_HOURS
    # default, and it carries no artifacts_path/pid file, so it always
    # classifies as a dead-process phantom.
    phantom_id = _uid("session-phantom")
    phantom_progression_id = _uid("prog")
    await db.create_progression(phantom_progression_id)
    await db.create_session(
        {
            "id": phantom_id,
            "progression_id": phantom_progression_id,
            "name": "e2e-smoke-session-phantom-stale",
            "status": "running",
            "invocation_kind": "agent",
            "source_kind": "live",
            "started_at": now - 7200,
            "updated_at": now - 7200,
            "agent_name": SMOKE_AGENT_NAME,
            "project": SMOKE_PROJECT_NAME,
            "project_source": "studio",
        }
    )
    manifest["sessions"]["phantom"] = phantom_id

    # An invocation that failed with a reason code + error detail.
    failed_invocation_id = _uid("invocation")
    await db.create_invocation(
        {
            "id": failed_invocation_id,
            "skill": "e2e-smoke-review",
            "started_at": now - 1800,
            "status": "running",
        }
    )
    await db.update_invocation(
        failed_invocation_id,
        status="failed",
        ended_at=now - 1700,
        reason_code=RunReasons.FAILED_EXIT_NONZERO,
        reason_summary="e2e smoke fixture: nonzero exit seeded for coverage",
    )
    manifest["invocations"]["failed"] = failed_invocation_id

    # Schedules are seeded disabled so the daemon's live 30s scheduler tick
    # never fires a real subprocess action during the test run, regardless
    # of timing (studio.scheduler.engine only evaluates enabled=True rows).
    spent_schedule_id = _uid("schedule")
    await db.create_schedule(
        {
            "id": spent_schedule_id,
            "name": SMOKE_SCHEDULE_NAME,
            "description": "Spent one-shot fixture for e2e smoke assertions.",
            "enabled": 0,
            "trigger_type": "cron",
            "cron_expr": "0 0 1 1 *",
            "action_kind": "agent",
            "action_agent": SMOKE_AGENT_NAME,
            "last_fired_at": now - 86400,
            "next_fire_at": None,
            "max_runs": 1,
            "project": SMOKE_PROJECT_NAME,
        }
    )
    manifest["schedules"]["spent"] = spent_schedule_id

    failing_schedule_id = _uid("schedule")
    await db.create_schedule(
        {
            "id": failing_schedule_id,
            "name": SMOKE_FAILING_SCHEDULE_NAME,
            "description": "Failure-streak fixture for e2e smoke assertions.",
            "enabled": 0,
            "trigger_type": "interval",
            "interval_sec": 3600,
            "action_kind": "agent",
            "action_agent": SMOKE_AGENT_NAME,
            "last_fired_at": now - 600,
            "next_fire_at": now + 3600,
            "project": SMOKE_PROJECT_NAME,
        }
    )
    manifest["schedules"]["failing"] = failing_schedule_id

    for i in range(3):
        await db.create_schedule_run(
            {
                "id": _uid("schedule-run"),
                "schedule_id": failing_schedule_id,
                "trigger_context": {"seed": True},
                "action_kind": "agent",
                "action_args": {"agent": SMOKE_AGENT_NAME},
                "status": "failed",
                "exit_code": 1,
                "fired_at": now - 600 - (i * 300),
                "ended_at": now - 590 - (i * 300),
                "error_detail": "e2e smoke fixture: simulated consecutive failure",
            }
        )

    await db.register_project(SMOKE_PROJECT_NAME, "studio")

    return manifest


def seed_filesystem_fixtures(lionagi_home: Path) -> None:
    """Write a couple of agent/playbook definition files under *lionagi_home*
    so the Library-style list pages have real rows to render."""
    agents_dir = lionagi_home / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{SMOKE_AGENT_NAME}.md").write_text(
        "---\n"
        "description: Seeded reviewer agent for the e2e smoke harness.\n"
        "model: claude-sonnet-4-5\n"
        "provider: anthropic\n"
        "---\n\n"
        "You are a seeded fixture agent used only by the Playwright smoke suite.\n"
    )

    playbooks_dir = lionagi_home / "playbooks"
    playbooks_dir.mkdir(parents=True, exist_ok=True)
    (playbooks_dir / f"{SMOKE_PLAYBOOK_NAME}.playbook.yaml").write_text(
        "description: Seeded release-checklist playbook for the e2e smoke harness.\n"
        "steps:\n"
        "  - name: seed-step\n"
        "    prompt: This is a fixture step; never executed by the smoke suite.\n"
    )

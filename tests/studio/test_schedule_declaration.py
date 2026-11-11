# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit + DB-backed integration tests for the declarative ScheduleSet layer:
closed-schema validation, per-trigger/target static resolution, and the
atomic validate/diff/apply service."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")

from lionhub_tests.orchestration.schedule import _test_support_schedule_declaration as _support
from lionhub_tests.orchestration.schedule._test_support_schedule_declaration import (
    _AGENT_TARGET_BLOCK,
    _DIFFERING_ROW_OVERRIDES,
    _DIFFERING_TRIGGER_ROW_OVERRIDES,
    _TABLE_KIND_CASES,
    _TABLE_KINDS,
    _TABLE_TRIGGER_CASES,
    _TABLE_TRIGGERS,
    _agent_manifest,
    _command_manifest,
    _manifest_with_target,
    _table_kind_cases,
    _table_trigger_cases,
)

# Fixtures shared with the support module, bound here so they apply to this module only.
agent_profile = _support.agent_profile
pinned_scheduler_tz = _support.pinned_scheduler_tz
temp_db_path = _support.temp_db_path
from pydantic import ValidationError

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.state.db import StateDB
from lionhub.orchestration.schedule.declaration import (
    _ADOPT_DECLARATION_FIELDS,
    _ADOPT_UNDECLARED_COLUMNS,
    ScheduleMember,
    ScheduleSetDocument,
    ScheduleSetError,
    _adopt_comparable,
    _adopt_spec_differences,
    _adopt_undeclarable,
    _declared_columns,
    apply_schedule_set,
    build_plan,
    parse_schedule_set,
    resolve_schedule_set,
)

# fixtures


# Closed-schema rejection at every level


# Trigger resolution


# Target resolution


_PLAY_TARGET_WITH_ARGS_BLOCK = (
    "    target:\n      kind: playbook\n      name: health-audit\n      args:\n        depth: 2\n"
)


@pytest.mark.asyncio
async def test_a_declared_playbook_target_passes_its_args_to_the_launch(
    temp_db_path, agent_profile
):
    """A playbook target's declared args reach the fire path.

    The schedules table stores a play target's args only inside the resolved
    target snapshot, so the launch must read them from there; a launch that
    reads only the one-shot args field drops them and the play runs with its
    defaults, silently.
    """
    from lionagi.studio.scheduler.subprocess import build_argv

    doc = parse_schedule_set(
        _manifest_with_target("nightly", cwd=agent_profile, target=_PLAY_TARGET_WITH_ARGS_BLOCK)
    )
    async with StateDB() as db:
        await apply_schedule_set(db, doc, agent_profile)
        row = await db.get_schedule_by_name("demo/nightly")
    argv, tmp = build_argv(row, {}, executable_prefix=["li"])
    assert argv == ["li", "play", "health-audit", "--depth", "2"]
    assert tmp is None


# cwd / project resolution


# Atomic validate/diff/apply service


_EVERY_HOUR_TRIGGER = (
    '    trigger:\n      cron:\n        expression: "0 2 * * *"\n'
    "        timezone: America/New_York\n",
    "    trigger:\n      every: 1h\n",
)


_UNLAUNCHABLE_PROMPTS = {"end-of-options": "--", "over-cap": "x" * (MAX_SPEC_PROMPT_CHARS + 1)}


@pytest.mark.asyncio
async def test_a_reapply_that_changes_the_stored_model_says_so(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    from lionhub.cli.schedule import _fmt_plan_line

    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await apply_schedule_set(db, doc, agent_profile)
        (agent_profile / ".lionagi" / "agents" / "reviewer.md").write_text(
            "---\nmodel: anthropic/claude-sonnet-5-5\n---\nBody.\n"
        )
        plan, _ = await build_plan(db, doc, agent_profile)
    (entry,) = plan
    assert entry.action == "UPDATE"
    assert entry.model_change == ("anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5-5")
    assert _fmt_plan_line(entry, "demo/automation") == (
        "UPDATE     demo/nightly  model=anthropic/claude-sonnet-5 -> anthropic/claude-sonnet-5-5"
    )


@pytest.mark.asyncio
async def test_adopt_keeps_playbook_args_the_launch_now_passes(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    manifest = f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  audit:
    trigger:
      every: 1h
    target:
      kind: playbook
      name: health-audit
      args:
        depth: 2
    execution:
      cwd: {agent_profile}
"""
    doc = parse_schedule_set(manifest)
    async with StateDB() as db:
        await db.create_schedule(
            {
                "id": "legacyplay01",
                "name": "audit",
                "enabled": 1,
                "trigger_type": "interval",
                "interval_sec": 3600,
                "action_kind": "play",
                "action_playbook": "health-audit",
                "action_cwd": str(agent_profile),
                "action_project": "demo",
                "action_command_args": [],
                "missed_fire_policy": "skip",
                "overlap_policy": "skip",
            }
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail

        # The adopted row keeps the declared args in its resolved target, which is
        # where the launch reads them, so the play fires with what the member wrote.
        from lionagi.studio.scheduler.subprocess import build_argv

        await apply_schedule_set(db, doc, agent_profile, adopt=True)
        row = await db.get_schedule_by_name("demo/audit")
        argv, _ = build_argv(row, {}, executable_prefix=["li"])
        assert argv == ["li", "play", "health-audit", "--depth", "2"]

        without_args = parse_schedule_set(manifest.replace("      args:\n        depth: 2\n", ""))
        plan, _ = await build_plan(db, without_args, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["UPDATE"]


# Command-target args: both create paths write action_command_args, so both
# must hold it to the same contract


def test_a_templated_arg_rendering_to_a_flag_is_still_refused(tmp_path):
    """The protection that does apply, kept asserted.

    A literal flag is author-written. A ``{{var}}`` element takes its value
    from trigger context, which an attacker may influence, so the rendered
    result is what gets checked — and it is checked where rendering happens.
    """
    from lionagi.studio.scheduler.subprocess import _render_command_arg

    assert _render_command_arg("--repo", {}) == "--repo"
    assert _render_command_arg("{{pr}}", {"pr": "123"}) == "123"
    with pytest.raises(ValueError, match="would inject a flag"):
        _render_command_arg("{{pr}}", {"pr": "--oops"})


# The distinct argument shapes real scheduled commands take. Each is a command
# line someone actually wrote and scheduled, reduced to its structure: an
# interpreter subcommand, a scope flag, flag/value pairs whose values are paths,
# a valueless boolean flag, a templated value behind a flag, and free-text
# positionals. Paths and names here are placeholders; the shapes are the point.
_REAL_COMMAND_ARG_SHAPES = [
    ["run", "--project", "PROJECT_DIR", "python", "worker.py", "one arg", "another"],
    [
        "run",
        "--project",
        "PROJECT_DIR",
        "python",
        "probe.py",
        "--config",
        "probe.json",
        "--state",
        "probe_state.json",
    ],
    ["run", "--no-project", "python", "health.py", "--json", "--cwd", "PROJECT_DIR"],
    ["run", "--no-project", "bash", "watch.sh"],
    [
        "run",
        "--no-project",
        "python",
        "poll.py",
        "--pr",
        "{{pr_number}}",
        "--repo",
        "owner/name",
        "--out",
        "OUT_DIR",
    ],
]


def test_a_resolved_manifest_still_refuses_an_injected_flag_at_fire_time(tmp_path, monkeypatch):
    """Manifest acceptance and fire-time rejection, joined end to end.

    Permitting author-written flags is only safe because a templated element is
    checked against its rendered value where the argv is built. Asserting
    ``_render_command_arg`` directly leaves that argument one step short: it
    does not show the fire path actually routes every element through it. This
    takes what the resolver persisted, hands it to ``build_argv``, and asserts
    a trigger-supplied flag is refused there.
    """
    from lionagi.studio.scheduler.subprocess import build_argv

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    manifest = _command_manifest(
        '        - "--repo"\n        - "owner/name"\n        - "--pr"\n        - "{{pr_number}}"\n',
        tmp_path,
    )
    resolved = resolve_schedule_set(parse_schedule_set(manifest), tmp_path)
    schedule = dict(resolved["m"].db_fields)

    argv, _ = build_argv(schedule, {"pr_number": "123"})
    assert argv == ["refresh-index", "--repo", "owner/name", "--pr", "123"]

    with pytest.raises(ValueError, match="would inject a flag"):
        build_argv(schedule, {"pr_number": "--oops"})


# ---------------------------------------------------------------------------
# Adoption reads each column the way the launch that holds it reads it
# ---------------------------------------------------------------------------


_PLAY_TARGET_BLOCK = "    target:\n      kind: playbook\n      name: health-audit\n"
_COMMAND_TARGET_BLOCK = "    target:\n      kind: command\n      executable: refresh-index\n"
_NOTIFY_BLOCK = '    notify:\n      on: [failed]\n      command: "echo done"\n'


# ---------------------------------------------------------------------------
# A refusal offers no member edit for a value no member can state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# A one-shot is compared against the fire the engine would make
# ---------------------------------------------------------------------------

_AT_PAST_TRIGGER = '      at: "2020-01-01T00:00:00+00:00"\n'
_CRON_TRIGGER_BLOCK = (
    '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n'
)
_RUN_ONCE_POLICIES = "    policies:\n      missedFire: run_once\n"


# ---------------------------------------------------------------------------
# The poll interval a github trigger reads at its own default
# ---------------------------------------------------------------------------

_GITHUB_TRIGGER_BLOCK = "      github:\n        repo: acme/widgets\n"


@pytest.mark.asyncio
async def test_github_apply_target_change_fences_old_inflight_poll(
    temp_db_path, agent_profile, monkeypatch
):
    from lionagi.studio.scheduler import github
    from lionagi.studio.scheduler.engine import SchedulerEngine
    from tests.studio.test_github_poll_stagnation import _NOW, _Client, _reap, _Service

    def doc(repo, limit):
        trigger = (
            f"      github:\n        repo: {repo}\n        filter:\n"
            f"          event: pr_merged\n          merged_page_limit: {limit}\n"
        )
        return parse_schedule_set(
            _agent_manifest("nightly", cwd=agent_profile).replace(_CRON_TRIGGER_BLOCK, trigger)
        )

    client = _Client()

    async def token(**kwargs):
        return "test-token"

    monkeypatch.setattr(github, "_cached_token", None)
    monkeypatch.setattr(github, "_get_gh_token", token)
    monkeypatch.setattr(github, "_get_client", lambda: client)
    monkeypatch.setattr(github, "_PER_PAGE", 2)
    async with StateDB(temp_db_path) as db, StateDB(temp_db_path) as editor:
        await apply_schedule_set(db, doc("acme/widgets", 5), agent_profile)
        row = await db.get_schedule_by_name("demo/nightly")
        sid = row["id"]
        for _ in range(3):
            assert await db.record_github_poll_stagnation(sid, reset=False, page_limit=5)
        unchanged = await apply_schedule_set(editor, doc("acme/widgets", 5), agent_profile)
        assert unchanged.unchanged == 1
        row = await db.get_schedule(sid)
        assert (
            row["poller_consecutive_cap_no_dispatch"],
            row["poller_last_capped_page_limit"],
            row["poller_target_generation"],
        ) == (3, 5, 0)
        limit_edit = await apply_schedule_set(editor, doc("acme/widgets", 6), agent_profile)
        assert limit_edit.updated == 1
        row = await db.get_schedule(sid)
        assert row["github_filter"]["merged_page_limit"] == 6
        assert (
            row["poller_consecutive_cap_no_dispatch"],
            row["poller_last_capped_page_limit"],
            row["poller_target_generation"],
        ) == (3, 5, 0)

        entered, release = asyncio.Event(), asyncio.Event()

        async def pause_first_page():
            client.before_get = None
            entered.set()
            await release.wait()

        client.before_get = pause_first_page
        task = asyncio.create_task(SchedulerEngine(svc=_Service(db))._tick_github(row, _NOW))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            moved = await apply_schedule_set(editor, doc("other/repo", 6), agent_profile)
            assert moved.updated == 1
            after_apply = await db.get_schedule(sid)
            assert (
                after_apply["poller_consecutive_cap_no_dispatch"],
                after_apply["poller_last_capped_page_limit"],
                after_apply["poller_target_generation"],
            ) == (0, None, 1)
            release.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            release.set()
            await _reap([task])
        row = await db.get_schedule(sid)
        assert client.calls == 6
        assert row["github_repo"] == "other/repo"
        assert (
            row["poller_consecutive_cap_no_dispatch"],
            row["poller_last_capped_page_limit"],
            row["poller_target_generation"],
        ) == (0, None, 1)


# ---------------------------------------------------------------------------
# Adoption compares a column only under the launch that reads it
# ---------------------------------------------------------------------------
#
# One case per table row: a row whose kind and trigger read the column differs
# from the member and must be refused, and a row whose kind and trigger never
# consult it must be adopted though its stored value differs. Each case names the
# column it belongs to, so a table row without a case -- or a case without a row
# -- fails test_every_compared_column_has_a_case below. A case is
# (tag, member, row, expected): *member* is (target, trigger, extra_member) and
# *row* is (target, trigger, stored overrides).

_AT_PAST_EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
_CRON_ROW_FIELDS = {
    "trigger_type": "cron",
    "cron_expr": "0 2 * * *",
    "effective_timezone": "America/New_York",
}
_INTERVAL_ROW_FIELDS = {
    "trigger_type": "interval",
    "cron_expr": None,
    "effective_timezone": None,
    "interval_sec": 3600,
}
_AT_ROW_FIELDS = {
    "trigger_type": "at",
    "cron_expr": None,
    "effective_timezone": None,
    "next_fire_at": _AT_PAST_EPOCH,
    "max_runs": 1,
    "missed_fire_policy": "run_once",
}
_GITHUB_ROW_FIELDS = {
    "trigger_type": "github_poll",
    "cron_expr": None,
    "effective_timezone": None,
    "github_repo": "acme/widgets",
}
_TRIGGER_ROW_FIELDS = {
    "cron": _CRON_ROW_FIELDS,
    "interval": _INTERVAL_ROW_FIELDS,
    "at": _AT_ROW_FIELDS,
    # The same instant under the default missed-fire policy.
    "at_skip": {**_AT_ROW_FIELDS, "missed_fire_policy": "skip"},
    "github": _GITHUB_ROW_FIELDS,
}
_TRIGGER_BLOCKS = {
    "cron": _CRON_TRIGGER_BLOCK,
    "interval": "      every: 1h\n",
    "at": _AT_PAST_TRIGGER,
    "at_skip": _AT_PAST_TRIGGER,
    "github": _GITHUB_TRIGGER_BLOCK,
}
_FLOW_TARGET_BLOCK = "    target:\n      kind: flow\n      file: flows/nightly.yaml\n"
_TARGET_BLOCKS = {
    "agent": _AGENT_TARGET_BLOCK,
    "play": _PLAY_TARGET_BLOCK,
    "command": _COMMAND_TARGET_BLOCK,
    "flow": _FLOW_TARGET_BLOCK,
}
_TARGET_ROW_FIELDS = {
    "agent": {
        "action_kind": "agent",
        "action_model": "anthropic/claude-sonnet-5",
        "action_prompt": "check things",
        "action_agent": "reviewer",
        "action_playbook": None,
        "action_command": None,
        "action_flow_yaml": None,
    },
    "play": {
        "action_kind": "play",
        "action_model": None,
        "action_prompt": None,
        "action_agent": None,
        "action_playbook": "health-audit",
        "action_command": None,
        "action_flow_yaml": None,
    },
    "command": {
        "action_kind": "command",
        "action_model": None,
        "action_prompt": None,
        "action_agent": None,
        "action_playbook": None,
        "action_command": "refresh-index",
        "action_flow_yaml": None,
    },
    "flow": {
        "action_kind": "flow_yaml",
        "action_model": None,
        "action_prompt": None,
        "action_agent": None,
        "action_playbook": None,
        "action_command": None,
        "action_flow_yaml": "workers: 2\n",
    },
}


_AGENT_CRON = ("agent", "cron", "")
_AGENT_CRON_RUN_ONCE = ("agent", "cron", "    policies:\n      missedFire: run_once\n")
_AT_RUN_ONCE = ("agent", "at", "    policies:\n      missedFire: run_once\n")

_ADOPT_TABLE_CASES = {
    "trigger_type": [
        (
            "a stored trigger the member does not declare",
            _AGENT_CRON,
            ("agent", "interval", {}),
            "ERROR",
        ),
    ],
    "cron_expr": [
        (
            "a stale expression no cron reader sees",
            ("agent", "interval", ""),
            ("agent", "interval", {"cron_expr": "0 3 * * *"}),
            "ADOPT",
        ),
        (
            "an expression that fires at another time",
            _AGENT_CRON,
            ("agent", "cron", {"cron_expr": "0 3 * * *"}),
            "ERROR",
        ),
    ],
    "interval_sec": [
        (
            "a cadence a cron trigger never reads",
            _AGENT_CRON,
            ("agent", "cron", {"interval_sec": 3600}),
            "ADOPT",
        ),
        (
            "a cadence that fires on another period",
            ("agent", "interval", ""),
            ("agent", "interval", {"interval_sec": 7200}),
            "ERROR",
        ),
    ],
    "github_repo": [
        (
            "a repository a cron trigger never polls",
            _AGENT_CRON,
            ("agent", "cron", {"github_repo": "acme/old"}),
            "ADOPT",
        ),
        (
            "a repository that polls another place",
            ("agent", "github", ""),
            ("agent", "github", {"github_repo": "acme/other"}),
            "ERROR",
        ),
    ],
    "github_filter": [
        (
            "a filter a cron trigger never applies",
            _AGENT_CRON,
            ("agent", "cron", {"github_filter": {"state": "open"}}),
            "ADOPT",
        ),
        (
            "a filter that selects other events",
            ("agent", "github", ""),
            ("agent", "github", {"github_filter": {"state": "open"}}),
            "ERROR",
        ),
    ],
    "poll_interval_sec": [
        (
            "a poll interval a cron trigger never reads",
            _AGENT_CRON,
            ("agent", "cron", {"poll_interval_sec": 60}),
            "ADOPT",
        ),
        (
            "a poll interval that polls at another cadence",
            ("agent", "github", ""),
            ("agent", "github", {"poll_interval_sec": 60}),
            "ERROR",
        ),
    ],
    "enabled": [
        (
            "a disabled row no enabled-schedule read reaches",
            _AGENT_CRON,
            ("agent", "cron", {"enabled": 0}),
            "ERROR",
        ),
    ],
    "action_kind": [
        (
            "a launch kind no member target produces",
            _AGENT_CRON,
            ("agent", "cron", {"action_kind": "flow", "action_prompt": None, "action_agent": None}),
            "ERROR",
        ),
    ],
    "action_cwd": [
        (
            "a root the launch would spawn in another directory",
            _AGENT_CRON,
            ("agent", "cron", {"action_cwd": "REPLACED-ROOT"}),
            "ERROR",
        ),
    ],
    "action_project": [
        (
            "a project argument a play launch never passes on",
            ("play", "cron", ""),
            ("play", "cron", {"action_project": "other-project"}),
            "ADOPT",
        ),
        (
            "a project argument passed on as another project",
            _AGENT_CRON,
            ("agent", "cron", {"action_project": "other-project"}),
            "ERROR",
        ),
        (
            "a project the launch refuses whatever the kind",
            ("play", "cron", ""),
            ("play", "cron", {"action_project": "bad name"}),
            "ERROR",
        ),
    ],
    "budget_usd": [
        (
            "a spend cap no member declares",
            _AGENT_CRON,
            ("agent", "cron", {"budget_usd": 5.0}),
            "ERROR",
        ),
    ],
    "budget_tokens": [
        (
            "a token cap no member declares",
            _AGENT_CRON,
            ("agent", "cron", {"budget_tokens": 500}),
            "ERROR",
        ),
    ],
    "rate_limit": [
        (
            "a rolling cap no member declares",
            _AGENT_CRON,
            ("agent", "cron", {"rate_limit": {"max_fires": 1, "window_sec": 60}}),
            "ERROR",
        ),
    ],
    "notify_on": [
        (
            "an event with no command to run on it",
            _AGENT_CRON,
            ("agent", "cron", {"notify_on": ["failed"]}),
            "ADOPT",
        ),
        (
            "an event the member never registers",
            ("agent", "cron", '    notify:\n      on: [failed]\n      command: "echo done"\n'),
            ("agent", "cron", {"notify_on": ["completed"], "notify_command": "echo done"}),
            "ERROR",
        ),
    ],
    "notify_command": [
        (
            "a command with no event to run on",
            _AGENT_CRON,
            ("agent", "cron", {"notify_command": "echo done"}),
            "ADOPT",
        ),
        (
            "a command the member never runs",
            ("agent", "cron", '    notify:\n      on: [failed]\n      command: "echo done"\n'),
            ("agent", "cron", {"notify_on": ["failed"], "notify_command": "echo other"}),
            "ERROR",
        ),
    ],
    "action_agent": [
        (
            "a profile a play launch validates and drops",
            ("play", "cron", ""),
            ("play", "cron", {"action_agent": "reviewer"}),
            "ADOPT",
        ),
        (
            "a profile the agent launch passes on",
            _AGENT_CRON,
            ("agent", "cron", {"action_agent": "other"}),
            "ERROR",
        ),
    ],
    "action_model": [
        (
            "a model a play launch validates and drops",
            ("play", "cron", ""),
            ("play", "cron", {"action_model": "anthropic/claude-sonnet-5"}),
            "ADOPT",
        ),
        (
            "a model the agent launch passes on",
            _AGENT_CRON,
            ("agent", "cron", {"action_model": "anthropic/claude-opus-5"}),
            "ERROR",
        ),
        (
            "a model the launch refuses whatever the kind",
            ("play", "cron", ""),
            ("play", "cron", {"action_model": "-oops"}),
            "ERROR",
        ),
    ],
    "action_prompt": [
        (
            "a prompt a command launch validates and drops",
            ("command", "cron", ""),
            ("command", "cron", {"action_prompt": "hello"}),
            "ADOPT",
        ),
        (
            "a prompt the agent launch passes on",
            _AGENT_CRON,
            ("agent", "cron", {"action_prompt": "other things"}),
            "ERROR",
        ),
        (
            "a prompt the launch refuses whatever the kind",
            ("command", "cron", ""),
            ("command", "cron", {"action_prompt": "--"}),
            "ERROR",
        ),
        (
            "a leading-dash prompt a play launch takes as a flag",
            ("play", "cron", ""),
            ("play", "cron", {"action_prompt": "-oops"}),
            "ERROR",
        ),
    ],
    "action_playbook": [
        (
            "a playbook an agent launch never appends",
            _AGENT_CRON,
            ("agent", "cron", {"action_playbook": "health-audit"}),
            "ADOPT",
        ),
        (
            "a playbook the play launch runs instead",
            ("play", "cron", ""),
            ("play", "cron", {"action_playbook": "other-pb"}),
            "ERROR",
        ),
        (
            "a playbook the launch refuses whatever the kind",
            _AGENT_CRON,
            ("agent", "cron", {"action_playbook": "-oops"}),
            "ERROR",
        ),
    ],
    "action_command": [
        (
            "an executable an agent launch never spawns",
            _AGENT_CRON,
            ("agent", "cron", {"action_command": "refresh-index"}),
            "ADOPT",
        ),
        (
            "an executable a command launch spawns instead",
            ("command", "cron", ""),
            ("command", "cron", {"action_command": "other-cmd"}),
            "ERROR",
        ),
    ],
    "action_command_args": [
        (
            "arguments an agent launch never renders",
            _AGENT_CRON,
            ("agent", "cron", {"action_command_args": ["fast"]}),
            "ADOPT",
        ),
        (
            "arguments a command launch renders instead",
            ("command", "cron", ""),
            ("command", "cron", {"action_command_args": ["slow"]}),
            "ERROR",
        ),
    ],
    "action_flow_yaml": [
        (
            "a snapshot an agent launch never reads",
            _AGENT_CRON,
            ("agent", "cron", {"action_flow_yaml": "workers: 4\n"}),
            "ADOPT",
        ),
        (
            "a snapshot a flow launch runs instead",
            ("flow", "cron", ""),
            ("flow", "cron", {"action_flow_yaml": "workers: 4\n"}),
            "ERROR",
        ),
    ],
    "missed_fire_policy": [
        (
            "a missed-fire policy no missed-fire read reaches",
            ("agent", "github", ""),
            ("agent", "github", {"missed_fire_policy": "run_once"}),
            "ADOPT",
        ),
        (
            "a missed-fire policy the tick reads instead",
            _AGENT_CRON,
            ("agent", "cron", {"missed_fire_policy": "run_once"}),
            "ERROR",
        ),
    ],
    "overlap_policy": [
        (
            "an overlap policy no overlap read reaches",
            ("agent", "github", ""),
            ("agent", "github", {"overlap_policy": "allow"}),
            "ADOPT",
        ),
        (
            "an overlap policy the fire reads instead",
            _AGENT_CRON,
            ("agent", "cron", {"overlap_policy": "allow"}),
            "ERROR",
        ),
    ],
    "max_runs": [
        (
            "a run cap the admission reads",
            _AGENT_CRON,
            ("agent", "cron", {"max_runs": 3}),
            "ERROR",
        ),
        (
            "no run cap on a one-shot whose declaration always writes one",
            _AT_RUN_ONCE,
            ("agent", "at", {"max_runs": None}),
            "ERROR",
        ),
        (
            "no run cap on a skipped one-shot",
            ("agent", "at_skip", ""),
            ("agent", "at_skip", {"max_runs": None}),
            "ERROR",
        ),
    ],
    "next_fire_at": [
        (
            "the declared instant a one-shot holds, fired or not",
            _AT_RUN_ONCE,
            ("agent", "at", {}),
            "ADOPT",
        ),
        (
            "the declared instant on a one-shot whose missed fire is skipped",
            ("agent", "at_skip", ""),
            ("agent", "at_skip", {}),
            "ADOPT",
        ),
        (
            "the declared instant on a one-shot that already fired",
            _AT_RUN_ONCE,
            ("agent", "at", {"next_fire_at": None, "last_fired_at": 1.0}),
            "ADOPT",
        ),
        (
            "another instant the one-shot would fire at",
            _AT_RUN_ONCE,
            ("agent", "at", {"next_fire_at": _AT_PAST_EPOCH + 1000.0}),
            "ERROR",
        ),
        (
            "no instant at all on a one-shot the scheduler would still run",
            _AT_RUN_ONCE,
            ("agent", "at", {"next_fire_at": None}),
            "ERROR",
        ),
        (
            "no instant at all on a skipped one-shot, whose write keeps its null",
            ("agent", "at_skip", ""),
            ("agent", "at_skip", {"next_fire_at": None}),
            "ADOPT",
        ),
    ],
    "resolved_timezone": [
        (
            "a zone no interval fire time is read in",
            ("agent", "interval", ""),
            ("agent", "interval", {"resolved_timezone": "America/New_York"}),
            "ADOPT",
        ),
        (
            "a zone another cron expression is read in",
            _AGENT_CRON,
            ("agent", "cron", {"resolved_timezone": "Europe/Berlin"}),
            "ERROR",
        ),
    ],
    "description": [
        (
            "a description no launch reads",
            _AGENT_CRON,
            ("agent", "cron", {"description": "new"}),
            "ADOPT",
        ),
    ],
    "on_success": [
        (
            "a chained success action no declaration writes",
            _AGENT_CRON,
            ("agent", "cron", {"on_success": {"kind": "agent", "agent": "publisher"}}),
            "ERROR",
        ),
    ],
    "on_fail": [
        (
            "a chained failure action no declaration writes",
            _AGENT_CRON,
            ("agent", "cron", {"on_fail": {"kind": "agent", "agent": "pager"}}),
            "ERROR",
        ),
    ],
    "action_extra_args": [
        (
            "extra positionals no declaration writes",
            _AGENT_CRON,
            ("agent", "cron", {"action_extra_args": ["extra"]}),
            "ERROR",
        ),
    ],
    "threshold_config": [
        (
            "a metric threshold the fire path evaluates",
            _AGENT_CRON,
            (
                "agent",
                "cron",
                {"threshold_config": {"metric": "failed_sessions", "op": "gt", "value": 3}},
            ),
            "ERROR",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Every column names, per kind and trigger, the launches whose path reads it
# ---------------------------------------------------------------------------
#
# The cases above are hand-written, one per table row. These walks are derived: for
# every column carrying an action-kind set and every kind a member target can
# produce, one case builds a row of that kind differing in that one column, and the
# same for every trigger the table distinguishes. What each case expects comes from
# the ``_PINNED_*`` maps below -- independent copies of the table's kind and trigger
# sets -- so a kind or trigger dropped from a table entry leaves the case in place
# and turns it red instead of removing the only case that would have caught it.

# The row overrides that store a different value than the member declares, per
# column. Every value is one the launch accepts, so a kind or trigger whose launch
# reads no part of the column has nothing to differ in, and one whose launch reads
# it has a real difference to report. The kind walk and the trigger walk each carry
# only the columns their axis varies.
# An independent copy of the kinds the table names for each column the kind walk
# varies.
_PINNED_ACTION_KINDS = {
    "action_project": {"agent", "flow", "fanout", "flow_yaml"},
    "action_agent": {"agent", "engine"},
    "action_model": {"agent", "flow", "fanout", "flow_yaml", "engine"},
    "action_prompt": {"agent", "flow", "fanout", "play", "engine"},
    "action_playbook": {"play"},
}
# An independent copy of the triggers the table names for each column the trigger
# walk varies.
_PINNED_READING_TRIGGERS = {
    "cron_expr": {"cron"},
    "resolved_timezone": {"cron"},
    "interval_sec": {"interval", "github_poll"},
    "github_repo": {"github_poll"},
    "github_filter": {"github_poll"},
    "poll_interval_sec": {"github_poll"},
    "missed_fire_policy": {"cron", "interval", "at"},
    "overlap_policy": {"cron", "interval", "at"},
}
# The kinds a member target can produce, and the member/row target block for each.
# The trigger types the table distinguishes, and for each the member manifest block
# and the stored row's trigger. The one-shot row is stored under the default
# missed-fire policy, so that policy is not the difference a one-shot case is about.


# ---------------------------------------------------------------------------
# A row whose own target column names nothing is undeclarable
# ---------------------------------------------------------------------------
#
# Every member target resolves the column its launch runs by: an agent's profile,
# a playbook's name, a command's executable, a flow's snapshot. None of them
# resolves an empty one, so a row that stores none of its own column launches
# nothing there and no member can be edited to match it.

_MISSING_OWN_COLUMN_CASES = {
    "agent": (_AGENT_TARGET_BLOCK, {"action_agent": None}),
    "play": (
        _PLAY_TARGET_BLOCK,
        {
            "action_kind": "play",
            "action_agent": None,
            "action_model": None,
            "action_prompt": None,
            "action_playbook": None,
        },
    ),
    "command": (
        _COMMAND_TARGET_BLOCK,
        {
            "action_kind": "command",
            "action_agent": None,
            "action_model": None,
            "action_prompt": None,
            "action_command": None,
        },
    ),
    "flow": (
        _FLOW_TARGET_BLOCK,
        {
            "action_kind": "flow_yaml",
            "action_agent": None,
            "action_model": None,
            "action_prompt": None,
            "action_flow_yaml": None,
        },
    ),
}


# The plan-level walk above can only store a kind a member target produces, so the
# kinds a member never produces -- flow, fanout and engine -- are pinned here on the
# comparison itself: a launch of such a kind acts on a value exactly when the table
# names that kind for the column.

# One value the launch accepts for each column the comparison reads by kind.
_COMPARABLE_VALUES = {
    "action_agent": "reviewer",
    "action_model": "anthropic/claude-sonnet-5",
    "action_prompt": "check things",
    "action_playbook": "health-audit",
}
# Every launch kind, member-producible or not.
_ALL_LAUNCH_KINDS = ("agent", "command", "engine", "fanout", "flow", "flow_yaml", "play")

_COMPARABLE_KIND_CASES = [
    (column, kind) for column in sorted(_COMPARABLE_VALUES) for kind in _ALL_LAUNCH_KINDS
]


def test_the_comparison_walk_covers_every_kind_a_launch_can_carry():
    """The walk varies every launch kind, and the table's kind sets name only ones
    the walk covers -- so a kind the table names cannot be added without the walk
    varying it."""
    from lionagi.studio.scheduler.subprocess import (
        _ALIAS_ACTION_KINDS,
        _VALID_ACTION_KINDS,
    )

    assert set(_ALL_LAUNCH_KINDS) >= _VALID_ACTION_KINDS
    # A legacy spelling is canonicalized before the comparison reads it, so its
    # target is what the walk must cover -- not the spelling itself.
    assert set(_ALIAS_ACTION_KINDS.values()) <= set(_ALL_LAUNCH_KINDS)
    for spelling, canonical in _ALIAS_ACTION_KINDS.items():
        for column in _COMPARABLE_VALUES:
            assert _adopt_comparable(
                column, {"action_kind": spelling}, _COMPARABLE_VALUES[column]
            ) == _adopt_comparable(column, {"action_kind": canonical}, _COMPARABLE_VALUES[column])
    named = set().union(*_PINNED_ACTION_KINDS.values())
    assert named <= set(_ALL_LAUNCH_KINDS)

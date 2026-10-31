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

from pydantic import ValidationError

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.state.db import StateDB
from lionagi.studio.services.schedule_declaration import (
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


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def agent_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal .lionagi/agents/reviewer.md discoverable via cwd.

    lionagi.cli._providers imports find_lionagi_dirs by value at module load
    time, so a global home directory that happens to declare its own
    same-named profiles (e.g. a real ~/.lionagi/agents/reviewer.md) can
    shadow-merge with this fixture's directory. Pin the module's resolved
    dirs list directly so this test only ever sees its own tmp_path.
    """
    import lionagi.cli._providers as providers_mod

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
    monkeypatch.setattr("lionagi.studio.config.SCHEDULER_TZ", "America/New_York")
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


# Closed-schema rejection at every level


def test_top_level_unknown_key_rejected():
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(
            {
                "apiVersion": "lionagi.io/v1alpha1",
                "kind": "ScheduleSet",
                "metadata": {"name": "a", "project": "demo"},
                "schedules": {},
                "unexpectedTopLevel": True,
            }
        )


def test_metadata_unknown_key_rejected():
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(
            {
                "apiVersion": "lionagi.io/v1alpha1",
                "kind": "ScheduleSet",
                "metadata": {"name": "a", "project": "demo", "surprise": 1},
                "schedules": {},
            }
        )


def test_trigger_requires_exactly_one_kind():
    base = {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {
                    "cron": {"expression": "0 * * * *", "timezone": "UTC"},
                    "every": "1h",
                },
                "target": {"kind": "command", "executable": "x"},
            }
        },
    }
    with pytest.raises(ValidationError, match="exactly one"):
        ScheduleSetDocument.model_validate(base)


def test_trigger_zero_kinds_rejected():
    base = {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {},
                "target": {"kind": "command", "executable": "x"},
            }
        },
    }
    with pytest.raises(ValidationError, match="exactly one"):
        ScheduleSetDocument.model_validate(base)


def test_target_unknown_key_rejected():
    base = {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {"every": "1h"},
                "target": {"kind": "command", "executable": "x", "shell": "rm -rf /"},
            }
        },
    }
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(base)


def test_target_unknown_kind_rejected():
    base = {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {"every": "1h"},
                "target": {"kind": "webhook", "url": "http://x"},
            }
        },
    }
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(base)


def test_policies_unknown_key_rejected():
    base = {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {"every": "1h"},
                "target": {"kind": "command", "executable": "x"},
                "policies": {"retries": 3},
            }
        },
    }
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(base)


@pytest.mark.parametrize("chain_field", ["on_success", "on_fail", "after", "depends_on"])
def test_member_level_chain_fields_rejected(chain_field):
    """Dependencies live in the flow layer -- a v1 member schema has no
    chain/dependency fields at all, so any attempt to declare one is a
    closed-schema (unknown key) rejection."""
    base = {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {"every": "1h"},
                "target": {"kind": "command", "executable": "x"},
                chain_field: {"prompt": "notify"},
            }
        },
    }
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(base)


def _notify_manifest(notify: dict) -> dict:
    return {
        "apiVersion": "lionagi.io/v1alpha1",
        "kind": "ScheduleSet",
        "metadata": {"name": "a", "project": "demo"},
        "schedules": {
            "m": {
                "trigger": {"every": "1h"},
                "target": {"kind": "command", "executable": "x"},
                "notify": notify,
            }
        },
    }


def test_notify_unknown_status_rejected():
    base = _notify_manifest({"on": ["not_a_real_status"], "command": "notify-run"})
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(base)


def test_notify_empty_on_rejected():
    base = _notify_manifest({"on": [], "command": "notify-run"})
    with pytest.raises(ValidationError, match="notify.on"):
        ScheduleSetDocument.model_validate(base)


def test_notify_empty_command_rejected():
    base = _notify_manifest({"on": ["failed"], "command": "   "})
    with pytest.raises(ValidationError, match="notify.command"):
        ScheduleSetDocument.model_validate(base)


def test_notify_duplicate_status_rejected():
    base = _notify_manifest({"on": ["failed", "failed"], "command": "notify-run"})
    with pytest.raises(ValidationError, match="duplicate"):
        ScheduleSetDocument.model_validate(base)


def test_notify_extra_key_rejected():
    base = _notify_manifest({"on": ["failed"], "command": "notify-run", "bogus": 1})
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(base)


def test_notify_valid_accepted():
    base = _notify_manifest(
        {"on": ["failed", "timed_out"], "command": "notify-run --payload {payload}"}
    )
    doc = ScheduleSetDocument.model_validate(base)
    assert doc.schedules["m"].notify.on == ["failed", "timed_out"]


def _notify_yaml_manifest(notify_body: str) -> str:
    return f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: a
  project: demo
schedules:
  m:
    trigger:
      every: 1h
    target:
      kind: command
      executable: notify-run
    notify:
{notify_body}
"""


def test_notify_bare_on_key_parses_as_string_not_bool():
    """PyYAML's SafeLoader resolves a bare `on` key to the bool True (YAML
    1.1 implicit booleans). A hand-authored notify block with an unquoted
    `on:` must still parse and validate, not fail extra="forbid" naming
    `True`."""
    manifest = _notify_yaml_manifest("      on: [failed]\n      command: notify-run\n")
    doc = parse_schedule_set(manifest)
    assert doc.schedules["m"].notify.on == ["failed"]


def test_notify_quoted_on_key_still_works():
    manifest = _notify_yaml_manifest('      "on": [failed]\n      command: notify-run\n')
    doc = parse_schedule_set(manifest)
    assert doc.schedules["m"].notify.on == ["failed"]


def test_notify_genuinely_unknown_key_still_rejected():
    manifest = _notify_yaml_manifest(
        "      on: [failed]\n      command: notify-run\n      bogus: 1\n"
    )
    with pytest.raises(ValidationError, match="bogus"):
        parse_schedule_set(manifest)


def test_notify_explicit_bool_tagged_on_key_still_rejected():
    """The bare/implicit `on:` leniency is scoped to YAML 1.1's *implicit*
    bool resolution. A key explicitly tagged `!!bool on:` is an author
    asking for a real bool key on purpose, so it must still construct as
    `True` and trip the closed schema, not silently parse as the string
    "on"."""
    manifest = _notify_yaml_manifest("      !!bool on: [failed]\n      command: notify-run\n")
    with pytest.raises(ValidationError, match="True"):
        parse_schedule_set(manifest)


def test_notify_anchor_then_explicit_bool_tagged_key_still_rejected():
    """YAML node properties may appear in either order; `&a !!bool on:` is
    just as explicit as `!!bool on:` and must also construct as `True` and
    trip the closed schema."""
    manifest = _notify_yaml_manifest("      &a !!bool on: [failed]\n      command: notify-run\n")
    with pytest.raises(ValidationError, match="True"):
        parse_schedule_set(manifest)


def test_notify_alias_of_implicit_anchored_key_stays_text():
    """An anchored-but-untagged key (`&a on:`) is still implicit resolution,
    so it keeps the text leniency; an alias (`*a`) reusing it in a second
    schedule's notify block inherits the same node and therefore the same
    outcome."""
    manifest = """
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: a
  project: demo
schedules:
  m:
    trigger:
      every: 1h
    target:
      kind: command
      executable: notify-run
    notify:
      &a on: [failed]
      command: notify-run
  n:
    trigger:
      every: 1h
    target:
      kind: command
      executable: notify-run
    notify:
      *a : [timed_out]
      command: notify-run
"""
    doc = parse_schedule_set(manifest)
    assert doc.schedules["m"].notify.on == ["failed"]
    assert doc.schedules["n"].notify.on == ["timed_out"]


def test_sequence_mapping_key_raises_value_error_not_type_error():
    """A sequence used as a mapping key is unhashable. SafeLoader rejects
    this with ConstructorError; the custom key-construction override must
    preserve that check instead of raising an uncaught TypeError."""
    with pytest.raises(ValueError, match="invalid.yaml"):
        parse_schedule_set("[a, b]: value\n", source="invalid.yaml")


def _policies_manifest(policies_yaml: str, cwd: Path) -> str:
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
    execution:
      cwd: {cwd}
    policies:
{policies_yaml}
"""


def test_policies_rate_limit_malformed_rejected(tmp_path):
    """Policies.rateLimit is validated with the same
    lionagi.studio.scheduler.admit.validate_rate_limit the fire-time engine
    admission path uses -- an open dict[str, Any] would otherwise let a
    malformed rateLimit commit."""
    manifest = _policies_manifest(
        "      rateLimit:\n        max_fires: 3\n        # missing window_sec\n", tmp_path
    )
    with pytest.raises(ValidationError, match="rateLimit"):
        parse_schedule_set(manifest)


def test_policies_rate_limit_valid_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    manifest = _policies_manifest(
        "      rateLimit:\n        max_fires: 3\n        window_sec: 60\n", tmp_path
    )
    doc = parse_schedule_set(manifest)
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["m"].db_fields["rate_limit"] == {"max_fires": 3, "window_sec": 60}


@pytest.mark.parametrize("bad_usd", [".inf", ".nan", "-.inf"])
def test_policies_budget_non_finite_usd_rejected(tmp_path, bad_usd):
    manifest = _policies_manifest(f"      budget:\n        usd: {bad_usd}\n", tmp_path)
    with pytest.raises(ValidationError, match="budget"):
        parse_schedule_set(manifest)


def test_policies_budget_non_int_tokens_rejected(tmp_path):
    manifest = _policies_manifest("      budget:\n        tokens: 5.5\n", tmp_path)
    with pytest.raises(ValidationError):
        parse_schedule_set(manifest)


def test_policies_budget_valid_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    manifest = _policies_manifest(
        "      budget:\n        usd: 5.0\n        tokens: 1000\n", tmp_path
    )
    doc = parse_schedule_set(manifest)
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["m"].db_fields["budget_usd"] == 5.0
    assert resolved["m"].db_fields["budget_tokens"] == 1000


def test_wrong_api_version_rejected():
    with pytest.raises(ValidationError):
        ScheduleSetDocument.model_validate(
            {
                "apiVersion": "lionagi.io/v2",
                "kind": "ScheduleSet",
                "metadata": {"name": "a", "project": "demo"},
                "schedules": {},
            }
        )


# Trigger resolution


@pytest.mark.parametrize(
    ("project_line", "expected"),
    [
        ("", "demo"),
        ("      project: other", "other"),
        ("      project: null", None),
        ("      project: ~", None),
        ("      project:", None),
        ("      project: ''", "demo"),
    ],
    ids=[
        "omitted-inherits",
        "named",
        "explicit-null-is-none",
        "tilde-is-none",
        "blank-is-none",
        "empty-inherits",
    ],
)
def test_a_member_project_inherits_the_document_unless_named_or_null(
    agent_profile, project_line, expected
):
    """A project written as YAML null is a project of None, never the document's.

    ``null``, ``~`` and a blank value parse alike, so all three declare no project.

    It is what an export writes for a row stored without a project, and the project
    reaches the spawned command, so inheriting there would change the row on re-apply.
    The qualified name keeps the document's namespace either way.
    """
    doc = parse_schedule_set(
        _agent_manifest("nightly", cwd=agent_profile, extra_member=project_line)
    )
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    assert resolved.qualified_name == "demo/nightly"
    assert resolved.db_fields["action_project"] == expected
    assert resolved.resolved["execution"]["project"] == expected


@pytest.mark.parametrize(
    ("project_line", "expected_stored"),
    [
        ("", "absent"),
        ("      project: null", None),
    ],
    ids=["omitted-stays-absent", "null-stays-null"],
)
def test_stored_authored_spec_preserves_an_omitted_project(
    agent_profile, project_line, expected_stored
):
    """The stored ``authored_spec`` must keep an omitted project omitted.

    A null project read on its own declares no project, so dumping an inherited
    project as ``project: null`` would store a different schedule than the one
    applied: re-reading that spec beside a project-scoped document would drop
    the project instead of inheriting it.
    """
    doc = parse_schedule_set(
        _agent_manifest("nightly", cwd=agent_profile, extra_member=project_line)
    )
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    execution = resolved.authored["execution"]
    reread = ScheduleMember.model_validate(resolved.authored)
    if expected_stored == "absent":
        assert "project" not in execution
        assert "project" not in reread.execution.model_fields_set
    else:
        assert execution["project"] is None
        assert "project" in reread.execution.model_fields_set


def test_resolve_cron_trigger_persists_timezone(agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile)
    doc = parse_schedule_set(text)
    resolved = resolve_schedule_set(doc, agent_profile)
    r = resolved["nightly"]
    assert r.resolved["trigger"] == {
        "kind": "cron",
        "expression": "0 2 * * *",
        "timezone": "America/New_York",
    }
    assert r.timezone == "America/New_York"


def test_resolve_cron_invalid_expression_rejected(agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile).replace('"0 2 * * *"', '"not a cron"')
    doc = parse_schedule_set(text)
    with pytest.raises(ScheduleSetError, match="cron"):
        resolve_schedule_set(doc, agent_profile)


def test_resolve_cron_invalid_timezone_rejected(agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile).replace("America/New_York", "Not/AZone")
    doc = parse_schedule_set(text)
    with pytest.raises(ScheduleSetError, match="timezone"):
        resolve_schedule_set(doc, agent_profile)


def _every_manifest(value: str, cwd: Path) -> str:
    return f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  hourly:
    trigger:
      every: "{value}"
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: {cwd}
"""


def test_resolve_every_trigger(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_every_manifest("1h", tmp_path))
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["hourly"].resolved["trigger"] == {
        "kind": "every",
        "interval_sec": 3600,
        "raw": "1h",
    }


@pytest.mark.parametrize("bad", ["0s", "-5m", "abc", "5", "40d"])
def test_resolve_every_trigger_rejects_bad_values(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_every_manifest(bad, tmp_path))
    with pytest.raises(ScheduleSetError):
        resolve_schedule_set(doc, tmp_path)


def _at_manifest(value: str, cwd: Path) -> str:
    return f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  once:
    trigger:
      at: "{value}"
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: {cwd}
"""


def test_resolve_at_trigger_requires_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15T09:00:00-04:00", tmp_path))
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["once"].resolved["trigger"]["kind"] == "at"


def test_resolve_at_trigger_rejects_missing_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15T09:00:00", tmp_path))
    with pytest.raises(ScheduleSetError, match="offset"):
        resolve_schedule_set(doc, tmp_path)


def test_resolve_at_trigger_accepts_z_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15T09:00:00Z", tmp_path))
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["once"].resolved["trigger"]["kind"] == "at"


def test_resolve_at_trigger_rejects_space_separated_timestamp(tmp_path, monkeypatch):
    """RFC 3339 mandates a 'T' date/time separator; fromisoformat() alone
    would also accept a space, a non-conformant variant."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15 09:00:00+00:00", tmp_path))
    with pytest.raises(ScheduleSetError, match="'T'"):
        resolve_schedule_set(doc, tmp_path)


def test_resolve_at_trigger_sets_next_fire_epoch_and_forces_max_runs_one(tmp_path, monkeypatch):
    """The apply path must persist the resolved at-instant as next_fire_at
    (so the row is due exactly once) and force max_runs=1 -- an 'at' member
    is implicitly max one run, enforced via the existing claim-before-fire
    gate rather than a bespoke history check."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15T09:00:00Z", tmp_path))
    resolved = resolve_schedule_set(doc, tmp_path)
    member = resolved["once"]
    assert member.db_fields["trigger_type"] == "at"
    assert member.db_fields["max_runs"] == 1
    from datetime import datetime, timezone

    expected = datetime(2026, 7, 15, 9, 0, 0, tzinfo=timezone.utc).timestamp()
    assert member.db_fields["next_fire_at"] == pytest.approx(expected)


def _github_manifest(cwd: Path, *, repo: str = "acme/widgets") -> str:
    return f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  on-pr:
    trigger:
      github:
        repo: {repo}
        filter:
          state: open
          base: main
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: {cwd}
"""


def test_resolve_github_trigger_reuses_service_validators(tmp_path, monkeypatch):
    """The github trigger must mirror the existing service-boundary checks,
    not fork them -- verified here by monkeypatching the *service* functions
    schedule_declaration imports and asserting they're actually called."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    calls = []
    import lionagi.studio.services._schedule_validation as svc

    monkeypatch.setattr(svc, "_svc_validate_github_repo", lambda repo: calls.append(("repo", repo)))
    monkeypatch.setattr(svc, "_svc_validate_github_filter", lambda f: calls.append(("filter", f)))
    doc = parse_schedule_set(_github_manifest(tmp_path))
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["on-pr"].resolved["trigger"]["repo"] == "acme/widgets"
    assert ("repo", "acme/widgets") in calls
    assert any(c[0] == "filter" for c in calls)


def test_resolve_github_trigger_rejects_invalid_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_github_manifest(tmp_path, repo="../../etc/passwd"))
    with pytest.raises(ScheduleSetError):
        resolve_schedule_set(doc, tmp_path)


# Target resolution


def test_agent_target_resolves_model_from_profile(agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    resolved = resolve_schedule_set(doc, agent_profile)
    assert resolved["nightly"].resolved["target"]["model"] == "anthropic/claude-sonnet-5"


def test_agent_target_missing_profile_rejected(agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile).replace("reviewer", "ghost-profile")
    doc = parse_schedule_set(text)
    with pytest.raises(ScheduleSetError, match="does not exist"):
        resolve_schedule_set(doc, agent_profile)


def test_agent_target_no_model_anywhere_rejected(agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile).replace("reviewer", "no-model")
    doc = parse_schedule_set(text)
    with pytest.raises(ScheduleSetError, match="model"):
        resolve_schedule_set(doc, agent_profile)


def test_agent_target_explicit_model_override_wins(agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile).replace(
        "profile: reviewer", "profile: reviewer\n      model: openai/gpt-4.1-mini"
    )
    doc = parse_schedule_set(text)
    resolved = resolve_schedule_set(doc, agent_profile)
    assert resolved["nightly"].resolved["target"]["model"] == "openai/gpt-4.1-mini"


def test_command_target_requires_allowlist(tmp_path, monkeypatch):
    """Matched on the word "allow" until the refusal stopped saying "add it to the
    allow-list", which was the sentence that made an env-var problem read as a
    document problem. The env variable's own name is the durable thing to match:
    it is what the reader has to act on, and it names the surface, not the prose.
    """
    monkeypatch.delenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", raising=False)
    doc = parse_schedule_set(_every_manifest("1h", tmp_path))
    with pytest.raises(ScheduleSetError, match="LIONAGI_SCHEDULER_COMMAND_ALLOWLIST") as exc:
        resolve_schedule_set(doc, tmp_path)
    # This path runs in the author's shell, never in the daemon, so the refusal it
    # raises is the one that has to say whose environment it read.
    assert "environment of this process" in str(exc.value)


def test_command_target_rejects_shell_style_string(tmp_path):
    with pytest.raises(ValidationError):
        parse_schedule_set(
            f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  x:
    trigger:
      every: 1h
    target:
      kind: command
      command: "rm -rf /"
    execution:
      cwd: {tmp_path}
"""
        )


def test_flow_target_relative_file_resolves_against_manifest_dir(tmp_path, monkeypatch):
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    flows_dir = manifest_dir / "flows"
    flows_dir.mkdir()
    (flows_dir / "nightly.yaml").write_text("workers: 2\n")

    doc = parse_schedule_set(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly:
    trigger:
      every: 1h
    target:
      kind: flow
      file: flows/nightly.yaml
    execution:
      cwd: {tmp_path}
"""
    )
    resolved = resolve_schedule_set(doc, manifest_dir)
    member = resolved["nightly"]
    target = member.resolved["target"]
    assert target["file"] == str((flows_dir / "nightly.yaml").resolve())
    assert "content_digest" in target
    # The flow launch path must route through flow_yaml with the
    # validated snapshot captured at resolution time, never bare 'flow'
    # (which would build `li o flow -- <model> <prompt>` positionals that
    # have nothing to do with a target.flow file).
    assert member.db_fields["action_kind"] == "flow_yaml"
    assert member.db_fields["action_flow_yaml"] == "workers: 2\n"


def test_flow_target_with_inputs_rejected(tmp_path):
    """flow_yaml launches take no positionals and reject extra args -- there
    is no field to merge a separate 'inputs' mapping into. Fail closed at
    declaration time rather than silently dropping it at fire time."""
    (tmp_path / "nightly.yaml").write_text("workers: 2\n")
    doc = parse_schedule_set(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly:
    trigger:
      every: 1h
    target:
      kind: flow
      file: nightly.yaml
      inputs:
        key: value
    execution:
      cwd: {tmp_path}
"""
    )
    with pytest.raises(ScheduleSetError, match="inputs"):
        resolve_schedule_set(doc, tmp_path)


def test_flow_target_missing_file_rejected(tmp_path):
    doc = parse_schedule_set(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly:
    trigger:
      every: 1h
    target:
      kind: flow
      file: does-not-exist.yaml
    execution:
      cwd: {tmp_path}
"""
    )
    with pytest.raises(ScheduleSetError, match="not found"):
        resolve_schedule_set(doc, tmp_path)


def test_flow_target_invalid_spec_rejected(tmp_path):
    (tmp_path / "bad.yaml").write_text("workers: 999\n")
    doc = parse_schedule_set(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  nightly:
    trigger:
      every: 1h
    target:
      kind: flow
      file: bad.yaml
    execution:
      cwd: {tmp_path}
"""
    )
    with pytest.raises(ScheduleSetError):
        resolve_schedule_set(doc, tmp_path)


def test_playbook_target_resolves(tmp_path):
    doc = parse_schedule_set(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  audit:
    trigger:
      every: 6h
    target:
      kind: playbook
      name: health-audit
      args:
        scope: lionagi
    execution:
      cwd: {tmp_path}
"""
    )
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["audit"].resolved["target"] == {
        "kind": "playbook",
        "name": "health-audit",
        "args": {"scope": "lionagi"},
    }


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


def test_a_playbook_target_arg_the_launch_would_refuse_is_refused_at_declaration(agent_profile):
    """A playbook target's args are held to the launch's rules when the set is
    resolved, so validate and apply report a bad arg instead of the row failing
    each time it fires."""
    target = (
        "    target:\n      kind: playbook\n      name: health-audit\n      args:\n"
        '        mode: "-deep"\n'
    )
    doc = parse_schedule_set(_manifest_with_target("nightly", cwd=agent_profile, target=target))
    with pytest.raises(ScheduleSetError) as excinfo:
        resolve_schedule_set(doc, agent_profile)
    ((name, message),) = excinfo.value.errors
    assert name == "demo/nightly"
    assert "starts with '-'" in message


@pytest.mark.parametrize("subcommand", ["list", "check", "status"])
def test_a_playbook_target_named_for_a_play_subcommand_is_refused_at_declaration(
    agent_profile, subcommand
):
    # `li play list` runs the listing, so the row would never run a playbook.
    target = f"    target:\n      kind: playbook\n      name: {subcommand}\n"
    doc = parse_schedule_set(_manifest_with_target("nightly", cwd=agent_profile, target=target))
    with pytest.raises(ScheduleSetError) as excinfo:
        resolve_schedule_set(doc, agent_profile)
    ((name, message),) = excinfo.value.errors
    assert name == "demo/nightly"
    assert "names a `li play` subcommand" in message


@pytest.mark.parametrize(
    ("row", "current", "undeclarable"),
    [
        ({"action_kind": "play"}, "list", True),
        ({"action_kind": "playbook"}, "status", True),
        ({"action_kind": "play"}, "health-audit", False),
        # Only a play launch reads the playbook name.
        ({"action_kind": "agent"}, "check", False),
    ],
)
@pytest.mark.asyncio
async def test_a_stored_playbook_named_for_a_play_subcommand_is_undeclarable(
    row, current, undeclarable
):
    assert (
        await _adopt_undeclarable(row, "action_playbook", current, is_global=False)
    ) is undeclarable


# cwd / project resolution


def test_relative_cwd_resolves_against_manifest_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    manifest_dir = tmp_path / "manifests"
    target_dir = tmp_path / "workdir"
    manifest_dir.mkdir()
    target_dir.mkdir()
    doc = parse_schedule_set(
        """
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  hourly:
    trigger:
      every: 1h
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: ../workdir
"""
    )
    resolved = resolve_schedule_set(doc, manifest_dir)
    assert resolved["hourly"].cwd == str(target_dir.resolve())


def test_global_scope_requires_explicit_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(
        """
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
  scope: global
schedules:
  hourly:
    trigger:
      every: 1h
    target:
      kind: command
      executable: refresh-index
"""
    )
    with pytest.raises(ScheduleSetError, match="global"):
        resolve_schedule_set(doc, tmp_path)


def test_global_scope_with_explicit_cwd_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(
        f"""
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
  scope: global
schedules:
  hourly:
    trigger:
      every: 1h
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: {tmp_path}
"""
    )
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["hourly"].qualified_name == "global/hourly"


def test_project_scope_defaults_to_git_root(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    manifest_dir = tmp_path / "sub" / "dir"
    manifest_dir.mkdir(parents=True)
    doc = parse_schedule_set(
        """
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: automation
  project: demo
schedules:
  hourly:
    trigger:
      every: 1h
    target:
      kind: command
      executable: refresh-index
"""
    )
    resolved = resolve_schedule_set(doc, manifest_dir)
    assert resolved["hourly"].cwd == str(tmp_path.resolve())


# Atomic validate/diff/apply service


@pytest.mark.asyncio
async def test_apply_is_idempotent_on_double_apply(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        result1 = await apply_schedule_set(db, doc, agent_profile)
        assert (result1.created, result1.updated, result1.unchanged, result1.disabled) == (
            1,
            0,
            0,
            0,
        )

        result2 = await apply_schedule_set(db, doc, agent_profile)
        assert (result2.created, result2.updated, result2.unchanged, result2.disabled) == (
            0,
            0,
            1,
            0,
        )

        row = await db.get_schedule_by_name("demo/nightly")
        assert row["owner_key"] == "demo/automation"
        assert row["spec_version"] == "lionagi.io/v1alpha1"
        assert row["managed_by"] == "declaration"


@pytest.mark.asyncio
async def test_get_schedule_decodes_authored_spec_and_resolved_target(temp_db_path, agent_profile):
    """The JSON columns written by apply must come back as decoded objects
    from every schedule getter, consistent with the rest of the row shape."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await apply_schedule_set(db, doc, agent_profile)
        by_name = await db.get_schedule_by_name("demo/nightly")
        assert isinstance(by_name["authored_spec"], dict)
        assert isinstance(by_name["resolved_target"], dict)
        by_id = await db.get_schedule(by_name["id"])
        assert isinstance(by_id["authored_spec"], dict)
        assert isinstance(by_id["resolved_target"], dict)


@pytest.mark.asyncio
async def test_apply_updates_in_place_preserving_id(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        result1 = await apply_schedule_set(db, doc, agent_profile)
        row1 = await db.get_schedule_by_name("demo/nightly")

        changed_text = _agent_manifest("nightly", cwd=agent_profile).replace(
            "check things", "check other things"
        )
        doc2 = parse_schedule_set(changed_text)
        result2 = await apply_schedule_set(db, doc2, agent_profile)
        assert (result2.created, result2.updated) == (0, 1)

        row2 = await db.get_schedule_by_name("demo/nightly")
        assert row2["id"] == row1["id"]
        assert row2["action_prompt"] == "check other things"


@pytest.mark.asyncio
async def test_apply_persists_notify_and_digest_reacts_to_it(temp_db_path, agent_profile):
    no_notify = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await apply_schedule_set(db, no_notify, agent_profile)
        row1 = await db.get_schedule_by_name("demo/nightly")
        assert row1["notify_on"] is None
        assert row1["notify_command"] is None

        with_notify = parse_schedule_set(
            _agent_manifest(
                "nightly",
                cwd=agent_profile,
                extra_member='    notify:\n      "on": [failed, timed_out]\n      command: notify-run --payload {payload}\n',
            )
        )
        result2 = await apply_schedule_set(db, with_notify, agent_profile)
        assert (result2.created, result2.updated, result2.unchanged) == (0, 1, 0)
        row2 = await db.get_schedule_by_name("demo/nightly")
        assert row2["id"] == row1["id"]
        assert row2["notify_on"] == ["failed", "timed_out"]
        assert row2["notify_command"] == "notify-run --payload {payload}"

        # Re-applying the identical notify-bearing doc is UNCHANGED.
        result3 = await apply_schedule_set(db, with_notify, agent_profile)
        assert (result3.created, result3.updated, result3.unchanged) == (0, 0, 1)


@pytest.mark.asyncio
async def test_apply_partial_invalid_set_writes_nothing(temp_db_path, agent_profile):
    text = _agent_manifest("nightly", cwd=agent_profile)
    broken_member = f"""
  broken:
    trigger:
      cron:
        expression: "not a cron"
        timezone: UTC
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: {agent_profile}
"""
    doc = parse_schedule_set(text + broken_member)

    async with StateDB() as db:
        with pytest.raises(ScheduleSetError):
            await apply_schedule_set(db, doc, agent_profile)
        rows = await db.list_schedules()
        assert rows == []


@pytest.mark.asyncio
async def test_apply_omitted_member_disables_only_that_member(
    temp_db_path, agent_profile, monkeypatch
):
    two_members = (
        _agent_manifest("nightly", cwd=agent_profile)
        + f"""
  hourly:
    trigger:
      every: 1h
    target:
      kind: command
      executable: refresh-index
    execution:
      cwd: {agent_profile}
"""
    )
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(two_members)
    async with StateDB() as db:
        result1 = await apply_schedule_set(db, doc, agent_profile)
        assert result1.created == 2

        one_member_doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
        result2 = await apply_schedule_set(db, one_member_doc, agent_profile)
        assert result2.disabled == 1
        assert result2.unchanged == 1

        nightly = await db.get_schedule_by_name("demo/nightly")
        hourly = await db.get_schedule_by_name("demo/hourly")
        assert nightly["enabled"] == 1
        assert hourly["enabled"] == 0
        # DISABLE, never a delete: the row and its history survive.
        assert hourly is not None

        # Re-adding the identical member must re-enable it — the digest still
        # matches the disabled row, so digest equality alone must not read
        # as UNCHANGED.
        result3 = await apply_schedule_set(db, doc, agent_profile)
        assert result3.updated == 1
        assert result3.unchanged == 1
        hourly = await db.get_schedule_by_name("demo/hourly")
        assert hourly["enabled"] == 1
        assert hourly["id"] == (await db.get_schedule_by_name("demo/hourly"))["id"]


@pytest.mark.asyncio
async def test_apply_at_trigger_reapply_after_fire_resets_gate_not_the_history(
    temp_db_path, tmp_path, monkeypatch
):
    """A re-apply after the fire leaves next_fire_at cleared; the max_runs gate also covers this one, but only because the run reached a counted status."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15T09:00:00Z", tmp_path))
    async with StateDB() as db:
        result1 = await apply_schedule_set(db, doc, tmp_path)
        assert result1.created == 1
        row1 = await db.get_schedule_by_name("demo/once")
        assert row1["max_runs"] == 1
        assert row1["next_fire_at"] is not None

        # Simulate the engine's own post-fire bookkeeping: one run recorded,
        # next_fire_at cleared, auto-disabled by the max_runs gate.
        await db.create_schedule_run(
            {
                "id": "run1",
                "schedule_id": row1["id"],
                "trigger_context": {},
                "action_kind": "command",
                "action_args": [],
                "status": "completed",
                "chain_depth": 0,
                "fired_at": time.time(),
            }
        )
        await db.update_schedule(row1["id"], next_fire_at=None, enabled=0)

        # Re-apply the identical (unchanged) document.
        result2 = await apply_schedule_set(db, doc, tmp_path)
        assert result2.updated == 1  # enabled mismatch forces UPDATE, not UNCHANGED
        row2 = await db.get_schedule_by_name("demo/once")
        assert row2["id"] == row1["id"]
        assert row2["max_runs"] == 1
        assert row2["next_fire_at"] is None  # a past instant is not written back
        # The run history from the first fire is untouched -- apply never
        # deletes or rewrites schedule_runs.
        assert await db.count_schedule_runs(row1["id"], chain_depth=0) == 1


@pytest.mark.asyncio
async def test_apply_does_not_resurrect_a_one_shot_that_was_skipped(
    temp_db_path, tmp_path, monkeypatch
):
    """A skip never spends the max_runs budget, and the instant has still passed, so a re-apply must not make it due again."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2026-07-15T09:00:00Z", tmp_path))
    async with StateDB() as db:
        await apply_schedule_set(db, doc, tmp_path)
        row1 = await db.get_schedule_by_name("demo/once")

        # What the engine leaves behind on missed_fire_policy=skip.
        await db.create_schedule_run(
            {
                "id": "skipped1",
                "schedule_id": row1["id"],
                "trigger_context": {},
                "action_kind": "command",
                "action_args": [],
                "status": "skipped",
                "chain_depth": 0,
                "fired_at": time.time(),
            }
        )
        await db.update_schedule(row1["id"], next_fire_at=None, enabled=0)
        # Premise: the budget really is unspent, so nothing downstream is
        # standing in for the rule under test.
        assert await db.count_schedule_runs(row1["id"], chain_depth=0) == 0

        result = await apply_schedule_set(db, doc, tmp_path)
        assert result.updated == 1
        row2 = await db.get_schedule_by_name("demo/once")
        assert row2["next_fire_at"] is None


@pytest.mark.asyncio
async def test_apply_still_arms_a_one_shot_whose_instant_is_ahead(
    temp_db_path, tmp_path, monkeypatch
):
    """The control: the rule declines past instants, not every re-apply."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_at_manifest("2099-07-15T09:00:00Z", tmp_path))
    async with StateDB() as db:
        await apply_schedule_set(db, doc, tmp_path)
        row1 = await db.get_schedule_by_name("demo/once")
        assert row1["next_fire_at"] > time.time()

        await db.update_schedule(row1["id"], next_fire_at=None, enabled=0)
        result = await apply_schedule_set(db, doc, tmp_path)
        assert result.updated == 1
        row2 = await db.get_schedule_by_name("demo/once")
        assert row2["next_fire_at"] == row1["next_fire_at"]


@pytest.mark.asyncio
async def test_apply_cross_owner_collision_is_an_error(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        # A row with the same qualified name, owned by a different set.
        await db.create_schedule(
            {
                "id": "existing123",
                "name": "demo/nightly",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "agent",
                "owner_key": "demo/other-set",
                "managed_by": "declaration",
            }
        )
        with pytest.raises(ScheduleSetError, match="owned by") as exc:
            await apply_schedule_set(db, doc, agent_profile)
        # --adopt refuses another set's row, so the error must not offer it.
        assert "--adopt" not in str(exc.value)
        assert "cannot be adopted" in str(exc.value)

        # Zero writes: the pre-existing row is untouched, no new one created.
        rows = await db.list_schedules()
        assert len(rows) == 1
        assert rows[0]["id"] == "existing123"


@pytest.mark.asyncio
async def test_apply_cli_quick_create_collision_is_an_error(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            {
                "id": "quickcreate1",
                "name": "demo/nightly",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "agent",
                "managed_by": "cli",
            }
        )
        with pytest.raises(ScheduleSetError) as exc:
            await apply_schedule_set(db, doc, agent_profile)
        assert "use --adopt" in str(exc.value)


@pytest.mark.asyncio
async def test_adopt_refuses_a_row_owned_by_another_set(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            {
                "id": "existing123",
                "name": "demo/nightly",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "agent",
                "owner_key": "demo/other-set",
                "managed_by": "declaration",
            }
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "demo/other-set" in plan[0].detail
        with pytest.raises(ScheduleSetError, match="adopt"):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        rows = await db.list_schedules()
        assert len(rows) == 1  # zero writes


def _legacy_adoption_row(name: str, *, cwd: Path, **overrides):
    profile_name = next(
        path.stem
        for path in (cwd / ".lionagi" / "agents").glob("*.md")
        if "model:" in path.read_text()
    )
    row = {
        "id": "legacy00001",
        "name": name,
        "enabled": 1,
        "trigger_type": "cron",
        "cron_expr": "0 2 * * *",
        # No stored zone: the zone the scheduler recorded the row firing in.
        "effective_timezone": "America/New_York",
        "action_kind": "agent",
        "action_model": "anthropic/claude-sonnet-5",
        "action_prompt": "check things",
        "action_agent": profile_name,
        "action_cwd": str(cwd),
        "action_project": "demo",
        "action_command_args": [],
        "missed_fire_policy": "skip",
        "overlap_policy": "skip",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_apply_without_adopt_leaves_a_legacy_row_and_creates_a_member(
    temp_db_path, agent_profile
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile))
        plan, _ = await build_plan(db, doc, agent_profile)
        assert [entry.action for entry in plan] == ["CREATE"]

        await apply_schedule_set(db, doc, agent_profile)
        rows = sorted(await db.list_schedules(), key=lambda row: row["name"])
        assert [row["name"] for row in rows] == ["demo/nightly", "nightly"]


@pytest.mark.asyncio
async def test_adopt_keeps_the_row_id_and_history(temp_db_path, agent_profile, pinned_scheduler_tz):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile))
        await db.create_schedule_run(
            {
                "id": "run-adopted",
                "schedule_id": "legacy00001",
                "trigger_context": {},
                "action_kind": "agent",
                "action_args": [],
                "status": "completed",
                "chain_depth": 0,
                "fired_at": time.time(),
            }
        )
        runs_before = await db.list_schedule_runs("legacy00001")

        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"]
        assert plan[0].adopted_from == "nightly"
        assert plan[0].existing_id == "legacy00001"

        result = await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert (result.adopted, result.created, result.updated) == (1, 0, 0)
        rows = await db.list_schedules()
        assert len(rows) == 1
        assert rows[0]["id"] == "legacy00001"
        assert rows[0]["name"] == "demo/nightly"
        assert rows[0]["owner_key"] == "demo/automation"
        assert rows[0]["managed_by"] == "declaration"
        assert rows[0]["enabled"] == 1
        assert await db.list_schedule_runs("legacy00001") == runs_before


@pytest.mark.asyncio
async def test_apply_after_adoption_is_unchanged(temp_db_path, agent_profile, pinned_scheduler_tz):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile))
        await apply_schedule_set(db, doc, agent_profile, adopt=True)
        plan, _ = await build_plan(db, doc, agent_profile)
        assert [entry.action for entry in plan] == ["UNCHANGED"]


@pytest.mark.asyncio
async def test_adopt_refusal_names_each_different_column(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                cron_expr="17 4 * * *",
                action_prompt="something else",
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "cron_expr" in plan[0].detail
        assert "17 4 * * *" in plan[0].detail and "0 2 * * *" in plan[0].detail
        assert "action_prompt" in plan[0].detail and "something else" in plan[0].detail
        with pytest.raises(ScheduleSetError, match="--adopt"):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        rows = await db.list_schedules()
        assert len(rows) == 1 and rows[0]["owner_key"] is None


def _disabled_member_manifest(name: str, *, cwd: Path) -> str:
    return _agent_manifest(name, cwd=cwd).replace(
        "    trigger:\n", "    enabled: false\n    trigger:\n", 1
    )


@pytest.mark.asyncio
async def test_adopt_refuses_to_enable_a_disabled_row(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile, enabled=0))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "enabled: row=0 declared=1" in plan[0].detail
        with pytest.raises(ScheduleSetError):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        row = await db.get_schedule_by_name("nightly")
        assert row["enabled"] == 0 and row["owner_key"] is None

        disabled = parse_schedule_set(_disabled_member_manifest("nightly", cwd=agent_profile))
        await apply_schedule_set(db, disabled, agent_profile, adopt=True)
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["enabled"] == 0 and row["owner_key"] == "demo/automation"


@pytest.mark.asyncio
async def test_adopt_accepts_an_unowned_quick_create_with_the_qualified_name(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "demo/nightly", cwd=agent_profile, id="quick0000001", managed_by="cli"
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"]
        assert plan[0].adopted_from is None
        await apply_schedule_set(db, doc, agent_profile, adopt=True)
        rows = await db.list_schedules()
        assert len(rows) == 1
        assert rows[0]["id"] == "quick0000001"
        assert rows[0]["managed_by"] == "declaration"


@pytest.mark.asyncio
async def test_adopt_refuses_two_members_that_resolve_to_one_row(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    profile_name = _legacy_adoption_row("nightly", cwd=agent_profile)["action_agent"]
    manifest = _agent_manifest(
        "nightly",
        cwd=agent_profile,
        extra_member=f"""  demo/nightly:
    trigger:
      cron:
        expression: "0 2 * * *"
        timezone: America/New_York
    target:
      kind: agent
      profile: {profile_name}
      prompt: "check things"
    execution:
      cwd: {agent_profile}
""",
    )
    doc = parse_schedule_set(manifest)
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("demo/nightly", cwd=agent_profile))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR", "ERROR"]
        assert all("legacy00001" in entry.detail for entry in plan)


def _perturb(value):
    if value is None:
        return "unlisted-value"
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value + "-changed"
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, list):
        return [*value, "changed"]
    if isinstance(value, dict):
        return {**value, "changed": True}
    raise AssertionError(f"no perturbation defined for {type(value).__name__}")


@pytest.mark.asyncio
async def test_adoption_compares_each_column_it_writes(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    written = _declared_columns(resolved)
    compared = sorted(set(written) - _ADOPT_DECLARATION_FIELDS)
    assert compared
    assert {"action_model", "action_project", "enabled", "resolved_timezone"} <= set(compared)

    async with StateDB() as db:
        await apply_schedule_set(db, doc, agent_profile)
        row = (await db.list_schedules())[0]

    candidate = {**row, "name": "nightly", "owner_key": None, "managed_by": "cli"}
    assert await _adopt_spec_differences(candidate, resolved) == []
    unchecked = []
    for column in compared:
        differences = await _adopt_spec_differences(
            {**candidate, column: _perturb(candidate.get(column))}, resolved
        )
        if column not in {name for name, _, _ in differences}:
            unchecked.append(column)
    # A column no launch of the row's kind and trigger reads is not compared at
    # all, and a column the launch acts on alike either way -- an inert
    # notification, an unset and an empty prompt -- has no difference to report.
    # The expectation is derived rather than listed, so it cannot drift from what
    # the comparison and the launch-reader table do; the per-column enumeration
    # test pins each table row on its own.
    from lionagi.studio.services.schedule_declaration import _column_read_by

    expected_unchecked = sorted(
        column
        for column in compared
        if not _column_read_by(column, candidate)
        or _adopt_comparable(column, candidate, _perturb(candidate.get(column)))
        == _adopt_comparable(column, {**candidate, **written}, written.get(column))
    )
    assert unchecked == expected_unchecked


_EVERY_HOUR_TRIGGER = (
    '    trigger:\n      cron:\n        expression: "0 2 * * *"\n'
    "        timezone: America/New_York\n",
    "    trigger:\n      every: 1h\n",
)


@pytest.mark.asyncio
async def test_adopt_compares_the_interval_an_interval_row_reads(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(*_EVERY_HOUR_TRIGGER)
    resolved = resolve_schedule_set(parse_schedule_set(manifest), agent_profile)["nightly"]
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="interval",
        cron_expr=None,
        effective_timezone=None,
        interval_sec=3600,
    )
    assert await _adopt_spec_differences(row, resolved) == []
    row["interval_sec"] = 7200
    assert [name for name, _, _ in await _adopt_spec_differences(row, resolved)] == ["interval_sec"]


@pytest.mark.asyncio
async def test_adopt_resolves_an_unpinned_agent_model_from_its_execution_root(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    unpinned = _legacy_adoption_row("nightly", cwd=agent_profile, action_model=None)
    assert resolved.db_fields["action_model"] == "anthropic/claude-sonnet-5"
    assert await _adopt_spec_differences(unpinned, resolved) == []

    gone = _legacy_adoption_row(
        "nightly", cwd=agent_profile, action_model=None, action_agent="missing-profile"
    )
    names = {name for name, _, _ in await _adopt_spec_differences(gone, resolved)}
    assert {"action_agent", "action_model"} <= names


def test_a_declared_agent_records_the_profile_its_execution_root_sees(tmp_path, monkeypatch):
    """The launched child reads its profile from the execution root, so the
    declaration records that profile wherever the apply runs from."""
    from lionagi._paths import find_lionagi_dirs
    from lionagi.cli._providers import load_agent_profile

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    apply_dir, root = tmp_path / "apply", tmp_path / "root"
    for directory, model, yolo in (
        (apply_dir, "anthropic/apply-model", "false"),
        (root, "anthropic/root-model", "true"),
    ):
        agents = directory / ".lionagi" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(f"---\nmodel: {model}\nyolo: {yolo}\n---\nBody.\n")
    assert find_lionagi_dirs(root) == [root / ".lionagi"]
    assert load_agent_profile("reviewer", cwd=root).yolo is True

    doc = parse_schedule_set(_agent_manifest("nightly", cwd=root))
    seen = []
    for applier in (apply_dir, root):
        monkeypatch.chdir(applier)
        resolved = resolve_schedule_set(doc, applier)["nightly"]
        seen.append((resolved.db_fields["action_model"], resolved.resolved["target"]))
    assert seen[0] == seen[1]
    assert seen[0][0] == "anthropic/root-model"


@pytest.mark.asyncio
async def test_adopt_refuses_chained_actions_until_they_are_cleared(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                on_success={"kind": "agent", "agent": "publisher", "prompt": "ship it"},
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "on_success" in plan[0].detail and "publisher" in plan[0].detail
        with pytest.raises(ScheduleSetError):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert (await db.get_schedule_by_name("nightly"))["owner_key"] is None
        await db.update_schedule("legacy00001", on_success=None, on_fail=None)
        await apply_schedule_set(db, doc, agent_profile, adopt=True)
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["on_success"] is None and row["on_fail"] is None


@pytest.mark.asyncio
async def test_adopt_compares_cron_zones_by_the_fire_time(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    row = _legacy_adoption_row("nightly", cwd=agent_profile)
    assert (
        await _adopt_spec_differences(row, resolve_schedule_set(doc, agent_profile)["nightly"])
        == []
    )

    async with StateDB() as db:
        await db.create_schedule(row)
        # The scheduler recorded Berlin; the declaration names New York.
        await db.update_schedule("legacy00001", effective_timezone="Europe/Berlin")
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        detail = plan[0].detail
        assert "resolved_timezone" in detail
        assert "Europe/Berlin" in detail and "America/New_York" in detail
        assert "zone each side's cron is read in" in detail


@pytest.mark.asyncio
async def test_adopt_reads_a_zoneless_row_in_the_zone_the_scheduler_recorded(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    """The scheduler daemon and the applying process can run in different zones;
    a row with no stored zone fires in the daemon's, never the applier's."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]

    # Applier in New York, scheduler recorded Berlin: refused although the
    # applier's own zone matches the declaration.
    berlin = _legacy_adoption_row("nightly", cwd=agent_profile, effective_timezone="Europe/Berlin")
    differences = await _adopt_spec_differences(berlin, resolved)
    assert ("resolved_timezone", "Europe/Berlin", "America/New_York") in differences

    # Applier in UTC, scheduler recorded New York: the applier's zone is irrelevant.
    monkeypatch.setattr("lionagi.studio.config.SCHEDULER_TZ", "UTC")
    new_york = _legacy_adoption_row("nightly", cwd=agent_profile)
    assert await _adopt_spec_differences(new_york, resolved) == []

    # A stored zone wins over the recorded one, as it does at fire time.
    stored = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        resolved_timezone="America/New_York",
        effective_timezone="Europe/Berlin",
    )
    assert await _adopt_spec_differences(stored, resolved) == []


@pytest.mark.asyncio
async def test_adopt_refuses_a_zoneless_row_no_scheduler_has_recorded(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, effective_timezone=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "resolved_timezone: row=None declared='America/New_York'" in plan[0].detail
        assert "no scheduler has recorded the zone it fires in" in plan[0].detail


def _undeclared_value(column: str):
    return {
        "on_success": {"kind": "agent", "agent": "publisher", "prompt": "ship it"},
        "on_fail": {"kind": "agent", "agent": "pager", "prompt": "page"},
        "action_extra_args": ["extra"],
        "threshold_config": {"metric": "failed_sessions", "op": "gt", "value": 3},
        "poll_interval_sec": 60,
    }[column]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column",
    ["on_success", "on_fail", "action_extra_args", "threshold_config"],
)
async def test_adopt_refuses_a_row_setting_a_column_no_declaration_writes(
    temp_db_path, agent_profile, pinned_scheduler_tz, column
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    value = _undeclared_value(column)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, **{column: value})
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert f"{column}={value!r}" in plan[0].detail
        assert "which a declaration cannot express" in plan[0].detail
        with pytest.raises(ScheduleSetError):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert (await db.get_schedule_by_name("nightly"))["owner_key"] is None


@pytest.mark.asyncio
async def test_adopt_refuses_a_github_poll_row_with_its_own_poll_interval(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        _EVERY_HOUR_TRIGGER[0], "    trigger:\n      github:\n        repo: acme/widgets\n"
    )
    doc = parse_schedule_set(manifest)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                trigger_type="github_poll",
                cron_expr=None,
                effective_timezone=None,
                github_repo="acme/widgets",
                poll_interval_sec=60,
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "poll_interval_sec=60" in plan[0].detail
        assert "which a declaration cannot express" in plan[0].detail
        assert "differs from the existing row" not in plan[0].detail


def test_the_undeclared_column_list_is_the_one_the_refusal_test_names():
    assert set(_ADOPT_UNDECLARED_COLUMNS) == {
        "on_success",
        "on_fail",
        "action_extra_args",
        "threshold_config",
        "poll_interval_sec",
    }


def test_a_profile_model_the_launch_refuses_is_refused_at_declaration(agent_profile):
    agents = agent_profile / ".lionagi" / "agents"
    (agents / "wide.md").write_text("---\nmodel: anthropic/claude-sonnet-5[1m]\n---\nBody.\n")
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        "profile: reviewer", "profile: wide"
    )
    with pytest.raises(ScheduleSetError) as excinfo:
        resolve_schedule_set(parse_schedule_set(manifest), agent_profile)
    ((name, message),) = excinfo.value.errors
    assert name == "demo/nightly"
    assert "'anthropic/claude-sonnet-5[1m]' (from profile 'wide')" in message
    assert "cannot be passed to a scheduled launch" in message

    pinned = manifest.replace(
        'prompt: "check things"', 'prompt: "check things"\n      model: anthropic/claude-sonnet-5'
    )
    resolved = resolve_schedule_set(parse_schedule_set(pinned), agent_profile)["nightly"]
    assert resolved.db_fields["action_model"] == "anthropic/claude-sonnet-5"


def test_a_profile_model_that_is_not_text_is_refused_naming_the_profile(agent_profile):
    agents = agent_profile / ".lionagi" / "agents"
    (agents / "numbered.md").write_text("---\nmodel: 123\n---\nBody.\n")
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        "profile: reviewer", "profile: numbered"
    )
    with pytest.raises(ScheduleSetError) as excinfo:
        resolve_schedule_set(parse_schedule_set(manifest), agent_profile)
    ((name, message),) = excinfo.value.errors
    assert name == "demo/nightly"
    assert "target.agent model 123 (from profile 'numbered') is not a model name" in message


def test_a_model_with_a_trailing_newline_is_refused_at_declaration(agent_profile):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        'prompt: "check things"',
        'prompt: "check things"\n      model: "anthropic/claude-sonnet-5\\n"',
    )
    with pytest.raises(ScheduleSetError) as excinfo:
        resolve_schedule_set(parse_schedule_set(manifest), agent_profile)
    ((name, message),) = excinfo.value.errors
    assert name == "demo/nightly"
    assert "'anthropic/claude-sonnet-5\\n' (from target.model)" in message
    assert "cannot be passed to a scheduled launch" in message


_UNLAUNCHABLE_PROMPTS = {"end-of-options": "--", "over-cap": "x" * (MAX_SPEC_PROMPT_CHARS + 1)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt", list(_UNLAUNCHABLE_PROMPTS.values()), ids=list(_UNLAUNCHABLE_PROMPTS)
)
async def test_a_prompt_the_launch_refuses_is_refused_before_any_write(
    temp_db_path, agent_profile, pinned_scheduler_tz, prompt
):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        'prompt: "check things"', f"prompt: {json.dumps(prompt)}"
    )
    doc = parse_schedule_set(manifest)
    async with StateDB() as db:
        with pytest.raises(ScheduleSetError) as excinfo:
            await apply_schedule_set(db, doc, agent_profile)
        assert "target.prompt cannot be passed to a scheduled launch" in str(excinfo.value)
        assert await db.list_schedules() == []

        # The same prompt on an unowned row: adoption refuses and leaves the row as it was.
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_prompt=prompt)
        )
        before = await db.get_schedule_by_name("nightly")
        with pytest.raises(ScheduleSetError) as excinfo:
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert "target.prompt cannot be passed to a scheduled launch" in str(excinfo.value)
        assert await db.get_schedule_by_name("nightly") == before


@pytest.mark.asyncio
async def test_adopt_refuses_a_recorded_zone_this_process_does_not_share(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A zone-less row fires in whichever scheduler runs it; the recorded zone may
    be from a scheduler that has stopped, so adoption needs this process to agree."""
    berlin_member = _agent_manifest("nightly", cwd=agent_profile).replace(
        "America/New_York", "Europe/Berlin"
    )
    doc = parse_schedule_set(berlin_member)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, effective_timezone="Europe/Berlin")
        )
        assert (
            await _adopt_spec_differences(
                await db.get_schedule_by_name("nightly"),
                resolve_schedule_set(doc, agent_profile)["nightly"],
            )
            == []
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        detail = plan[0].detail
        assert "last recorded 'Europe/Berlin'" in detail
        assert "reads the scheduler zone as 'America/New_York'" in detail
        assert "LIONAGI_SCHEDULER_TZ set to the scheduler's zone" in detail
        assert "edit the member to match the row" not in detail

        # The same row recorded in this process's zone adopts.
        await db.update_schedule("legacy00001", effective_timezone="America/New_York")
        plan, _ = await build_plan(
            db,
            parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile)),
            agent_profile,
            adopt=True,
        )
        assert [entry.action for entry in plan] == ["ADOPT"]


@pytest.mark.asyncio
async def test_an_unrecorded_zone_is_not_offered_as_a_member_edit(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, effective_timezone=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert "edit the member to match the row" not in plan[0].detail
        assert "start the scheduler once to record it" in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_names_an_unloadable_recorded_zone_as_one_no_member_declares(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, effective_timezone="Not/AZone")
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    detail = plan[0].detail
    assert "resolved_timezone: row='Not/AZone' declared='America/New_York'" in detail
    assert (
        "no member can declare the row's resolved_timezone, so this row cannot be adopted "
        "as it stands." in detail
    )
    assert "edit the member to match the row" not in detail
    assert "this host cannot load 'Not/AZone', so start the scheduler" in detail
    assert "LIONAGI_SCHEDULER_TZ" not in detail


@pytest.mark.asyncio
async def test_adopt_names_a_github_cadence_as_one_no_member_declares(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n',
        "      github:\n        repo: acme/widgets\n",
    )
    doc = parse_schedule_set(manifest)
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="github_poll",
        cron_expr=None,
        github_repo="acme/widgets",
        github_filter=None,
        interval_sec=60,
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    detail = plan[0].detail
    assert "differs from the existing row in 1 field(s) -- interval_sec: row=60" in detail
    assert (
        "no member can declare the row's interval_sec, so this row cannot be adopted "
        "as it stands." in detail
    )
    assert "edit the member to match the row" not in detail


@pytest.mark.asyncio
async def test_adopt_refusal_for_a_row_without_an_execution_root(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_cwd=None, action_project=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    detail = plan[0].detail
    assert (
        "action_project: row='execution root: inherits the scheduler working directory; "
        "run project: detected from the scheduler working directory' "
        f"declared=\"execution root '{agent_profile}'; run project: 'demo', set explicitly\""
    ) in detail
    assert (
        "no member can declare the row's action_cwd, action_project, so this row cannot be "
        "adopted as it stands." in detail
    )
    assert "edit the member to match the row" not in detail


@pytest.mark.asyncio
async def test_adopt_refuses_a_row_written_between_planning_and_commit(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    chain = {"kind": "agent", "agent": "publisher", "prompt": "ship it"}
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile))
        real_apply = db.apply_schedule_set

        async def chain_written_first(**kwargs):
            await db.update_schedule("legacy00001", on_success=chain)
            return await real_apply(**kwargs)

        monkeypatch.setattr(db, "apply_schedule_set", chain_written_first)
        with pytest.raises(ScheduleSetError, match="changed while this apply was planning") as exc:
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert "(its owner or its last update is no longer the one this plan read)" in str(
            exc.value
        )
        assert "owner_key" not in str(exc.value)
        row = await db.get_schedule_by_name("nightly")
        assert row["owner_key"] is None
        assert row["on_success"] == chain


@pytest.mark.asyncio
async def test_an_update_refuses_a_row_written_between_planning_and_commit(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    async with StateDB() as db:
        await apply_schedule_set(
            db, parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile)), agent_profile
        )
        row = await db.get_schedule_by_name("demo/nightly")
        changed = parse_schedule_set(
            _agent_manifest("nightly", cwd=agent_profile).replace("check things", "check more")
        )
        real_apply = db.apply_schedule_set

        async def disabled_first(**kwargs):
            await db.update_schedule(row["id"], enabled=0)
            return await real_apply(**kwargs)

        monkeypatch.setattr(db, "apply_schedule_set", disabled_first)
        with pytest.raises(ScheduleSetError, match="changed while this apply was planning"):
            await apply_schedule_set(db, changed, agent_profile)
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["enabled"] == 0
        assert row["action_prompt"] == "check things"


@pytest.mark.asyncio
async def test_a_disable_refuses_a_row_written_between_planning_and_commit(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    extra = (
        "  weekly:\n"
        "    trigger:\n"
        "      every: 6h\n"
        "    target:\n"
        "      kind: agent\n"
        "      profile: reviewer\n"
        '      prompt: "weekly things"\n'
        "    execution:\n"
        f"      cwd: {agent_profile}\n"
    )
    async with StateDB() as db:
        await apply_schedule_set(
            db,
            parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile, extra_member=extra)),
            agent_profile,
        )
        weekly = await db.get_schedule_by_name("demo/weekly")
        real_apply = db.apply_schedule_set

        async def edited_first(**kwargs):
            await db.update_schedule(weekly["id"], action_prompt="edited by hand")
            return await real_apply(**kwargs)

        monkeypatch.setattr(db, "apply_schedule_set", edited_first)
        with pytest.raises(ScheduleSetError, match="changed while this apply was planning"):
            await apply_schedule_set(
                db, parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile)), agent_profile
            )
        weekly = await db.get_schedule_by_name("demo/weekly")
        assert weekly["enabled"] == 1
        assert weekly["action_prompt"] == "edited by hand"


@pytest.mark.parametrize("spelling", ["symlink", "trailing_slash"])
@pytest.mark.asyncio
async def test_adopt_compares_the_working_directory_where_it_resolves(
    temp_db_path, agent_profile, pinned_scheduler_tz, spelling
):
    if spelling == "symlink":
        link = agent_profile.parent / "linked-profile"
        link.symlink_to(agent_profile, target_is_directory=True)
        row_cwd = str(link)
    else:
        row_cwd = str(agent_profile) + "/"
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_cwd=row_cwd)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_ignores_intervals_a_cron_row_never_reads(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly", cwd=agent_profile, interval_sec=3600, poll_interval_sec=600
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_clears_a_poll_interval_so_a_later_github_member_polls_as_declared(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, poll_interval_sec=600)
        )
        await apply_schedule_set(db, doc, agent_profile, adopt=True)
        adopted = await db.get_schedule_by_name("demo/nightly")
    assert adopted["owner_key"] == "demo/automation"
    assert adopted["poll_interval_sec"] is None


@pytest.mark.parametrize(
    "trigger,overrides",
    [
        (
            "cron",
            {"github_repo": "acme/old", "github_filter": "label:stale"},
        ),
        (
            "every",
            {"trigger_type": "interval", "interval_sec": 3600, "cron_expr": "0 2 * * *"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_adopt_ignores_trigger_columns_the_rows_own_trigger_never_reads(
    temp_db_path, agent_profile, pinned_scheduler_tz, trigger, overrides
):
    manifest = _agent_manifest("nightly", cwd=agent_profile)
    if trigger == "every":
        manifest = manifest.replace(
            '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n',
            "      every: 1h\n",
        )
    doc = parse_schedule_set(manifest)
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile, **overrides))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_reads_a_passed_instant_as_the_write_does(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n',
        '      at: "2020-01-01T00:00:00+00:00"\n',
    )
    doc = parse_schedule_set(manifest)
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="at",
        cron_expr=None,
        next_fire_at=None,
        max_runs=1,
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_refuses_a_live_one_shot_when_the_declared_instant_has_passed(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n',
        '      at: "2020-01-01T00:00:00+00:00"\n',
    )
    doc = parse_schedule_set(manifest)
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="at",
        cron_expr=None,
        next_fire_at=time.time() + 365 * 86400,
        max_runs=1,
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "next_fire_at: row=" in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_reads_a_row_with_no_command_args_as_an_empty_list(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_command_args=None)
        )
        assert (await db.list_schedules())[0]["action_command_args"] is None
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


def test_adopt_compares_a_column_only_under_the_trigger_that_reads_it():
    cron, github = {"trigger_type": "cron"}, {"trigger_type": "github_poll"}
    assert _adopt_comparable("cron_expr", cron, "0 2 * * *") == "0 2 * * *"
    assert _adopt_comparable("cron_expr", github, "0 2 * * *") is None
    for name in ("github_repo", "github_filter"):
        assert _adopt_comparable(name, github, "acme/widgets") == "acme/widgets"
        assert _adopt_comparable(name, cron, "acme/widgets") is None
    assert _adopt_comparable("notify_command", {"notify_on": []}, "echo done") is None
    assert _adopt_comparable("notify_command", {"notify_on": ["failed"]}, "echo done") == (
        "echo done"
    )


@pytest.mark.parametrize(
    "row,name,current",
    [
        ({"action_kind": "playbook"}, "action_prompt", "hello"),
        ({"action_kind": "flow_yaml"}, "action_model", "anthropic/claude-sonnet-5"),
        ({"action_kind": "agent"}, "action_model", None),
    ],
)
@pytest.mark.asyncio
async def test_a_prompt_or_model_no_member_can_declare_is_named_as_such(row, name, current):
    assert await _adopt_undeclarable(row, name, current, is_global=False)


@pytest.mark.asyncio
async def test_a_project_named_global_is_not_the_global_scope(
    temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch
):
    from lionagi.studio.services import schedule_declaration

    manifest = _agent_manifest("nightly", cwd=agent_profile).replace(
        "  project: demo\n", "  project: global\n"
    )
    doc = parse_schedule_set(manifest)
    seen: list[bool] = []
    real = schedule_declaration._adopt_undeclarable

    async def recording(row, name, current, *, is_global):
        seen.append(is_global)
        return await real(row, name, current, is_global=is_global)

    monkeypatch.setattr(schedule_declaration, "_adopt_undeclarable", recording)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_prompt="other things")
        )
        await build_plan(db, doc, agent_profile, adopt=True)
    assert seen and not any(seen)


@pytest.mark.asyncio
async def test_adopt_reads_a_repeated_notify_status_as_one(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    notify = '    notify:\n      on: [failed]\n      command: "echo done"\n'
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile, extra_member=notify))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    row = _legacy_adoption_row(
        "nightly", cwd=agent_profile, notify_on=["failed", "failed"], notify_command="echo done"
    )
    assert await _adopt_spec_differences(row, resolved) == []


@pytest.mark.parametrize(
    "column,override",
    [
        ("action_prompt", {"action_prompt": "--"}),
        ("action_model", {"action_model": "anthropic/claude-sonnet-5[1m]"}),
    ],
)
@pytest.mark.asyncio
async def test_adopt_offers_no_member_edit_for_a_value_no_member_can_declare(
    temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch, column, override
):
    from lionagi.cli import _project

    monkeypatch.setattr(_project, "detect_project", lambda path: (None, "none"))
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile, **override))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    detail = plan[0].detail
    assert [entry.action for entry in plan] == ["ERROR"]
    assert f"no member can declare the row's {column}" in detail
    assert "edit the member to match the row" not in detail


@pytest.mark.parametrize(
    "extra_member", ["", "      project: null"], ids=["set-project", "null-project"]
)
@pytest.mark.asyncio
async def test_adopt_names_no_null_project_edit_for_a_row_of_a_kind_without_a_project(
    temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch, extra_member
):
    """A play row takes no project argument, so its project effect reads "none" for a
    reason ``project: null`` on an agent member does not close: the kinds differ."""
    from lionagi.cli import _project

    monkeypatch.setattr(_project, "detect_project", lambda path: (None, "none"))
    doc = parse_schedule_set(
        _agent_manifest("nightly", cwd=agent_profile, extra_member=extra_member)
    )
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                action_kind="play",
                action_playbook="nightly-review",
                action_project="demo",
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    detail = plan[0].detail
    assert [entry.action for entry in plan] == ["ERROR"], detail
    assert "action_kind" in detail
    assert "execution: {project: null}" not in detail


@pytest.mark.asyncio
async def test_adopt_names_the_null_project_edit_for_a_row_running_without_one(
    temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch
):
    """A row holding no project whose root detects none runs with no project, which
    an explicit ``project: null`` on the member declares. The refusal is therefore a
    difference a member edit can close, and it names that edit rather than calling
    the value one no member can declare."""
    from lionagi.cli import _project

    monkeypatch.setattr(_project, "detect_project", lambda path: (None, "none"))
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_project=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        detail = plan[0].detail
        assert [entry.action for entry in plan] == ["ERROR"], detail
        assert "action_project" in detail
        assert "no member can declare" not in detail
        assert "declaring no project with execution: {project: null}." in detail

        # The edit the refusal names is one that works: the member declaring no
        # project resolves the row's stored None rather than the document's project.
        null_project = parse_schedule_set(
            _agent_manifest("nightly", cwd=agent_profile, extra_member="      project: null")
        )
        plan, _ = await build_plan(db, null_project, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_reads_an_unpinned_model_at_the_execution_root_not_the_apply_directory(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    root = agent_profile / "root"
    (root / ".lionagi" / "agents").mkdir(parents=True)
    (root / ".lionagi" / "agents" / "reviewer.md").write_text(
        "---\nmodel: anthropic/root-model\n---\nBody.\n"
    )
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=root))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    assert resolved.db_fields["action_model"] == "anthropic/root-model"

    unpinned = _legacy_adoption_row("nightly", cwd=root, action_model=None)
    assert await _adopt_spec_differences(unpinned, resolved) == []
    # The apply directory's own profile names another model.
    pinned_elsewhere = _legacy_adoption_row(
        "nightly", cwd=root, action_model="anthropic/claude-sonnet-5"
    )
    assert ("action_model", "anthropic/claude-sonnet-5", "anthropic/root-model") in (
        await _adopt_spec_differences(pinned_elsewhere, resolved)
    )


@pytest.mark.asyncio
async def test_adopt_reads_a_profile_model_that_is_not_text_as_no_model(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A profile file can carry any value under ``model``; only a string names a
    model. A row that leaves its model to a profile naming a number is read as
    naming no model, not as a crash while the plan is built."""
    (agent_profile / ".lionagi" / "agents" / "numbered.md").write_text(
        "---\nmodel: 123\n---\nBody.\n"
    )
    doc = parse_schedule_set(
        _agent_manifest("nightly", cwd=agent_profile)
        .replace("profile: reviewer", "profile: numbered")
        .replace(
            'prompt: "check things"',
            'prompt: "check things"\n      model: anthropic/claude-sonnet-5',
        )
    )
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    row = _legacy_adoption_row(
        "nightly", cwd=agent_profile, action_model=None, action_agent="numbered"
    )
    # The profile names no model, so the row names none and the member's pinned model
    # is a difference -- not an unhandled exception from reading 123 as a model name.
    assert await _adopt_spec_differences(row, resolved) == [
        ("action_model", None, "anthropic/claude-sonnet-5")
    ]


@pytest.mark.asyncio
async def test_a_reapply_that_changes_the_stored_model_says_so(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    from lionagi.studio.cli import _fmt_plan_line

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


def test_columns_the_scheduler_reads_alike_compare_alike():
    github = {"trigger_type": "github_poll", "poll_interval_sec": None}
    assert _adopt_comparable("interval_sec", github, 300) == _adopt_comparable(
        "interval_sec", github, None
    )
    assert _adopt_comparable("interval_sec", github, 60) != _adopt_comparable(
        "interval_sec", github, None
    )
    interval = {"trigger_type": "interval"}
    assert _adopt_comparable("interval_sec", interval, 300) != _adopt_comparable(
        "interval_sec", interval, None
    )
    notifying = {"notify_command": "echo done"}
    assert _adopt_comparable("notify_on", notifying, []) == _adopt_comparable(
        "notify_on", notifying, None
    )
    assert _adopt_comparable("notify_on", notifying, ["failed"]) != _adopt_comparable(
        "notify_on", notifying, None
    )
    # No notification is registered without a command to run on the event, so the
    # event list is inert wherever the row holds no command.
    assert _adopt_comparable("notify_on", {}, ["failed"]) == _adopt_comparable(
        "notify_on", {}, None
    )
    assert _adopt_comparable("notify_command", {"notify_on": ["failed"]}, "") is None


@pytest.mark.asyncio
async def test_adopt_reads_an_empty_notify_list_as_no_notification(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    row = _legacy_adoption_row("nightly", cwd=agent_profile, notify_on=[])
    assert await _adopt_spec_differences(row, resolved) == []


@pytest.mark.asyncio
async def test_adopt_reads_notify_statuses_in_any_order_alike(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    notify = '    notify:\n      on: [failed, completed]\n      command: "echo done"\n'
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile, extra_member=notify))
    resolved = resolve_schedule_set(doc, agent_profile)["nightly"]
    row = _legacy_adoption_row(
        "nightly", cwd=agent_profile, notify_on=["completed", "failed"], notify_command="echo done"
    )
    assert await _adopt_spec_differences(row, resolved) == []
    row["notify_on"] = ["completed"]
    assert [name for name, _, _ in await _adopt_spec_differences(row, resolved)] == ["notify_on"]


@pytest.mark.asyncio
async def test_adopt_accepts_the_default_github_poll_interval(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, poll_interval_sec=300)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"]


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


@pytest.mark.asyncio
async def test_plain_reapply_keeps_chain_columns_set_outside_the_declaration(
    temp_db_path, agent_profile
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    chain = {"kind": "agent", "agent": "reviewer", "prompt": "follow up"}
    async with StateDB() as db:
        await apply_schedule_set(db, doc, agent_profile)
        row = await db.get_schedule_by_name("demo/nightly")
        await db.update_schedule(row["id"], on_success=chain, on_fail=chain)

        edited = parse_schedule_set(
            _agent_manifest("nightly", cwd=agent_profile).replace(
                'prompt: "check things"', 'prompt: "check things again"'
            )
        )
        result = await apply_schedule_set(db, edited, agent_profile)
        assert result.updated == 1
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["action_prompt"] == "check things again"
        assert row["on_success"] == chain and row["on_fail"] == chain


@pytest.mark.asyncio
async def test_adopt_recognizes_a_project_inferred_from_the_execution_root(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    config = agent_profile / ".lionagi" / "config.toml"
    config.write_text('[project]\nname = "demo"\n')
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_project=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"]


@pytest.mark.asyncio
async def test_adopt_refusal_describes_changed_project_attribution(
    temp_db_path, agent_profile, monkeypatch, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    monkeypatch.setattr(
        "lionagi.cli._project.detect_project", lambda cwd=None: ("other-project", "config_toml")
    )
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_project=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        assert "action_project" in plan[0].detail
        assert "other-project" in plan[0].detail and "demo" in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_refusal_describes_both_changed_execution_roots(
    temp_db_path, agent_profile, tmp_path, monkeypatch, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    row_root = tmp_path / "row-root"
    row_root.mkdir()

    async def get_project(name):
        if name == "row-root":
            return {"path": str(row_root)}
        return None

    monkeypatch.setattr("lionagi.studio.services.projects.get_project", get_project)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly", cwd=agent_profile, action_cwd=None, action_project="row-root"
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ERROR"]
        detail = plan[0].detail
        assert "action_project" in detail
        assert str(row_root) in detail and str(agent_profile) in detail


@pytest.mark.asyncio
async def test_apply_that_loses_an_ownership_claim_rolls_back_its_creates(
    temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch
):
    profile_name = _legacy_adoption_row("nightly", cwd=agent_profile)["action_agent"]
    manifest = _agent_manifest(
        "nightly",
        cwd=agent_profile,
        extra_member=f"""  weekly:
    trigger:
      cron:
        expression: "0 3 * * 0"
        timezone: America/New_York
    target:
      kind: agent
      profile: {profile_name}
      prompt: "check things"
    execution:
      cwd: {agent_profile}
""",
    )
    doc = parse_schedule_set(manifest)
    rival = parse_schedule_set(
        _agent_manifest("nightly", cwd=agent_profile).replace("name: automation", "name: rival")
    )
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile))
        real_apply = db.apply_schedule_set
        raced = False

        async def competing_apply(**kwargs):
            nonlocal raced
            if not raced:
                raced = True
                await apply_schedule_set(db, rival, agent_profile, adopt=True)
            return await real_apply(**kwargs)

        monkeypatch.setattr(db, "apply_schedule_set", competing_apply)
        with pytest.raises(ScheduleSetError, match="changed while this apply was planning"):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert raced is True
        rows = await db.list_schedules()
        assert len(rows) == 1
        assert rows[0]["id"] == "legacy00001"
        assert rows[0]["owner_key"] == "demo/rival"


@pytest.mark.asyncio
async def test_dry_run_plan_never_writes(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        plan, _resolved = await build_plan(db, doc, agent_profile)
        assert [(e.qualified_name, e.action) for e in plan] == [("demo/nightly", "CREATE")]
        rows = await db.list_schedules()
        assert rows == []


# Command-target args: both create paths write action_command_args, so both
# must hold it to the same contract


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


def test_declarative_command_target_accepts_author_written_flags(tmp_path, monkeypatch):
    """A command runner whose args may not start with '-' cannot express a command.

    Nearly every real command line carries a flag, so rejecting them left
    ``kind: command`` unable to say what it exists to say. The args land in
    ``action_command_args``, whose contract permits author-written flags; the
    field that forbids them is ``action_extra_args``, which a command launch
    never uses.
    """
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    manifest = _command_manifest(
        '        - "run"\n'
        '        - "--no-project"\n'
        '        - "python"\n'
        '        - "worker.py"\n'
        '        - "--repo"\n'
        '        - "owner/name"\n',
        tmp_path,
    )
    resolved = resolve_schedule_set(parse_schedule_set(manifest), tmp_path)
    assert resolved["m"].db_fields["action_command_args"] == [
        "run",
        "--no-project",
        "python",
        "worker.py",
        "--repo",
        "owner/name",
    ]


def test_every_flag_position_resolves_not_only_the_first(tmp_path, monkeypatch):
    """The old rejection returned at the first offending element.

    That made the error name one token, so a reader inferred one defect and
    tried renaming it. Each of these would have been refused in turn, so the
    test asserts a flag resolves in first, middle and last position rather
    than trusting a single case.
    """
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    for args in (
        ['        - "--first"\n', '        - "x"\n'],
        ['        - "x"\n', '        - "--middle"\n', '        - "y"\n'],
        ['        - "x"\n', '        - "--last"\n'],
    ):
        manifest = _command_manifest("".join(args), tmp_path)
        resolved = resolve_schedule_set(parse_schedule_set(manifest), tmp_path)
        got = resolved["m"].db_fields["action_command_args"]
        assert any(tok.startswith("-") for tok in got), got


def test_both_create_paths_hold_command_args_to_the_same_contract(tmp_path, monkeypatch):
    """The divergence guard, and the test whose absence let the paths drift.

    The declarative resolver and the imperative create service write the same
    database field, and they validated it with different functions. What has to
    be asserted is the *delegation*: that the resolver hands this field to the
    imperative path's own validator rather than to a check of its own.

    Calling that validator on the resolver's output would not assert it. The
    resolver already calls it, so re-running it on the result passes by
    construction and would stay green through exactly the regression this test
    exists to catch: a resolver that substitutes a weaker check and still
    returns a list. So the validator is replaced with a recorder and the call
    itself is what gets asserted.
    """
    from lionagi.studio.services import _schedule_validation as schedules_svc

    seen: list[list[str]] = []
    real_validator = schedules_svc._svc_validate_command_args

    def _recording_validator(value):
        seen.append(value)
        return real_validator(value)

    # The resolver imports this symbol inside the function body, so patching it
    # on the defining module is what the call actually resolves.
    monkeypatch.setattr(schedules_svc, "_svc_validate_command_args", _recording_validator)
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    manifest = _command_manifest(
        '        - "run"\n        - "--no-project"\n        - "worker.py"\n', tmp_path
    )
    resolved = resolve_schedule_set(parse_schedule_set(manifest), tmp_path)

    assert seen == [["run", "--no-project", "worker.py"]], (
        "the resolver did not delegate action_command_args validation to the "
        f"imperative path's validator; recorded calls: {seen}"
    )
    assert resolved["m"].db_fields["action_command_args"] == [
        "run",
        "--no-project",
        "worker.py",
    ]


def test_declarative_command_target_still_refuses_a_non_allowlisted_executable(
    tmp_path, monkeypatch
):
    """Accepting flags must not have widened the boundary that is real.

    For this kind the security boundary is the executable allow-list, not the
    shape of the arguments.
    """
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "something-else")
    manifest = _command_manifest('        - "--flag"\n', tmp_path)
    with pytest.raises(ValueError, match="ALLOWLIST"):
        resolve_schedule_set(parse_schedule_set(manifest), tmp_path)


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


@pytest.mark.parametrize("args", _REAL_COMMAND_ARG_SHAPES)
def test_real_command_shapes_are_expressible_declaratively(args, tmp_path, monkeypatch):
    """Every shape a scheduled command actually uses must survive the resolver.

    Flags are not an edge case for this kind: all five shapes carry at least
    one, so a resolver that refuses them can express none of them. The last
    shape is the mixed case that matters most — an author-written flag whose
    value is a ``{{var}}`` template, which is exactly the combination the two
    contracts have to tell apart.
    """
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    args_yaml = "".join(f'        - "{a}"\n' for a in args)
    resolved = resolve_schedule_set(
        parse_schedule_set(_command_manifest(args_yaml, tmp_path)), tmp_path
    )
    assert resolved["m"].db_fields["action_command_args"] == args


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


@pytest.mark.parametrize("args", _REAL_COMMAND_ARG_SHAPES)
def test_the_other_fields_validator_would_refuse_every_real_shape(args):
    """Proof the test above discriminates, and why the field identity matters.

    ``action_extra_args`` and ``action_command_args`` are not two names for one
    contract: this asserts the stricter one rejects every shape the looser one
    must accept. That is what makes the accept assertions above meaningful
    rather than vacuous — they would all fail against the stricter validator.

    If this test ever fails because the strict check stopped rejecting flags,
    the two contracts have converged and the reason these fields are validated
    separately no longer holds. Read that before deleting this.
    """
    from lionagi.studio.scheduler.subprocess import _validate_extra_args

    with pytest.raises(ValueError, match="starts with '-'"):
        _validate_extra_args(args)


# ---------------------------------------------------------------------------
# Adoption reads each column the way the launch that holds it reads it
# ---------------------------------------------------------------------------

_AGENT_TARGET_BLOCK = (
    '    target:\n      kind: agent\n      profile: reviewer\n      prompt: "check things"\n'
)
_PLAY_TARGET_BLOCK = "    target:\n      kind: playbook\n      name: health-audit\n"
_COMMAND_TARGET_BLOCK = "    target:\n      kind: command\n      executable: refresh-index\n"
_NOTIFY_BLOCK = '    notify:\n      on: [failed]\n      command: "echo done"\n'


def _manifest_with_target(name: str, *, cwd: Path, target: str, extra: str = "") -> str:
    """The agent member's manifest with its target block replaced by *target*."""
    return _agent_manifest(name, cwd=cwd, extra_member=extra).replace(_AGENT_TARGET_BLOCK, target)


def _play_row(cwd: Path, **overrides) -> dict:
    row = {
        "action_kind": "play",
        "action_model": None,
        "action_prompt": None,
        "action_agent": None,
        "action_playbook": "health-audit",
    }
    row.update(overrides)
    return _legacy_adoption_row("nightly", cwd=cwd, **row)


def _command_row(cwd: Path, **overrides) -> dict:
    row = {
        "action_kind": "command",
        "action_model": None,
        "action_prompt": None,
        "action_agent": None,
        "action_command": "refresh-index",
    }
    row.update(overrides)
    return _legacy_adoption_row("nightly", cwd=cwd, **row)


@pytest.mark.asyncio
async def test_adopt_reads_an_empty_prompt_on_a_play_row_as_no_prompt(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A play launch appends a prompt only when it is set, so '' and none act alike."""
    doc = parse_schedule_set(
        _manifest_with_target("nightly", cwd=agent_profile, target=_PLAY_TARGET_BLOCK)
    )
    async with StateDB() as db:
        await db.create_schedule(_play_row(agent_profile, action_prompt=""))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [_PLAY_TARGET_BLOCK, _COMMAND_TARGET_BLOCK])
async def test_adopt_reads_a_stale_agent_a_kind_never_passes_on(
    temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch, target
):
    """Only an agent or engine launch passes the profile on, so a play or command
    row holding one -- which the launch validates and then drops -- acts like a
    row holding none."""
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    doc = parse_schedule_set(_manifest_with_target("nightly", cwd=agent_profile, target=target))
    row = (
        _command_row(agent_profile, action_agent="reviewer")
        if target == _COMMAND_TARGET_BLOCK
        else _play_row(agent_profile, action_agent="reviewer")
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_reads_a_notification_that_can_never_run_as_none(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A notification is registered only with both an event and a command, so a
    row holding events and an empty command notifies on nothing."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly", cwd=agent_profile, notify_on=["failed"], notify_command=""
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


# ---------------------------------------------------------------------------
# A refusal offers no member edit for a value no member can state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_names_an_agent_identifier_the_launch_always_refuses(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """The launch checks the profile identifier whatever the kind, so a row holding
    one it refuses fails at every fire and no member can carry it."""
    doc = parse_schedule_set(
        _manifest_with_target("nightly", cwd=agent_profile, target=_PLAY_TARGET_BLOCK)
    )
    async with StateDB() as db:
        await db.create_schedule(_play_row(agent_profile, action_agent="-oops"))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "no member can declare the row's action_agent" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_names_an_empty_agent_identifier_on_an_agent_row_as_undeclarable(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """An agent member always names a profile, so a row that names none -- where the
    launch would fall back to its own default -- cannot be matched by an edit."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_agent="")
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "no member can declare the row's action_agent" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_reads_an_empty_profile_on_a_play_row_as_no_profile(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A play launch passes no profile on, so a row storing an empty one acts like a
    row storing none."""
    doc = parse_schedule_set(
        _manifest_with_target("nightly", cwd=agent_profile, target=_PLAY_TARGET_BLOCK)
    )
    async with StateDB() as db:
        await db.create_schedule(_play_row(agent_profile, action_agent=""))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


@pytest.mark.asyncio
async def test_adopt_names_a_missing_agent_prompt_as_one_no_member_declares(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """An agent member's prompt is a required string, so a row holding none cannot
    be matched by editing the member."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_prompt=None)
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "no member can declare the row's action_prompt" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_names_a_profile_the_execution_root_does_not_have(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A member naming the row's profile would fail to resolve from the row's own
    root, so no member can be edited to match this row."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_agent="ghost")
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "no member can declare the row's action_agent" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_still_offers_the_member_edit_for_a_profile_that_resolves(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """Control: a profile the execution root resolves is one the member can be
    edited to name, so that refusal keeps its member remedy."""
    (agent_profile / ".lionagi" / "agents" / "other.md").write_text(
        "---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n"
    )
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, action_agent="other")
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "action_agent: row='other' declared='reviewer'" in plan[0].detail
    assert "edit the member to match the row" in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_names_a_status_no_member_can_declare(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """The declaration checks the notify status vocabulary, so a row holding a
    status outside it cannot be matched by editing the member."""
    doc = parse_schedule_set(
        _agent_manifest("nightly", cwd=agent_profile, extra_member=_NOTIFY_BLOCK)
    )
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly", cwd=agent_profile, notify_on=["bogus"], notify_command="echo done"
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "no member can declare the row's notify_on" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_names_a_launch_kind_no_member_target_produces(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A member's target maps to agent, flow_yaml, play or command, so a row
    holding any other launch kind is not one a member can be edited into."""
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                action_kind="flow",
                action_model=None,
                action_prompt=None,
                action_agent=None,
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"]
    assert "no member can declare the row's action_kind" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


# ---------------------------------------------------------------------------
# A one-shot is compared against the fire the engine would make
# ---------------------------------------------------------------------------

_AT_PAST_TRIGGER = '      at: "2020-01-01T00:00:00+00:00"\n'
_CRON_TRIGGER_BLOCK = (
    '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n'
)
_RUN_ONCE_POLICIES = "    policies:\n      missedFire: run_once\n"


def _at_agent_manifest(*, cwd: Path, policies: str = "") -> ScheduleSetDocument:
    return parse_schedule_set(
        _agent_manifest("nightly", cwd=cwd, extra_member=policies).replace(
            _CRON_TRIGGER_BLOCK, _AT_PAST_TRIGGER
        )
    )


@pytest.mark.asyncio
async def test_adopt_refuses_a_due_one_shot_the_engine_would_recover(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A run_once row holding no instant has never been fired and will not fire;
    the member's own row is due and a recovery queues one fire for it, so the two
    do not behave alike and the row cannot be adopted as it stands."""
    doc = _at_agent_manifest(cwd=agent_profile, policies=_RUN_ONCE_POLICIES)
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="at",
        cron_expr=None,
        next_fire_at=None,
        max_runs=1,
        missed_fire_policy="run_once",
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"], plan[0].detail
    assert "next_fire_at: row=" in plan[0].detail
    # No member states a one-shot with no instant, so the remedy says the row cannot
    # be adopted as it stands rather than naming a member edit nobody can make.
    assert "no member can declare the row's next_fire_at" in plan[0].detail
    assert "cannot be adopted as it stands" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_refuses_a_one_shot_whose_run_cap_no_member_can_state(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A one-shot's run cap follows from its single instant, so the declaration
    always writes one and no member states a row with none."""
    doc = _at_agent_manifest(cwd=agent_profile)
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="at",
        cron_expr=None,
        next_fire_at=None,
        max_runs=None,
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ERROR"], plan[0].detail
    assert "max_runs: row=" in plan[0].detail
    assert "no member can declare the row's max_runs" in plan[0].detail
    assert "cannot be adopted as it stands" in plan[0].detail
    assert "edit the member to match the row" not in plan[0].detail


@pytest.mark.asyncio
async def test_adopt_still_reads_a_run_once_one_shot_that_already_fired(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """Control: the same row with its single fire in its history has spent the
    one-shot, so its missing instant is the member's own outcome."""
    doc = _at_agent_manifest(cwd=agent_profile, policies=_RUN_ONCE_POLICIES)
    row = _legacy_adoption_row(
        "nightly",
        cwd=agent_profile,
        trigger_type="at",
        cron_expr=None,
        next_fire_at=None,
        last_fired_at=time.time() - 3600,
        max_runs=1,
        missed_fire_policy="run_once",
    )
    async with StateDB() as db:
        await db.create_schedule(row)
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail


# ---------------------------------------------------------------------------
# The poll interval a github trigger reads at its own default
# ---------------------------------------------------------------------------

_GITHUB_TRIGGER_BLOCK = "      github:\n        repo: acme/widgets\n"


def _github_agent_manifest(*, cwd: Path) -> ScheduleSetDocument:
    return parse_schedule_set(
        _agent_manifest("nightly", cwd=cwd).replace(_CRON_TRIGGER_BLOCK, _GITHUB_TRIGGER_BLOCK)
    )


@pytest.mark.asyncio
async def test_adopt_accepts_a_github_poll_row_polling_at_the_default(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    """A github row whose poll interval is the default polls exactly as a row with
    no interval does, so clearing it on adoption changes no fire."""
    doc = _github_agent_manifest(cwd=agent_profile)
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                trigger_type="github_poll",
                cron_expr=None,
                effective_timezone=None,
                github_repo="acme/widgets",
                poll_interval_sec=300,
            )
        )
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
        assert [entry.action for entry in plan] == ["ADOPT"], plan[0].detail
        await apply_schedule_set(db, doc, agent_profile, adopt=True)
        adopted = await db.get_schedule_by_name("demo/nightly")
    assert adopted["poll_interval_sec"] is None


@pytest.mark.asyncio
async def test_adopt_and_reapply_preserve_github_poll_runtime_stagnation(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = _github_agent_manifest(cwd=agent_profile)
    runtime = {
        "last_healthy_poll_at": 1_700_000_000.0,
        "poller_consecutive_401": 2,
        "github_cursor": "retained-cursor",
    }
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row(
                "nightly",
                cwd=agent_profile,
                trigger_type="github_poll",
                cron_expr=None,
                effective_timezone=None,
                github_repo="acme/widgets",
                poll_interval_sec=300,
            )
        )
        await db.update_schedule("legacy00001", **runtime)
        for _ in range(3):
            assert await db.record_github_poll_stagnation("legacy00001", reset=False, page_limit=5)

        adopted = await apply_schedule_set(db, doc, agent_profile, adopt=True)
        assert adopted.adopted == 1
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["id"] == "legacy00001"
        assert row["poller_consecutive_cap_no_dispatch"] == 3
        assert row["poller_last_capped_page_limit"] == 5
        assert {name: row[name] for name in runtime} == runtime

        unchanged = await apply_schedule_set(db, doc, agent_profile)
        assert unchanged.unchanged == 1
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["poller_consecutive_cap_no_dispatch"] == 3
        assert row["poller_last_capped_page_limit"] == 5

        edited = parse_schedule_set(
            _agent_manifest("nightly", cwd=agent_profile)
            .replace(_CRON_TRIGGER_BLOCK, _GITHUB_TRIGGER_BLOCK)
            .replace('prompt: "check things"', 'prompt: "check things again"')
        )
        changed = await apply_schedule_set(db, edited, agent_profile)
        assert changed.updated == 1
        row = await db.get_schedule_by_name("demo/nightly")
        assert row["id"] == "legacy00001"
        assert row["action_prompt"] == "check things again"
        assert row["poller_consecutive_cap_no_dispatch"] == 3
        assert row["poller_last_capped_page_limit"] == 5
        assert {name: row[name] for name in runtime} == runtime


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


@pytest.mark.asyncio
async def test_adopt_clearing_unused_github_target_clears_observed_progress(
    temp_db_path, agent_profile, pinned_scheduler_tz
):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        await db.create_schedule(
            _legacy_adoption_row("nightly", cwd=agent_profile, github_repo="old/repo")
        )
        await db.execute(
            "UPDATE schedules SET poller_consecutive_cap_no_dispatch = 3, "
            "poller_last_capped_page_limit = 5 WHERE id = 'legacy00001'"
        )
        assert (await apply_schedule_set(db, doc, agent_profile, adopt=True)).adopted == 1
        row = await db.get_schedule("legacy00001")
        assert row["github_repo"] is None
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


def _member_manifest(
    *, cwd: Path, target: str = "agent", trigger: str = "cron", extra: str = ""
) -> str:
    manifest = _agent_manifest("nightly", cwd=cwd, extra_member=extra).replace(
        _CRON_TRIGGER_BLOCK, _TRIGGER_BLOCKS[trigger]
    )
    if target != "agent":
        manifest = manifest.replace(_AGENT_TARGET_BLOCK, _TARGET_BLOCKS[target])
    return manifest


def _member_row(*, cwd: Path, target: str = "agent", trigger: str = "cron", **overrides) -> dict:
    row = dict(_legacy_adoption_row("nightly", cwd=cwd))
    row.update(_TRIGGER_ROW_FIELDS[trigger])
    row.update(_TARGET_ROW_FIELDS[target])
    row.update(overrides)
    return row


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


def test_every_compared_column_has_a_case():
    from lionagi.studio.services.schedule_declaration import _LAUNCH_READERS

    assert set(_ADOPT_TABLE_CASES) == set(_LAUNCH_READERS), (
        "a table row without a case is a comparison nobody pinned, and a case "
        "without a table row pins a comparison that no longer exists"
    )


@pytest.mark.parametrize(
    "column,tag,member,row,expected",
    [(column, *case) for column, cases in sorted(_ADOPT_TABLE_CASES.items()) for case in cases],
)
@pytest.mark.asyncio
async def test_adoption_reads_a_column_only_under_the_launch_that_reads_it(
    column,
    tag,
    member,
    row,
    expected,
    temp_db_path,
    agent_profile,
    pinned_scheduler_tz,
    monkeypatch,
):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    flows = agent_profile / "flows"
    flows.mkdir(exist_ok=True)
    (flows / "nightly.yaml").write_text("workers: 2\n")

    member_target, member_trigger, extra = member
    row_target, row_trigger, overrides = row
    other_root = agent_profile / "other-root"
    other_root.mkdir(exist_ok=True)
    overrides = {
        key: (str(other_root) if value == "REPLACED-ROOT" else value)
        for key, value in overrides.items()
    }
    doc = parse_schedule_set(
        _member_manifest(
            cwd=agent_profile, target=member_target, trigger=member_trigger, extra=extra
        )
    )
    stored = _member_row(cwd=agent_profile, target=row_target, trigger=row_trigger, **overrides)
    async with StateDB() as db:
        for existing in await db.list_schedules():
            await db.delete_schedule(existing["id"])
        await db.create_schedule(dict(stored))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    assert plan[0].action == expected, f"{column}: {tag}: {plan[0].detail}"
    if expected == "ERROR":
        # The refusal names the column it read, and where no member could state the
        # stored value it says so instead of offering a member edit nobody can make.
        assert column in plan[0].detail, f"{column}: {tag}: {plan[0].detail}"


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
_TABLE_KINDS = {"agent": "agent", "flow_yaml": "flow", "play": "play", "command": "command"}
# The trigger types the table distinguishes, and for each the member manifest block
# and the stored row's trigger. The one-shot row is stored under the default
# missed-fire policy, so that policy is not the difference a one-shot case is about.
_TABLE_TRIGGERS = {
    "cron": ("cron", "cron"),
    "interval": ("interval", "interval"),
    "at": ("at", "at_skip"),
    "github_poll": ("github", "github"),
}


def _table_kind_cases() -> list[tuple[str, str]]:
    """(column, kind) for every column the table gives an action-kind set."""
    from lionagi.studio.services.schedule_declaration import _LAUNCH_READERS

    return [
        (column, kind)
        for column, (kinds, _triggers) in sorted(_LAUNCH_READERS.items())
        if kinds is not None and column in _DIFFERING_ROW_OVERRIDES
        for kind in _TABLE_KINDS
    ]


def _table_trigger_cases() -> list[tuple[str, str]]:
    """(column, trigger) for every column the table gives a trigger set."""
    from lionagi.studio.services.schedule_declaration import _LAUNCH_READERS

    return [
        (column, trigger)
        for column, (_kinds, triggers) in sorted(_LAUNCH_READERS.items())
        if triggers is not None and column in _DIFFERING_TRIGGER_ROW_OVERRIDES
        for trigger in _TABLE_TRIGGERS
    ]


_TABLE_KIND_CASES = _table_kind_cases()
_TABLE_TRIGGER_CASES = _table_trigger_cases()


def test_the_walks_pin_every_column_whose_reader_set_they_vary():
    """Each walk's pin covers every column the table still gives that axis a set
    for, and names only columns the walk can build a row for -- so a table entry
    the walk dropped a kind from cannot quietly leave the walk with no case."""
    from lionagi.studio.services.schedule_declaration import _LAUNCH_READERS

    kind_columns = {
        column for column, (kinds, _triggers) in _LAUNCH_READERS.items() if kinds is not None
    }
    trigger_columns = {
        column for column, (_kinds, triggers) in _LAUNCH_READERS.items() if triggers is not None
    }
    assert set(_PINNED_ACTION_KINDS) & kind_columns == kind_columns & set(_DIFFERING_ROW_OVERRIDES)
    assert set(_PINNED_READING_TRIGGERS) & trigger_columns == trigger_columns & set(
        _DIFFERING_TRIGGER_ROW_OVERRIDES
    )
    assert set(_TABLE_KIND_CASES) and set(_TABLE_TRIGGER_CASES)


@pytest.mark.parametrize(
    "column,kind",
    _TABLE_KIND_CASES,
    ids=[f"{column}-{kind}" for column, kind in _TABLE_KIND_CASES],
)
@pytest.mark.asyncio
async def test_a_row_of_a_kind_the_table_names_for_a_column_is_refused(
    column, kind, temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch
):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index,other-cmd")
    (agent_profile / ".lionagi" / "agents" / "other.md").write_text(
        "---\nmodel: anthropic/claude-sonnet-5\n---\nBody.\n"
    )
    flows = agent_profile / "flows"
    flows.mkdir(exist_ok=True)
    (flows / "nightly.yaml").write_text("workers: 2\n")

    target = _TABLE_KINDS[kind]
    doc = parse_schedule_set(_member_manifest(cwd=agent_profile, target=target))
    stored = _member_row(cwd=agent_profile, target=target, **_DIFFERING_ROW_OVERRIDES[column])
    async with StateDB() as db:
        for existing in await db.list_schedules():
            await db.delete_schedule(existing["id"])
        await db.create_schedule(dict(stored))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)

    if kind in _PINNED_ACTION_KINDS[column]:
        assert plan[0].action == "ERROR", f"{column}: {kind}: {plan[0].detail}"
        assert column in plan[0].detail, f"{column}: {kind}: {plan[0].detail}"
    else:
        assert plan[0].action == "ADOPT", f"{column}: {kind}: {plan[0].detail}"


@pytest.mark.parametrize(
    "column,trigger",
    _TABLE_TRIGGER_CASES,
    ids=[f"{column}-{trigger}" for column, trigger in _TABLE_TRIGGER_CASES],
)
@pytest.mark.asyncio
async def test_a_row_on_a_trigger_the_table_names_for_a_column_is_refused(
    column, trigger, temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch
):
    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")

    member_trigger, row_trigger = _TABLE_TRIGGERS[trigger]
    doc = parse_schedule_set(_member_manifest(cwd=agent_profile, trigger=member_trigger))
    stored = _member_row(
        cwd=agent_profile, trigger=row_trigger, **_DIFFERING_TRIGGER_ROW_OVERRIDES[column]
    )
    async with StateDB() as db:
        for existing in await db.list_schedules():
            await db.delete_schedule(existing["id"])
        await db.create_schedule(dict(stored))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)

    if trigger in _PINNED_READING_TRIGGERS[column]:
        assert plan[0].action == "ERROR", f"{column}: {trigger}: {plan[0].detail}"
        assert column in plan[0].detail, f"{column}: {trigger}: {plan[0].detail}"
    else:
        assert plan[0].action == "ADOPT", f"{column}: {trigger}: {plan[0].detail}"


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


@pytest.mark.parametrize("kind", sorted(_MISSING_OWN_COLUMN_CASES))
@pytest.mark.asyncio
async def test_adopt_names_a_row_missing_its_own_target_column_as_undeclarable(
    kind, temp_db_path, agent_profile, pinned_scheduler_tz, monkeypatch
):
    from lionagi.studio.services.schedule_declaration import _MEMBER_TARGET_COLUMNS

    monkeypatch.setenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", "refresh-index")
    flows = agent_profile / "flows"
    flows.mkdir(exist_ok=True)
    (flows / "nightly.yaml").write_text("workers: 2\n")

    target_block, overrides = _MISSING_OWN_COLUMN_CASES[kind]
    kind_of_row = overrides.get("action_kind", "agent")
    column = _MEMBER_TARGET_COLUMNS[kind_of_row]
    doc = parse_schedule_set(
        _manifest_with_target("nightly", cwd=agent_profile, target=target_block)
    )
    async with StateDB() as db:
        await db.create_schedule(_legacy_adoption_row("nightly", cwd=agent_profile, **overrides))
        plan, _ = await build_plan(db, doc, agent_profile, adopt=True)
    detail = plan[0].detail
    assert [entry.action for entry in plan] == ["ERROR"], detail
    assert f"no member can declare the row's {column}" in detail
    assert "cannot be adopted as it stands" in detail
    assert "edit the member to match the row" not in detail


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


@pytest.mark.parametrize(
    "column,kind",
    _COMPARABLE_KIND_CASES,
    ids=[f"{column}-{kind}" for column, kind in _COMPARABLE_KIND_CASES],
)
def test_the_comparison_acts_on_a_value_exactly_under_the_kinds_the_table_names(column, kind):
    value = _COMPARABLE_VALUES[column]
    acted_on = _adopt_comparable(column, {"action_kind": kind}, value)
    if kind in _PINNED_ACTION_KINDS[column]:
        assert acted_on == value, f"{column}: {kind}: a value the launch passes on was dropped"
    else:
        # A row of a kind the table names none for acts on no value in this column,
        # so two stored values act alike there whatever they say.
        assert acted_on is None, f"{column}: {kind}: {acted_on!r} was acted on"


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

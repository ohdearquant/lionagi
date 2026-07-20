# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Unit + DB-backed integration tests for the declarative ScheduleSet layer:
closed-schema validation, per-trigger/target static resolution, and the
atomic validate/diff/apply service."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")

from pydantic import ValidationError

from lionagi.state.db import StateDB
from lionagi.studio.services.schedule_declaration import (
    ScheduleSetDocument,
    ScheduleSetError,
    apply_schedule_set,
    build_plan,
    parse_schedule_set,
    resolve_schedule_set,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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
    monkeypatch.setattr(providers_mod, "_find_lionagi_dirs", lambda: [agents_dir.parent])
    return tmp_path


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


# ---------------------------------------------------------------------------
# Closed-schema rejection at every level
# ---------------------------------------------------------------------------


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
    so it keeps the text leniency; an alias reusing it inherits the same
    node and therefore the same outcome."""
    manifest = _notify_yaml_manifest("      &a on: [failed]\n      command: notify-run\n")
    doc = parse_schedule_set(manifest)
    assert doc.schedules["m"].notify.on == ["failed"]


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


# ---------------------------------------------------------------------------
# Trigger resolution
# ---------------------------------------------------------------------------


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
    import lionagi.studio.services.schedules as svc

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


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


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
    monkeypatch.delenv("LIONAGI_SCHEDULER_COMMAND_ALLOWLIST", raising=False)
    doc = parse_schedule_set(_every_manifest("1h", tmp_path))
    with pytest.raises(ScheduleSetError, match="allow"):
        resolve_schedule_set(doc, tmp_path)


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
        project: lionagi
    execution:
      cwd: {tmp_path}
"""
    )
    resolved = resolve_schedule_set(doc, tmp_path)
    assert resolved["audit"].resolved["target"] == {
        "kind": "playbook",
        "name": "health-audit",
        "args": {"project": "lionagi"},
    }


# ---------------------------------------------------------------------------
# cwd / project resolution
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Atomic validate/diff/apply service
# ---------------------------------------------------------------------------


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
    """Re-applying an 'at' member after it already fired (simulated here by
    the row reaching the auto-disabled, budget-exhausted state the engine's
    max_runs=1 gate leaves it in) must not error and must not resurrect a
    second run: the apply layer is free to re-arm next_fire_at/enabled --
    the fire-time claim-before-fire gate is what actually prevents a second
    fire (see tests/studio/test_scheduler_engine.py's max_runs gate test)."""
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
        assert row2["next_fire_at"] == row1["next_fire_at"]  # same deterministic epoch
        # The run history from the first fire is untouched -- apply never
        # deletes or rewrites schedule_runs.
        assert await db.count_schedule_runs(row1["id"], chain_depth=0) == 1


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
        with pytest.raises(ScheduleSetError, match="owned by"):
            await apply_schedule_set(db, doc, agent_profile)

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
        with pytest.raises(ScheduleSetError):
            await apply_schedule_set(db, doc, agent_profile)


@pytest.mark.asyncio
async def test_apply_adopt_flag_raises_clean_not_supported_error(temp_db_path, agent_profile):
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
        with pytest.raises(ScheduleSetError, match="adopt"):
            await apply_schedule_set(db, doc, agent_profile, adopt=True)
        rows = await db.list_schedules()
        assert len(rows) == 1  # zero writes


@pytest.mark.asyncio
async def test_dry_run_plan_never_writes(temp_db_path, agent_profile):
    doc = parse_schedule_set(_agent_manifest("nightly", cwd=agent_profile))
    async with StateDB() as db:
        plan, _resolved = await build_plan(db, doc, agent_profile)
        assert [(e.qualified_name, e.action) for e in plan] == [("demo/nightly", "CREATE")]
        rows = await db.list_schedules()
        assert rows == []

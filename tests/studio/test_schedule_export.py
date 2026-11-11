# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li schedule export`: legacy-row conversion (happy path per
action kind, on_success/on_fail BLOCKED, unsupported action_kind BLOCKED,
malformed trigger BLOCKED, round-trip through validate+apply to a fresh DB),
default declaration/cli re-export (authored_spec round-trip incl. a quoted
notify "on" key, deterministic ordering), and the CLI surface (--output vs
stdout, never writes the database)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")
pytest.importorskip("croniter", reason="studio extra not installed")

import yaml
from lionhub_tests.orchestration.schedule import _test_support_schedule_export as _support
from lionhub_tests.orchestration.schedule._test_support_schedule_export import (
    _legacy_row,
)

# Fixtures shared with the support module, bound here so they apply to this module only.
agent_profile = _support.agent_profile
temp_db_path = _support.temp_db_path

from lionagi.state.db import StateDB
from lionhub.orchestration.schedule.declaration import (
    apply_schedule_set,
    build_plan,
    parse_schedule_set,
    resolve_schedule_set,
)
from lionhub.orchestration.schedule.export import (
    build_managed_export_document,
    convert_legacy_rows,
    dump_schedule_set_yaml,
    format_report,
)

# fixtures


async def _create_legacy_row(row: dict) -> None:
    async with StateDB() as db:
        await db.create_schedule(row)


# Legacy conversion — happy path per action kind


# Legacy conversion — BLOCKED cases


# Round-trip: export legacy -> validate -> apply to a fresh DB -> compare


# Default export — authored_spec round-trip incl. quoted notify "on"


# CLI surface — --output vs stdout, never writes the database


def _export_args(**overrides) -> types.SimpleNamespace:
    base = {"legacy": False, "output": None, "report": None}
    base.update(overrides)
    return types.SimpleNamespace(**base)


# Mixed-project export -- one document per project, exact qualified-name
# round-trip (no double-qualification)


# flow_yaml action_model override -- BLOCKED, not silently dropped


# action_extra_args -- BLOCKED, not silently dropped


# github_poll poll_interval_sec -- BLOCKED only when set and non-default


# CLI exit code -- 2 when any row is BLOCKED, document + report still emitted


# Flow snapshot portability -- sidecar path is relative, YAML carries no
# host prefix, absolute cwd is flagged on the report line


# Rows with no stored project — name identity across re-apply


# GitHub cadence fallback and sibling-file collisions


# The columns adopting a legacy row writes: the owner and the declaration it adopted.
ADOPTION_COLUMNS = {
    "authored_spec",
    "managed_by",
    "owner_key",
    "resolved_digest",
    "resolved_target",
    "resolved_timezone",
    "spec_version",
    "updated_at",
}

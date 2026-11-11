# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The scheduler service validates ``on_success`` / ``on_fail`` chain actions at
create and at update with the same rules, and the same message text, as the
``li schedule`` command-line parser."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi", reason="studio extra not installed")

from unittest.mock import AsyncMock, patch

from lionagi.studio.services.schedules import create_schedule, update_schedule
from lionhub.orchestration.schedule.parsing import (
    _MAX_CHAIN_DEPTH,
    _parse_chain_action,
    _validate_chain_kinds,
)

FIELDS = ("on_success", "on_fail")


def _flag(field: str) -> str:
    return "--" + field.replace("_", "-")


def _chain(field: str, nodes: int) -> dict:
    """A chain of ``nodes`` actions, each nested in the previous one's ``field``;
    the innermost explicitly nulls its own ``field`` so nothing warns."""
    node: dict = {"prompt": "step", field: None}
    for _ in range(nodes - 1):
        node = {"prompt": "step", field: node}
    return node


# (case id, builder from field name to chain action, expected message fragment or None)
CASES = [
    (
        "unknown-key-depth-1",
        lambda f: {"prompt": "x", f: None, "trigger_type": "interval"},
        "unknown key",
    ),
    (
        "unknown-key-depth-2",
        lambda f: {"prompt": "x", f: {"prompt": "y", f: None, "trigger_type": "interval"}},
        "unknown key",
    ),
    ("non-object", lambda f: [1, 2, 3], "must be a JSON object"),
    ("valid-nested", lambda f: _chain(f, 2), None),
    ("valid-at-depth-limit", lambda f: _chain(f, _MAX_CHAIN_DEPTH), None),
    ("nested-past-depth-limit", lambda f: _chain(f, _MAX_CHAIN_DEPTH + 1), "depth limit"),
]

CASE_PARAMS = [pytest.param(c[1], c[2], id=c[0]) for c in CASES]


def _create_data(**overrides) -> dict:
    base = {
        "name": "chain-validation-test",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_prompt": "ping",
    }
    base.update(overrides)
    return base


def _existing(**overrides) -> dict:
    base = {
        "id": "sid-chain-1",
        "name": "chain-validation-patch",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
    }
    base.update(overrides)
    return base


def _run_create(data: dict):
    """Run create_schedule against a stubbed store; return (store, ValueError | None)."""

    async def _go():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.create_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                await create_schedule(data)
            except ValueError as exc:
                return mock_db, exc
            return mock_db, None

    return asyncio.run(_go())


def _run_update(fields: dict, existing: dict | None = None):
    """Run update_schedule against a stubbed store; return (store, ValueError | None)."""
    row = existing if existing is not None else _existing()

    async def _go():
        with patch("lionagi.studio.services.schedules.StateDB") as MockDB:
            mock_db = AsyncMock()
            mock_db.get_schedule = AsyncMock(return_value=row)
            mock_db.update_schedule = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                await update_schedule(row["id"], fields)
            except ValueError as exc:
                return mock_db, exc
            return mock_db, None

    return asyncio.run(_go())


# create_schedule


# update_schedule


# the command-line parser and the service agree


# a value nested at the depth limit, and one level below it


def _nest_at(field: str, depth: int, value) -> dict:
    """A chain of ``depth`` actions whose deepest one carries ``value`` as its own ``field``."""
    node: dict = {"prompt": "step", field: value}
    for _ in range(depth - 1):
        node = {"prompt": "step", field: node}
    return node


def _nest_label(field: str, depth: int) -> str:
    """The label the validator gives the action at ``depth`` in a ``_nest_at`` chain."""
    return _flag(field) + f".{field}" * (depth - 1)


# (case id, builder from field name to the nested value, type name when it is not an object)
NESTED_VALUE_CASES = [
    ("empty-object", lambda f: {}, None),
    ("empty-list", lambda f: [], "list"),
    ("zero", lambda f: 0, "int"),
    ("empty-string", lambda f: "", "str"),
    ("false", lambda f: False, "bool"),
    ("object", lambda f: {"prompt": "leaf", f: None}, None),
]

NESTED_VALUE_PARAMS = [pytest.param(c[1], c[2], id=c[0]) for c in NESTED_VALUE_CASES]


# the chain-fire merge overlays only the keys a chain action may carry


def _parent_schedule() -> dict:
    return {
        "id": "sid-parent",
        "name": "parent",
        "trigger_type": "cron",
        "action_kind": "agent",
        "action_model": "base-model",
        "action_prompt": "base prompt",
        "action_agent": "base-agent",
        "action_playbook": "base-playbook",
        "action_cwd": "/work/parent",
        "action_command": "git",
        "on_success": {"prompt": "old"},
        "on_fail": None,
    }


def test_merge_keeps_parent_action_cwd_when_stored_chain_carries_one():
    from lionhub.orchestration.scheduler.engine import _merge_chain_action

    merged = _merge_chain_action(
        _parent_schedule(), {"prompt": "next", "action_cwd": "/somewhere/else"}
    )
    assert merged["action_cwd"] == "/work/parent"
    assert merged["action_prompt"] == "next"


@pytest.mark.parametrize(
    "key, value",
    [
        ("action_cwd", "/somewhere/else"),
        ("action_command", "rm"),
        ("action_prompt", "smuggled"),
        ("id", "other-id"),
        ("name", "other-name"),
        ("trigger_type", "interval"),
        ("enabled", False),
    ],
)
def test_merge_ignores_stored_chain_keys_that_are_not_allowed(key, value):
    from lionhub.orchestration.scheduler.engine import _merge_chain_action

    parent = _parent_schedule()
    merged = _merge_chain_action(parent, {"model": "m2", key: value})
    assert merged.get(key) == parent.get(key)
    assert merged["action_model"] == "m2"


def test_merge_applies_every_allowed_key():
    from lionhub.orchestration.schedule.parsing import _CHAIN_ACTION_ALLOWED_KEYS
    from lionhub.orchestration.scheduler.engine import _merge_chain_action

    nested = {"prompt": "after", "on_success": None}
    chain_action = {
        "kind": "play",
        "action_kind": "flow",
        "model": "m2",
        "prompt": "next",
        "agent": "next-agent",
        "playbook": "next-playbook",
        "on_success": nested,
        "on_fail": {"prompt": "recover", "on_fail": None},
    }
    assert set(chain_action) == _CHAIN_ACTION_ALLOWED_KEYS

    parent = _parent_schedule()
    merged = _merge_chain_action(parent, chain_action)
    assert merged["action_kind"] == "play", "kind wins over action_kind"
    assert merged["action_model"] == "m2"
    assert merged["action_prompt"] == "next"
    assert merged["action_agent"] == "next-agent"
    assert merged["action_playbook"] == "next-playbook"
    assert merged["on_success"] is nested
    assert merged["on_fail"] == {"prompt": "recover", "on_fail": None}
    assert parent["on_success"] == {"prompt": "old"}, "the parent schedule is not modified"


def test_scheduler_and_admission_share_chain_merge():
    from lionhub.orchestration.schedule.parsing import _merge_chain_action as admission_merge
    from lionhub.orchestration.scheduler.engine import _merge_chain_action as scheduler_merge

    assert scheduler_merge is admission_merge

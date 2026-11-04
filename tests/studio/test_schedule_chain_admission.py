"""Admission checks follow the effective values of chained schedule actions."""

from __future__ import annotations

import pytest

from tests.studio.test_schedule_chain_validation import (
    _create_data,
    _existing,
    _run_create,
    _run_update,
)


def _chain(depth: int, **leaf_fields) -> dict:
    node = {"kind": "agent", "on_success": None, "on_fail": None, **leaf_fields}
    if depth == 2:
        node = {"kind": "agent", "on_success": None, "on_fail": node}
    return node


def _write_with_chain(operation: str, chain: dict, **root_fields):
    if operation == "create":
        db, error = _run_create(_create_data(on_fail=chain, **root_fields))
        return db.create_schedule, error
    stored = _existing(**root_fields)
    db, error = _run_update({"on_fail": chain}, existing=stored)
    return db.update_schedule, error


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("field", ["model", "agent", "playbook"])
def test_chain_rejects_invalid_values_at_every_depth(operation, depth, field):
    write, error = _write_with_chain(operation, _chain(depth, **{field: "-invalid"}))
    assert error is not None
    assert ("on_fail.on_fail" if depth == 2 else "on_fail") in str(error)
    assert field in str(error)
    write.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize(
    "root_fields",
    [
        pytest.param({"action_playbook": "list"}, id="inherited-playbook"),
        pytest.param({"action_extra_args": ["list"]}, id="inherited-extra-args"),
        pytest.param(
            {"action_prompt": "-invalid", "action_playbook": "triage"},
            id="inherited-play-prompt",
        ),
    ],
)
def test_play_chain_rejects_invalid_inherited_launch_values(operation, depth, root_fields):
    write, error = _write_with_chain(operation, _chain(depth, kind="play"), **root_fields)
    assert error is not None
    assert ("on_fail.on_fail" if depth == 2 else "on_fail") in str(error)
    write.assert_not_called()


@pytest.mark.parametrize("field", ["model", "agent", "playbook"])
def test_new_chain_rejects_legacy_invalid_inherited_identifier(field):
    stored = _existing(**{f"action_{field}": "-invalid"})
    db, error = _run_update({"on_fail": _chain(1)}, existing=stored)
    assert error is not None and "on_fail" in str(error) and field in str(error)
    db.update_schedule.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [("action_playbook", "list"), ("action_extra_args", ["list"])],
)
def test_root_update_revalidates_inherited_play_target(field, value):
    stored = _existing(on_fail=_chain(1, kind="play"))
    db, error = _run_update({field: value}, existing=stored)
    assert error is not None and "on_fail" in str(error)
    db.update_schedule.assert_not_called()


def test_root_prompt_update_revalidates_a_play_child():
    stored = _existing(on_fail=_chain(1, kind="play", playbook="triage"))
    db, error = _run_update({"action_prompt": "-invalid"}, existing=stored)
    assert error is not None and "on_fail" in str(error)
    db.update_schedule.assert_not_called()


def test_kind_update_revalidates_an_inherited_play_child():
    stored = _existing(
        action_prompt="-invalid",
        on_fail={"playbook": "triage", "on_success": None, "on_fail": None},
    )
    db, error = _run_update({"action_kind": "play"}, existing=stored)
    assert error is not None and "on_fail" in str(error)
    db.update_schedule.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "update"])
def test_unsent_play_prompt_may_start_with_dash(operation):
    write, error = _write_with_chain(operation, _chain(1, kind="play", prompt="-unsent"))
    assert error is None
    write.assert_awaited_once()


def test_unrelated_edit_and_clear_can_repair_legacy_values():
    stored = _existing(on_fail=_chain(1, model="-legacy"))
    db, error = _run_update({"description": "updated"}, existing=stored)
    assert error is None
    db.update_schedule.assert_awaited_once_with("sid-chain-1", description="updated")

    db, error = _run_update({"on_fail": None}, existing=stored)
    assert error is None
    db.update_schedule.assert_awaited_once_with("sid-chain-1", on_fail=None)

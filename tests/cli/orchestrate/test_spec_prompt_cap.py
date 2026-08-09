# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Three surfaces validate a spec's prompt: the CLI, the Studio playbook
service, and the Studio schedule service. The bound they check against used to
be written out three times, so there was no single place to raise it and no
guarantee the three agreed.

The bound is for the pathological file, not the long prompt. An orchestration
prompt carries the whole task, and at the old 8192 characters a real one had
already been squeezed to fit — close enough to normal writing that an ordinary
edit to a working spec could kill the run at submit.

The schedule surface takes YAML text rather than a mapping, so it is called
through a wrapper here. That difference is the reason the three could drift
apart unnoticed, which is what these tests are for.
"""

from __future__ import annotations

import yaml

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.cli.orchestrate import _FLOW_SPEC_FIELDS, _validate_spec_fields
from lionagi.studio.services.playbooks import _check_spec_fields
from lionagi.studio.services.schedules import _validate_flow_yaml_spec


def _schedule_spec(spec: dict) -> str | None:
    """Feed the schedule surface the same spec through the shape it accepts."""
    return _validate_flow_yaml_spec(yaml.safe_dump(spec, width=10**9))


ALL_SURFACES = (
    ("cli", _validate_spec_fields),
    ("playbook", _check_spec_fields),
    ("schedule", _schedule_spec),
)


VALID_FIELD_VALUES = {
    "agent": "test-profile",
    "argument-hint": "[--mode MODE]",
    "args": {"mode": {"type": "str"}},
    "artifacts": {"expected": [{"id": "report", "path": "report.md"}]},
    "bare": True,
    "bypass": True,
    "description": "Review a target",
    "dry_run": False,
    "effort": "high",
    "max_agents": 0,
    "max_ops": 0,
    "model": "claude-code/opus-4-7",
    "name": "repo-review",
    "pack": "./routing.yaml",
    "permission_mode": "acceptEdits",
    "prompt": "Do the thing",
    "reactive": "off",
    "save": "./results",
    "show_graph": True,
    "team_attach": "existing-team",
    "team_mode": "new-team",
    "with_synthesis": True,
    "workers": 1,
    "yolo": True,
}


def test_every_declared_field_is_accepted_by_every_surface():
    assert frozenset(VALID_FIELD_VALUES) == _FLOW_SPEC_FIELDS

    for field, value in VALID_FIELD_VALUES.items():
        errors = [validate({field: value}) for _, validate in ALL_SURFACES]
        assert errors == [None] * len(ALL_SURFACES), (field, errors)


def test_every_surface_rejects_an_unknown_field_with_the_same_error():
    errors = [validate({"not_a_flow_field": True}) for _, validate in ALL_SURFACES]

    assert all(error is not None for error in errors), errors
    assert len(set(errors)) == 1, errors


def test_every_surface_returns_the_same_field_error():
    invalid_specs = (
        {"workers": 33},
        {"max_ops": 51},
        {"effort": "impossible"},
        {"with_synthesis": []},
        {"bare": "true"},
        {"prompt": 42},
        {"save": 7},
        {"model": 42},
        {"artifacts": None},
    )

    for spec in invalid_specs:
        errors = [validate(spec) for _, validate in ALL_SURFACES]
        assert all(error is not None for error in errors), (spec, errors)
        assert len(set(errors)) == 1, (spec, errors)


def test_the_bound_is_far_from_anything_a_prompt_reaches():
    """The old 8192 was reachable by writing. This one is not: a prompt would
    have to be a file that is not a prompt."""
    assert MAX_SPEC_PROMPT_CHARS == 256 * 1024


def test_a_prompt_that_the_old_cap_refused_is_accepted_everywhere():
    """The squeeze this fixes: a working playbook cut down to fit 8192."""
    prompt = "x" * 20000

    for name, validate in ALL_SURFACES:
        assert validate({"prompt": prompt}) is None, name


def test_every_surface_refuses_at_the_same_length():
    """The point of one constant. Three copies could drift, and a spec accepted
    by the CLI but refused by a schedule fails at the least useful moment."""
    over = "x" * (MAX_SPEC_PROMPT_CHARS + 1)

    for name, validate in ALL_SURFACES:
        error = validate({"prompt": over})
        assert error is not None, name
        assert str(MAX_SPEC_PROMPT_CHARS) in error, name


def test_a_prompt_exactly_at_the_bound_is_accepted():
    at = "x" * MAX_SPEC_PROMPT_CHARS

    for name, validate in ALL_SURFACES:
        assert validate({"prompt": at}) is None, name


def test_the_message_names_the_real_number():
    """A refusal that names a stale number sends the author to trim against a
    bound that is not the one being enforced."""
    error = _validate_spec_fields({"prompt": "x" * (MAX_SPEC_PROMPT_CHARS + 1)})

    assert "8192" not in error
    assert str(MAX_SPEC_PROMPT_CHARS) in error


def test_a_non_string_prompt_is_still_refused():
    """Raising the bound does not loosen the type check that sits above it."""
    for name, validate in ALL_SURFACES:
        error = validate({"prompt": 12345})
        assert error is not None, name
        assert "string" in error, name

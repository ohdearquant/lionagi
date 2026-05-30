# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Role (lionagi/casts/pattern.py), inline-Python roles, and the default Pack."""

from pathlib import Path

import pytest

from lionagi.casts.emission import Finding, Verdict
from lionagi.casts.pack import Pack, RolePolicy
from lionagi.casts.pattern import PatternKind, Role, list_roles

_ROOT = Path(__file__).parents[2]
DEFAULT_PACK = _ROOT / "lionagi/casts/packs/default.yaml"


def test_all_roles_load():
    names = list_roles()
    assert len(names) >= 38
    for n in names:
        r = Role.load(n)
        assert r.name == n, n
        assert r.description, f"{n} missing description"
        assert r.body, f"{n} missing body"
        assert r.kind == PatternKind.ROLE


def test_role_body_has_no_operational_sections():
    # Authority / Boundaries / Escalations live in the pack, not the prompt body.
    for n in list_roles():
        body = Role.load(n).body
        for section in ("## Authority", "## Boundaries", "## Escalations"):
            assert section not in body, f"{n} still carries {section}"


def test_role_is_frozen():
    r = Role.load("critic")
    with pytest.raises(AttributeError):
        r.name = "x"


def test_role_to_dict_excludes_empty():
    r = Role(name="x", description="d")  # body + emits default empty
    d = r.to_dict()
    assert "body" not in d
    assert "emits" not in d
    assert set(d) == {"name", "description"}


def test_role_emits_serialized_as_names():
    d = Role.load("critic").to_dict()
    assert d["emits"] == ["Verdict", "Finding"]


def test_role_emission_contract():
    critic = Role.load("critic")
    assert critic.emits == (Verdict, Finding)
    # build the Operable — present for any role that emits
    op = critic.emission_operable()
    assert op is not None
    # a role with no emission contract yields no operable
    assert Role(name="x", description="d").emission_operable() is None


def test_pack_loads_policy():
    p = Pack.from_file(DEFAULT_PACK)
    assert p.name == "default"
    critic = p.policy("critic")
    assert isinstance(critic, RolePolicy)
    assert critic.authority and critic.boundaries and critic.escalations
    # escalations are prose conditions (no routing target yet)
    assert all(isinstance(e, str) and e for e in critic.escalations)


def test_every_role_has_a_pack_entry():
    p = Pack.from_file(DEFAULT_PACK)
    role_names = set(list_roles())
    assert role_names == set(p.policies), role_names ^ set(p.policies)

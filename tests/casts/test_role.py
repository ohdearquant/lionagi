# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for Role (lionagi/casts/pattern.py) and the default Pack."""

from pathlib import Path

import pytest

from lionagi.casts.pack import Pack, RolePolicy
from lionagi.casts.pattern import PatternKind, Role

_ROOT = Path(__file__).parents[2]
ROLES_DIR = _ROOT / "lionagi/casts/roles"
DEFAULT_PACK = _ROOT / "lionagi/casts/packs/default.yaml"


def _role_files():
    return [p for p in sorted(ROLES_DIR.glob("*.md")) if p.name != "TEMPLATE.md"]


def test_all_roles_load():
    files = _role_files()
    assert len(files) >= 38
    for f in files:
        r = Role.from_file(f)
        assert r.name, f
        assert r.description, f"{f.name} missing description"
        assert r.body, f"{f.name} missing body"
        assert r.kind == PatternKind.ROLE


def test_role_body_has_no_operational_sections():
    # Authority / Boundaries / Escalations live in the pack, not the prompt body.
    for f in _role_files():
        body = Role.from_file(f).body
        for section in ("## Authority", "## Boundaries", "## Escalations"):
            assert section not in body, f"{f.name} still carries {section}"


def test_role_is_frozen():
    r = Role.from_file(ROLES_DIR / "critic.md")
    with pytest.raises(AttributeError):
        r.name = "x"


def test_role_to_dict_excludes_empty():
    r = Role(name="x", description="d")  # body defaults empty
    d = r.to_dict()
    assert "body" not in d
    assert set(d) == {"name", "description"}


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
    role_names = {Role.from_file(f).name for f in _role_files()}
    assert role_names == set(p.policies), role_names ^ set(p.policies)

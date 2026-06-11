# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read-only casts catalog — roles, modes, and their emission contracts."""

from __future__ import annotations

from .pattern import Mode, Role, list_modes, list_roles

__all__ = ("build_catalog",)


def _role_entry(role: Role) -> dict:
    return {
        "name": role.name,
        "description": role.description,
        "emits": [m.__name__ for m in role.emits] if role.emits else [],
        "body": role.body,
    }


def _mode_entry(mode: Mode) -> dict:
    return {
        "name": mode.name,
        "description": mode.description,
        "behaviors": mode.behaviors,
        "conflicts_with": sorted(mode.conflicts_with),
    }


def build_catalog() -> dict:
    """Return the full casts catalog as a plain dict."""
    roles = [_role_entry(Role.load(n)) for n in list_roles()]
    modes = [_mode_entry(Mode.load(n)) for n in list_modes()]
    return {"roles": roles, "modes": modes}

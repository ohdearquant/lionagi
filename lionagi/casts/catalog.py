# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read-only casts catalog — roles, modes, and their emission contracts.

This module is the documented seam for metadata inspection. It is data-only:
nothing here mutates casts state or executes agent behavior.
"""

from __future__ import annotations

from .pattern import Mode, Role, list_modes, list_roles

__all__ = ("build_catalog",)


def _load_default_pack():
    """Load the default pack; returns None if unavailable."""
    try:
        from importlib.resources import as_file, files

        from .pack import Pack

        packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
        with as_file(packaged) as p:
            return Pack.from_file(p)
    except Exception:
        return None


def _emission_entries(role: Role) -> list[dict]:
    """Derive emission entries from role.emission_operable() — the real port surface.

    Each entry exposes {model: "PascalCase", key: "snake_case"}.
    Includes EscalationRequest for every emitting role (injected by
    build_emission_operable) and is absent for roles with no emits.
    """
    op = role.emission_operable()
    if op is None:
        return []
    return [{"model": spec.base_type.__name__, "key": spec.name} for spec in op.get_specs()]


def _role_config_entry(role_name: str, pack) -> dict | None:
    if pack is None:
        return None
    cfg = pack.config(role_name)
    pol = pack.policy(role_name)
    if cfg is None and pol is None:
        return None
    entry: dict = {}
    if cfg is not None:
        entry["active"] = cfg.active
        entry["model"] = cfg.model
        entry["effort"] = cfg.effort
        entry["default_modes"] = list(cfg.default_modes)
        entry["modes_allow"] = list(cfg.modes_allow)
    if pol is not None:
        entry["authority"] = list(pol.authority)
        entry["boundaries"] = list(pol.boundaries)
        entry["escalations"] = list(pol.escalations)
    return entry


def _role_entry(role: Role, pack) -> dict:
    return {
        "name": role.name,
        "description": role.description,
        "emits": _emission_entries(role),
        "body": role.body,
        "config": _role_config_entry(role.name, pack),
    }


def _mode_entry(mode: Mode) -> dict:
    return {
        "name": mode.name,
        "description": mode.description,
        "behaviors": mode.behaviors,
        "conflicts_with": sorted(mode.conflicts_with),
    }


def build_catalog() -> dict:
    """Return the full casts catalog as a plain dict.

    Roles include their real emission port surface (derived from
    emission_operable, which adds EscalationRequest implicitly) and
    the default-pack config overlay (policy + runtime config). Modes
    include conflict declarations. The pack section is null per role
    when the default pack cannot be loaded.
    """
    pack = _load_default_pack()
    roles = [_role_entry(Role.load(n), pack) for n in list_roles()]
    modes = [_mode_entry(Mode.load(n)) for n in list_modes()]
    return {"roles": roles, "modes": modes}

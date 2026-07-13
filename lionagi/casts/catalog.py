# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Read-only catalog of built-in casts roles, modes, and emission contracts."""

from __future__ import annotations

from .pattern import Mode, Role, list_modes, list_roles

__all__ = ("build_catalog",)


def _load_packaged_pack(*, raise_on_error: bool = False):
    """Load the packaged default pack from the lionagi.casts resource tree.
    Raises if raise_on_error is True, otherwise returns None on failure."""
    try:
        from importlib.resources import as_file, files

        from .pack import Pack

        packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
        with as_file(packaged) as p:
            return Pack.from_file(p)
    except Exception:
        if raise_on_error:
            raise
        return None


def _load_default_pack():
    """Load the default pack; returns None if unavailable."""
    return _load_packaged_pack(raise_on_error=False)


def _emission_entries(role: Role) -> list[dict]:
    """Return [{model, key}] entries from the role's emission operable; empty list if none."""
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
    """Return the full casts catalog as a plain dict with roles, modes, and default-pack overlays."""
    pack = _load_default_pack()
    roles = [_role_entry(Role.load(n), pack) for n in list_roles()]
    modes = [_mode_entry(Mode.load(n)) for n in list_modes()]
    return {"roles": roles, "modes": modes}

# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.ln._lazy_init import lazy_import

_LAZY_MAP: dict[str, tuple[str, str | None]] = {
    "AgentSpec": ("spec", None),
    "HooksMixin": ("spec", None),
    "PermissionPolicy": ("permissions", None),
    "create_agent": ("factory", None),
    "load_settings": ("settings", None),
    "apply_hooks_from_settings": ("settings", None),
}


def __getattr__(name: str) -> object:
    return lazy_import(name, _LAZY_MAP, __name__, globals())


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = (
    "AgentSpec",
    "HooksMixin",
    "PermissionPolicy",
    "create_agent",
    "load_settings",
    "apply_hooks_from_settings",
)

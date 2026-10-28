# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Name-only credential metadata and environments for coding commands."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from types import UnionType
from typing import Annotated, ForwardRef, Union, get_args, get_origin

__all__ = (
    "credential_env_names",
    "normalize_command_env_allowlist",
    "scrub_command_env",
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPLICIT_AUTH_NAMES = frozenset({"GITHUB_TOKEN", "LIONAGI_STUDIO_AUTH_TOKEN"})
_SHELL_STARTUP_NAMES = frozenset({"BASH_ENV", "ENV"})


def _literal_names(names: Iterable[str], *, label: str) -> tuple[str, ...]:
    if isinstance(names, (str, bytes, Mapping)) or not isinstance(names, Iterable):
        raise TypeError(f"{label} must contain literal environment variable names")
    result: dict[str, None] = {}
    for name in names:
        if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
            raise ValueError(f"{label} must contain portable literal environment variable names")
        result[name] = None
    return tuple(result)


def normalize_command_env_allowlist(names: Sequence[str]) -> tuple[str, ...]:
    """Validate operator-owned names without changing spelling or displaying values."""
    if not isinstance(names, Sequence):
        raise TypeError("command_env_allowlist must be a sequence of literal variable names")
    return _literal_names(names, label="command_env_allowlist")


def _is_secret_annotation(annotation: object, secret_type: type) -> bool:
    if isinstance(annotation, (str, ForwardRef)):
        raise ValueError("settings field annotations must already be resolved")
    origin = get_origin(annotation)
    if origin is Annotated:
        return _is_secret_annotation(get_args(annotation)[0], secret_type)
    if origin in (Union, UnionType):
        # Visit every member so an unresolved annotation is never silently ignored.
        members = [_is_secret_annotation(item, secret_type) for item in get_args(annotation)]
        return any(members)
    return isinstance(annotation, type) and issubclass(annotation, secret_type)


def credential_env_names(
    *,
    settings_type: type,
    provider_env_names: Iterable[str] = (),
    account_env_names: Iterable[str] = (),
    lookup_names: Iterable[str] = (),
    mcp_auth_env_names: Iterable[str] = (),
    active_reference_names: Iterable[str] = (),
) -> frozenset[str]:
    """Union declared names without constructing settings or resolving any credential.

    Callers supply provider, account, global lookup and selected MCP metadata.
    Secret settings fields must use resolved scalar, nullable or Annotated
    SecretStr annotations and ordinary unprefixed, unaliased environment names.
    """
    from pydantic import SecretStr

    if not isinstance(settings_type, type):
        raise TypeError("settings_type must be an existing settings class")
    fields = getattr(settings_type, "model_fields", None)
    config = getattr(settings_type, "model_config", {})
    if not isinstance(fields, Mapping) or not isinstance(config, Mapping):
        raise TypeError("settings_type must expose resolved field and configuration metadata")
    if config.get("env_prefix", "") not in (None, ""):
        raise ValueError("prefixed settings credential names require explicit metadata support")

    names = set(_EXPLICIT_AUTH_NAMES)
    for field_name, field in fields.items():
        if not _is_secret_annotation(field.annotation, SecretStr):
            continue
        if any(
            getattr(field, attribute, None) is not None
            for attribute in ("alias", "validation_alias", "serialization_alias")
        ):
            raise ValueError("aliased settings credential names require explicit metadata support")
        names.update(_literal_names((field_name,), label="settings credential names"))

    for supplied in (
        provider_env_names,
        account_env_names,
        lookup_names,
        mcp_auth_env_names,
        active_reference_names,
    ):
        names.update(_literal_names(supplied, label="credential metadata"))
    return frozenset(name.upper() for name in names)


def scrub_command_env(
    parent_env: Mapping[str, str],
    *,
    declared_names: Iterable[str],
    command_env_allowlist: Sequence[str] = (),
) -> dict[str, str]:
    """Copy a supplied environment with default denial and exact operator grants.

    Removal matches declarations, shared sensitive-name patterns and shell
    startup names without regard to case. Grants match only the exact input
    key. The state-store URL remains excluded even when explicitly granted.
    """
    from lionagi.ln._proc import env_without_state_db_url
    from lionagi.service.connections.mcp_wrapper import _SENSITIVE_ENV_PATTERNS

    if not isinstance(parent_env, Mapping):
        raise TypeError("parent_env must be an environment mapping")
    declared = frozenset(
        name.upper() for name in _literal_names(declared_names, label="credential metadata")
    )
    allowed = normalize_command_env_allowlist(command_env_allowlist)
    result: dict[str, str] = {}
    for name, value in parent_env.items():
        if not isinstance(name, str):
            raise TypeError("parent_env must have string variable names")
        folded = name.upper()
        denied = (
            folded in declared
            or folded in _SHELL_STARTUP_NAMES
            or any(pattern in folded for pattern in _SENSITIVE_ENV_PATTERNS)
        )
        if not denied or name in allowed:
            result[name] = value
    return env_without_state_db_url(result)

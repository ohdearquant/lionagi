# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Supply declared command-environment names from their owned metadata sources."""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

__all__ = ("configured_command_env_names", "command_environment")


def configured_command_env_names(
    *,
    active_reference_names: Iterable[str] = (),
    mcp_auth_env_names: Iterable[str] = (),
) -> frozenset[str]:
    """Read existing declarations without resolving or retrieving credentials.

    Read settings class annotations, registered provider key names, account key
    names, and global lookup names. Credential values are never read. Global
    lookup configuration is loaded separately from project-local hook settings.
    """
    from lionagi._command_env import credential_env_names
    from lionagi._errors import ConfigurationError
    from lionagi._settings import load_settings
    from lionagi.config import settings
    from lionagi.service.accounts._secret_resolution import resolve_secret_lookup_config
    from lionagi.service.connections.registry import EndpointRegistry

    try:
        global_settings = load_settings(include_project=False)
    except Exception:
        # Not chained: a parse error can quote the file, and the file can hold values.
        raise ConfigurationError(
            "~/.lionagi/settings.yaml could not be read, so the credential names coding "
            "commands must remove are unknown; fix or remove the file"
        ) from None
    lookup = resolve_secret_lookup_config(settings=global_settings)
    if lookup.reason is not None:
        raise ConfigurationError(
            f"The secrets.lookup section of ~/.lionagi/settings.yaml is invalid ({lookup.reason}), "
            "so the credential names coding commands must remove are unknown; fix or remove "
            "that section"
        )

    with EndpointRegistry._lock:
        provider_names = tuple(
            entry.meta.api_key_env
            for entry in EndpointRegistry._entries
            if entry.meta.api_key_env is not None
        )
    account_names = tuple(account.key_env for account in settings.ANTHROPIC_ACCOUNTS)
    return credential_env_names(
        settings_type=type(settings),
        provider_env_names=provider_names,
        account_env_names=account_names,
        lookup_names=lookup.lookup.names if lookup.lookup is not None else (),
        mcp_auth_env_names=mcp_auth_env_names,
        active_reference_names=active_reference_names,
    )


def command_environment(
    *,
    declared_names: Iterable[str] | None = None,
    command_env_allowlist: Sequence[str] = (),
) -> dict[str, str]:
    """Prepare a command mapping from explicit declarations or their owned defaults."""
    from lionagi._command_env import scrub_command_env

    names = configured_command_env_names() if declared_names is None else declared_names
    return scrub_command_env(
        os.environ, declared_names=names, command_env_allowlist=command_env_allowlist
    )

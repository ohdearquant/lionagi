# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resolve one routed account's key, strictly, from a single named reference (ADR-0126 D2).

The reference is looked up unchanged in the process environment, then the dotenv
files the settings were built from, then the declared secret lookup. The first
place that has it decides: a present but invalid value refuses instead of falling
through, and nothing ever falls back to another name or another account.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from pydantic_settings import DotEnvSettingsSource

from lionagi.providers._secret_resolution import (
    SecretLookupResolution,
    lookup_one_name,
    resolve_secret_lookup_config,
)

from ._types import AccountRoutingError, RouterErrorCode

if TYPE_CHECKING:
    from lionagi.config import AppSettings

__all__ = ("CredentialSources", "resolve_account_key")

_MIN_KEY_CHARS = 8
_MAX_KEY_CHARS = 1024
# One more byte than the longest key, for the newline a command prints after it.
_MAX_LOOKUP_BYTES = _MAX_KEY_CHARS + 1


@dataclass(frozen=True, slots=True)
class CredentialSources:
    """Where keys may come from, captured once per process; holds no key values."""

    dotenv_files: tuple[Path, ...]
    dotenv_encoding: str | None
    lookup: SecretLookupResolution
    # Every configured account's reference: none of them reaches a lookup child.
    account_key_names: frozenset[str]

    @classmethod
    def capture(
        cls, settings: AppSettings, *, lookup_settings: dict[str, Any] | None = None
    ) -> CredentialSources:
        return cls(
            dotenv_files=settings._dotenv_files,
            dotenv_encoding=settings.model_config.get("env_file_encoding"),
            lookup=resolve_secret_lookup_config(settings=lookup_settings),
            account_key_names=frozenset(a.key_env for a in settings.ANTHROPIC_ACCOUNTS),
        )


def _refuse(code: RouterErrorCode) -> AccountRoutingError:
    return AccountRoutingError(code)


def _accept(value: Any, key_env: str) -> SecretStr:
    # A header-safe value only: printable ASCII without spaces. This checks that the
    # value can be sent, not that the provider will accept it.
    if (
        not isinstance(value, str)
        or not _MIN_KEY_CHARS <= len(value) <= _MAX_KEY_CHARS
        or any(not "\x21" <= ch <= "\x7e" for ch in value)
        or value == key_env
    ):
        raise _refuse(RouterErrorCode.CREDENTIAL_INVALID)
    return SecretStr(value)


def _dotenv(key_env: str, sources: CredentialSources) -> tuple[bool, str | None]:
    """Whether the dotenv files assign ``key_env``, and the last assignment if so."""
    if not sources.dotenv_files:
        return False, None
    from lionagi.config import AppSettings

    try:
        found: Mapping[str, str | None] | None = DotEnvSettingsSource(
            AppSettings,
            env_file=sources.dotenv_files,
            env_file_encoding=sources.dotenv_encoding,
            case_sensitive=True,
        ).env_vars
    except Exception:  # noqa: BLE001 -- a parser error can quote the file; report the outcome only
        found = None
    if found is None:
        # Raised outside the handler, so the refusal carries no context: the parser's error
        # can quote the file.
        raise _refuse(RouterErrorCode.CREDENTIAL_LOOKUP_FAILED)
    if key_env not in found:
        return False, None
    return True, found[key_env]


async def resolve_account_key(key_env: str, sources: CredentialSources) -> SecretStr:
    """The key named by ``key_env``, or an ``AccountRoutingError`` carrying only a code."""
    try:
        return await _resolve_account_key(key_env, sources)
    except AccountRoutingError as exc:
        code = exc.code
    # A fresh refusal raised outside the handler: the one caught above keeps the frames
    # that raised it, and their locals hold the value that was refused.
    raise _refuse(code)


async def _resolve_account_key(key_env: str, sources: CredentialSources) -> SecretStr:
    if key_env in os.environ:
        return _accept(os.environ[key_env], key_env)

    present, value = _dotenv(key_env, sources)
    if present:
        # An empty or bare assignment is an invalid key, not a missing one.
        return _accept(value, key_env)

    lookup = sources.lookup.lookup
    if lookup is None:
        if sources.lookup.reason is not None:
            raise _refuse(RouterErrorCode.CREDENTIAL_LOOKUP_FAILED)
        raise _refuse(RouterErrorCode.CREDENTIAL_MISSING)
    if key_env not in lookup.names:
        raise _refuse(RouterErrorCode.CREDENTIAL_MISSING)

    child_env = {k: v for k, v in os.environ.items() if k not in sources.account_key_names}
    result = await lookup_one_name(lookup, key_env, env=child_env, max_bytes=_MAX_LOOKUP_BYTES)
    if result.failure == "too_long":
        raise _refuse(RouterErrorCode.CREDENTIAL_INVALID)
    if result.value is None:
        raise _refuse(RouterErrorCode.CREDENTIAL_LOOKUP_FAILED)
    raw = result.value
    # One terminal newline is the command's, not the key's; nothing else is stripped.
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw:
        # The command worked and the store holds nothing under that name.
        raise _refuse(RouterErrorCode.CREDENTIAL_MISSING)
    try:
        text: str | None = raw.decode("ascii")
    except UnicodeDecodeError:
        text = None
    if text is None:
        # Raised outside the handler, so the refusal carries no context: the decode error
        # holds the bytes the lookup printed.
        raise _refuse(RouterErrorCode.CREDENTIAL_INVALID)
    return _accept(text, key_env)

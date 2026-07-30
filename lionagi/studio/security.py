# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Process-local Studio browser credentials and their trust origin.

Removing a variable from ``os.environ`` does not reliably remove the process's
initial environment from operating-system process inspection. Environment-
derived credentials therefore remain valid for ordinary Studio API auth but
are never trusted as the Operator's human-approval boundary.
"""

from __future__ import annotations

import os
import secrets

_AUTH_TOKEN: str | None = None
_HUMAN_TOKEN: str | None = None
_CREDENTIAL_ORIGIN: str | None = None


def install_generated_studio_token(token: str) -> None:
    """Install one trusted token received through a non-environment channel."""
    global _AUTH_TOKEN, _CREDENTIAL_ORIGIN, _HUMAN_TOKEN
    if not isinstance(token, str) or not 32 <= len(token) <= 4096:
        raise ValueError("Studio bootstrap token must contain 32 to 4096 characters")
    if "\x00" in token or "\r" in token or "\n" in token:
        raise ValueError("Studio bootstrap token contains an invalid control character")
    _AUTH_TOKEN = _HUMAN_TOKEN = token
    _CREDENTIAL_ORIGIN = "generated"


def capture_studio_credentials(*, generate_human: bool) -> str | None:
    """Capture configured credentials, optionally minting a human-only token."""
    global _AUTH_TOKEN, _CREDENTIAL_ORIGIN, _HUMAN_TOKEN
    auth = os.environ.pop("LIONAGI_STUDIO_AUTH_TOKEN", None)
    human = os.environ.pop("LIONAGI_STUDIO_HUMAN_TOKEN", None)
    if auth is not None or human is not None:
        _AUTH_TOKEN = auth
        _HUMAN_TOKEN = human
        _CREDENTIAL_ORIGIN = "environment"
    if _AUTH_TOKEN:
        return _AUTH_TOKEN
    if not _HUMAN_TOKEN and generate_human:
        # The browser uses one launch-scoped bearer for both the API boundary
        # and human-only Operator decisions. Keeping the generated capability
        # out of the environment is what makes that single-token scheme safe.
        _AUTH_TOKEN = _HUMAN_TOKEN = secrets.token_urlsafe(32)
        _CREDENTIAL_ORIGIN = "generated"
    return _HUMAN_TOKEN


def studio_auth_token() -> str | None:
    """Credential protecting the whole API, if configured."""
    return _AUTH_TOKEN or os.environ.get("LIONAGI_STUDIO_AUTH_TOKEN")


def studio_human_token() -> str | None:
    """Effective credential for human-only decisions."""
    return (
        _AUTH_TOKEN
        or os.environ.get("LIONAGI_STUDIO_AUTH_TOKEN")
        or _HUMAN_TOKEN
        or os.environ.get("LIONAGI_STUDIO_HUMAN_TOKEN")
    )


def studio_operator_credential_origin() -> str | None:
    """Return ``generated`` or ``environment`` for the effective credential."""
    if os.environ.get("LIONAGI_STUDIO_AUTH_TOKEN") is not None:
        return "environment"
    if os.environ.get("LIONAGI_STUDIO_HUMAN_TOKEN") is not None:
        return "environment"
    return _CREDENTIAL_ORIGIN


def clear_captured_studio_credentials() -> None:
    """Forget process-local credentials after the embedded daemon exits."""
    global _AUTH_TOKEN, _CREDENTIAL_ORIGIN, _HUMAN_TOKEN
    _AUTH_TOKEN = None
    _HUMAN_TOKEN = None
    _CREDENTIAL_ORIGIN = None

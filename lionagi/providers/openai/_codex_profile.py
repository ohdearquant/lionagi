# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resolution of a codex config profile named as a model.

Lives at the provider layer, not under ``cli/``, so both the library and CLI
entry points reach it. See
docs/internals/providers.md#codex-config-profile-resolution-and-effort-clamping.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from lionagi.libs.path_safety import validate_bare_name

__all__ = ("resolve_codex_config_profile",)

logger = logging.getLogger(__name__)


def _unreadable_symlink_target(path: Path) -> str | None:
    """Return a broken/non-file symlink's declared target, if applicable."""
    if not path.is_symlink() or path.is_file():
        return None
    try:
        return str(path.readlink())
    except OSError:
        return "<unreadable>"


def resolve_codex_config_profile(model: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve a codex model part that names a codex config profile.

    Only a bare name is looked up (no dots/separators), so an ordinary vendor
    model id is never treated as a profile path. Table-valued keys (notably
    ``mcp_servers``) are not applied, and are logged instead. Returns
    ``None`` when no such profile file exists, leaving the name to be
    treated as a model id exactly as before. See
    docs/internals/providers.md#codex-config-profile-resolution-and-effort-clamping.
    """
    try:
        validate_bare_name(model, label="codex config profile name")
    except ValueError:
        return None

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    profile_path = codex_home / f"{model}.config.toml"
    if not profile_path.is_file():
        # A broken symlink is not the same as no profile: is_file() answers
        # False for both, and falling through would send the name to codex as
        # a literal model id.
        broken_target = _unreadable_symlink_target(profile_path)
        if broken_target is not None:
            raise ValueError(
                f"codex config profile {str(profile_path)!r} is a symlink whose "
                f"target {broken_target!r} is unreadable. Repair or remove the "
                f"link; running without it would send {model!r} to codex as a "
                f"model id and silently run something else."
            )
        return None

    import toml

    try:
        data = toml.loads(profile_path.read_text())
    except (OSError, toml.TomlDecodeError) as exc:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} could not be read "
            f"({type(exc).__name__}: {exc}). Fix or remove the file; running "
            f"without it would send {model!r} as a model id instead."
        ) from exc

    resolved = data.get("model")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} declares no 'model'. "
            f"Add one, or use a model id instead of the profile name — "
            f"without it {model!r} would be sent to codex as a model id and "
            f"silently run something other than the profile."
        )

    overrides: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in data.items():
        if key == "model":
            continue
        if isinstance(value, (str, int, float, bool)):
            overrides[key] = value
        else:
            skipped.append(key)
    if skipped:
        logger.warning(
            "codex config profile %r: ignoring %s — lionagi applies a profile's "
            "model and scalar settings, and sets a leg's MCP servers itself",
            model,
            ", ".join(sorted(skipped)),
        )

    logger.info("codex profile %r resolves to model %r", model, resolved)
    return resolved, overrides

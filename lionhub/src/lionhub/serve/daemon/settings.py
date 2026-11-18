# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settings the Studio daemon reads from the environment: its listener, the origins it admits,
and the in-process transcript mirror. Read once, at import."""

from __future__ import annotations

import os
from pathlib import Path

from lionhub.orchestration.scheduler.settings import _env_flag

__all__ = (
    "CORS_ORIGINS",
    "HOST",
    "MIRROR_CLAUDE_ENABLED",
    "MIRROR_CLAUDE_INTERVAL",
    "MIRROR_CLAUDE_ROOT",
    "MIRROR_CLAUDE_SINCE",
    "MIRROR_CODEX_ROOT",
    "MIRROR_IMPORT_AMBIENT",
    "MIRROR_SOURCE",
    "STUDIO_PORT",
)

STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")

# How long a stopping server waits for open connections before closing them. One
# SIGTERM must end the process; this stays under the 5 s grace li kill gives
# before SIGKILL, so the app's own shutdown runs first.
_SHUTDOWN_GRACE_SECONDS: int = 3

_raw_origins = os.environ.get("CORS_ORIGINS", "")
CORS_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:3765",
        # The hosted static SPA (lion-studio.khive.ai) drives a user's local
        # daemon from a browser tab on this origin; an exact https origin,
        # never a wildcard or subdomain pattern.
        "https://lion-studio.khive.ai",
    ]
)


# Ambient transcript mirror
# When on, studio tails the local agent transcript trees in-process so those
# sessions show up (and stream live) without a separate `li mirror`. Bounded by
# the window below, so startup catches up the recent window only and never
# backfills full history — which matters most for codex, whose rollout corpus
# runs to tens of thousands of files.


def _mirror_import_ambient_default() -> bool:
    """Whether an unconfigured mirror may read the user's CLI transcript trees.

    The conventional ``~/.lionagi`` profile shares the same user boundary as
    ``~/.claude`` and ``~/.codex``. An explicitly selected ``LIONAGI_HOME`` is
    isolated unless the operator opts back in. Resolution failures fail closed.
    """
    configured_home = os.environ.get("LIONAGI_HOME")
    if configured_home is None:
        return True
    try:
        selected = Path(configured_home).expanduser().resolve()
        ambient = (Path.home() / ".lionagi").resolve()
    except (OSError, RuntimeError):
        return False
    return selected == ambient


def _optional_mirror_root(env_var: str) -> Path | None:
    """Resolve an optional transcript root without echoing its value on error."""
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        raise ValueError(f"{env_var} could not be resolved") from None


MIRROR_CLAUDE_ENABLED: bool = _env_flag("LIONAGI_STUDIO_MIRROR_CLAUDE", default=True)
MIRROR_CLAUDE_SINCE: str = os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_SINCE", "24h")
MIRROR_CLAUDE_INTERVAL: float = float(os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_INTERVAL", "5"))
# Which transcript trees the ambient mirror reads: "both", "claude", or "codex".
# An unrecognized value used to fall back to "both", which is the widest of the
# three: a misspelled "claude" silently read the codex tree as well. Refuse it
# instead, for the same reason the flags above refuse one.
_MIRROR_SOURCE_CHOICES = ("both", "claude", "codex")
_MIRROR_SOURCE_RAW: str = os.environ.get("LIONAGI_STUDIO_MIRROR_SOURCE", "both").strip().lower()
if _MIRROR_SOURCE_RAW not in _MIRROR_SOURCE_CHOICES:
    raise ValueError(
        f"LIONAGI_STUDIO_MIRROR_SOURCE must be one of {list(_MIRROR_SOURCE_CHOICES)}, "
        f"got {_MIRROR_SOURCE_RAW!r}"
    )
MIRROR_SOURCE: str = _MIRROR_SOURCE_RAW
MIRROR_CLAUDE_ROOT: Path | None = _optional_mirror_root("LIONAGI_STUDIO_MIRROR_CLAUDE_ROOT")
MIRROR_CODEX_ROOT: Path | None = _optional_mirror_root("LIONAGI_STUDIO_MIRROR_CODEX_ROOT")
MIRROR_IMPORT_AMBIENT: bool = _env_flag(
    "LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT", default=_mirror_import_ambient_default()
)

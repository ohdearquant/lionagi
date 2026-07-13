# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Environment-variable helpers for activating the scripted provider in-process and in subprocesses."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

# Re-exported from the endpoint module so import paths stay shallow.
from ._endpoint import ENV_SCRIPT_PATH

ENV_PROVIDER = "LIONAGI_CHAT_PROVIDER"
ENV_MODEL = "LIONAGI_CHAT_MODEL"
SCRIPTED_PROVIDER = "scripted"
DEFAULT_SCRIPTED_MODEL = "scripted-test"


def resolve_script_path() -> Path | None:
    """Return the path the scripted endpoint will use, or ``None``."""
    raw = os.environ.get(ENV_SCRIPT_PATH)
    if not raw:
        return None
    return Path(raw)


def is_scripted_provider_active() -> bool:
    """True iff ``LIONAGI_CHAT_PROVIDER`` selects the scripted endpoint."""
    return os.environ.get(ENV_PROVIDER, "").lower() == SCRIPTED_PROVIDER


@contextlib.contextmanager
def scripted_env(script_path: str | Path, model: str = DEFAULT_SCRIPTED_MODEL) -> Iterator[None]:
    """Context manager: set env so ``li`` subprocesses pick up the scripted provider,
    restoring prior values on exit."""
    previous: dict[str, str | None] = {
        ENV_PROVIDER: os.environ.get(ENV_PROVIDER),
        ENV_MODEL: os.environ.get(ENV_MODEL),
        ENV_SCRIPT_PATH: os.environ.get(ENV_SCRIPT_PATH),
    }
    os.environ[ENV_PROVIDER] = SCRIPTED_PROVIDER
    os.environ[ENV_MODEL] = model
    os.environ[ENV_SCRIPT_PATH] = str(script_path)
    try:
        yield
    finally:
        for key, val in previous.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def subprocess_env(
    script_path: str | Path, model: str = DEFAULT_SCRIPTED_MODEL, base: dict | None = None
) -> dict[str, str]:
    """Return a dict for ``subprocess.run(env=...)``, layered on ``base`` (defaults to a
    copy of ``os.environ``)."""
    env = dict(base if base is not None else os.environ)
    env[ENV_PROVIDER] = SCRIPTED_PROVIDER
    env[ENV_MODEL] = model
    env[ENV_SCRIPT_PATH] = str(script_path)
    return env


__all__ = (
    "DEFAULT_SCRIPTED_MODEL",
    "ENV_MODEL",
    "ENV_PROVIDER",
    "ENV_SCRIPT_PATH",
    "SCRIPTED_PROVIDER",
    "is_scripted_provider_active",
    "resolve_script_path",
    "scripted_env",
    "subprocess_env",
)

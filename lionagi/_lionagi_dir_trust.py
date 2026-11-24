# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Trusted project ``.lionagi`` directories for runs the MCP server starts (ADR-0088 Amendment 2).

A project ``.lionagi`` directory supplies agent profiles, playbooks and a settings layer. When
``lionagi_dir_trust.gate`` is ``mcp`` in the user-level settings file, the MCP server checks them at
submit and starts each job with ``LIONAGI_DIR_TRUST_GATE`` in its environment. A process with that
variable takes nothing from a project directory that ``li lionagi-dir trust`` has not recorded: it
refuses, naming the file, and never skips the file to carry on without it.

The record and the setting live in ``~/.lionagi/settings.yaml``, read through ``HOME``. Only the
CLI's trust step writes the record. The user-level ``~/.lionagi`` is never gated.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from lionagi._paths import is_user_lionagi_dir

__all__ = (
    "GATE_ENV_VAR",
    "GATE_SETTING",
    "RECORD_KEY",
    "LionagiDirTrustSettingError",
    "UntrustedLionagiDirError",
    "check_lionagi_dir",
    "enforced",
    "gate_active",
    "gate_setting",
    "is_trusted",
    "trust_step_refusal",
    "trusted_lionagi_dirs",
)

GATE_ENV_VAR = "LIONAGI_DIR_TRUST_GATE"
GATE_SETTING = "lionagi_dir_trust"
RECORD_KEY = "trusted_lionagi_dirs"

# Set while the MCP server checks a submission: the server itself has no gate variable, and a
# submission must get the answer its job process will get.
_ENFORCED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lionagi_dir_trust_enforced", default=False
)


class UntrustedLionagiDirError(Exception):
    """A gated run reached a file in a project ``.lionagi`` directory that is not trusted.

    Deliberately not a ``ValueError``, ``OSError`` or ``FileNotFoundError``: resolvers fall back on
    those (a missing profile becomes a model spec, an unreadable settings file becomes no
    settings), and this refusal must stop the run rather than be read as one of them.
    """

    def __init__(self, what: str, path: str | Path, lionagi_dir: str | Path) -> None:
        self.what = what
        self.path = Path(os.path.realpath(path))
        self.lionagi_dir = Path(os.path.realpath(lionagi_dir))
        super().__init__(
            f"{what} {self.path} comes from {self.lionagi_dir}, which is not a trusted .lionagi "
            "directory; this run was started through the MCP server with lionagi_dir_trust.gate "
            f"set to mcp. Run `li lionagi-dir trust {self.lionagi_dir.parent}` to trust it, or "
            "start the run from a directory without project files."
        )

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.what, self.path, self.lionagi_dir))


class LionagiDirTrustSettingError(ValueError):
    """``lionagi_dir_trust`` in the user-level settings file cannot be read as ``off`` or ``mcp``."""


def _user_settings_path() -> Path:
    return Path.home() / ".lionagi" / "settings.yaml"


def _read_user_settings(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    data = yaml.safe_load(text) if text.strip() else {}
    return data if isinstance(data, dict) else {}


def gate_active() -> bool:
    """Whether this process checks project directories.

    The variable alone turns the check on, whatever the setting says; the setting decides only
    whether the MCP server puts the variable on the jobs it starts.
    """
    return GATE_ENV_VAR in os.environ or _ENFORCED.get()


@contextlib.contextmanager
def enforced(on: bool = True) -> Iterator[None]:
    """Check project directories inside this block as a gated job would; a no-op when *on* is false."""
    token = _ENFORCED.set(_ENFORCED.get() or on)
    try:
        yield
    finally:
        _ENFORCED.reset(token)


def gate_setting() -> str:
    """``off`` or ``mcp``, from ``lionagi_dir_trust.gate`` in the user-level settings file.

    Absent means ``off``. A file that cannot be read, or a value that is neither, raises
    :class:`LionagiDirTrustSettingError` rather than guessing, because guessing ``off`` would run
    files the person asked to have checked.
    """
    path = _user_settings_path()
    try:
        settings = _read_user_settings(path)
    except (OSError, yaml.YAMLError) as exc:
        raise LionagiDirTrustSettingError(
            f"cannot read {GATE_SETTING}.gate from {path}: {exc}"
        ) from exc
    block = settings.get(GATE_SETTING)
    if block is None:
        return "off"
    if not isinstance(block, dict):
        raise LionagiDirTrustSettingError(
            f"{GATE_SETTING} in {path} must be a mapping with a gate key, got {block!r}"
        )
    gate = block.get("gate")
    # YAML 1.1 reads a bare `off` as false.
    if gate is None or gate is False or gate == "off":
        return "off"
    if gate == "mcp":
        return "mcp"
    raise LionagiDirTrustSettingError(
        f"{GATE_SETTING}.gate in {path} must be off or mcp, got {gate!r}"
    )


def trusted_lionagi_dirs() -> tuple[str, ...]:
    """The recorded directories, as written by ``li lionagi-dir trust`` (resolved paths).

    A settings file that cannot be read trusts nothing, so a gated run refuses instead of
    running files nobody can show were trusted.
    """
    try:
        settings = _read_user_settings(_user_settings_path())
    except (OSError, yaml.YAMLError):
        return ()
    record = settings.get(RECORD_KEY)
    if not isinstance(record, list):
        return ()
    return tuple(entry for entry in record if isinstance(entry, str))


def is_trusted(lionagi_dir: str | Path) -> bool:
    """Whether *lionagi_dir* is the user-level directory or is recorded, by its resolved path.

    Each directory answers for itself: a record of a parent's ``.lionagi`` covers no directory
    below it, and the reverse. The recorded entries are compared as written, never resolved
    again, so a recorded path later replaced by a link does not carry its trust to the target.
    """
    if is_user_lionagi_dir(Path(lionagi_dir)):
        return True
    return os.path.realpath(lionagi_dir) in trusted_lionagi_dirs()


def check_lionagi_dir(what: str, path: str | Path, lionagi_dir: str | Path) -> None:
    """Refuse *path* from *lionagi_dir* in a gated process unless the directory is trusted.

    *what* is ``agent profile``, ``playbook`` or ``project settings``. Ungated, this does nothing.
    """
    if not gate_active() or is_trusted(lionagi_dir):
        return
    raise UntrustedLionagiDirError(what, path, lionagi_dir)


def trust_step_refusal(step: str) -> str | None:
    """The refusal a trust step gives inside a run the MCP server started, or None outside one.

    A trust step records a grant; a run that could type the command would grant itself.
    """
    if GATE_ENV_VAR not in os.environ:
        return None
    return (
        f"li {step} refuses to run inside a run the MCP server started "
        f"({GATE_ENV_VAR} is set); run it from your own shell."
    )

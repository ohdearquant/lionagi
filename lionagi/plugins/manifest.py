# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Plugin manifest schema: a declarative, pure-data description of a plugin bundle.

Parsing a manifest imports nothing and executes nothing — every reference to
bundle code (``target``/``module`` strings) is a bundle-relative path resolved
only when that specific capability is later activated. See ``registry.py`` for
the two-stage discovery/activation split.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = (
    "Capabilities",
    "HookCommand",
    "HookMatcher",
    "ManifestError",
    "PluginManifest",
    "ProviderCapability",
    "ToolCapability",
    "parse_manifest",
    "parse_tool_target",
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_CALLABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_tool_target(target: str, *, label: str = "tool target") -> tuple[str, str]:
    """Split a bundle-relative ``path.py:callable`` reference into ``(path, callable)``.

    This is the single parser both declared-file collection (what gets
    content-hashed at trust time, in ``discovery.py``) and activation (what
    gets imported, in ``registry.py``) must use — never two independently
    written splitting expressions. A target with anything other than
    exactly one ``:`` is rejected outright (no "last colon wins" vs "first
    colon wins" ambiguity is possible once that's enforced), and the
    callable part must be a plain Python identifier: no colons, no path
    separators, no dots. A bundle-relative filename is separately barred
    from containing ``:`` at all (see ``discovery._validate_bundle_relative``),
    which closes the same gap from the other side.
    """
    parts = target.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"{label} {target!r} must have exactly one ':' separator ('relative/path.py:callable')"
        )
    path_part, callable_name = parts
    if not path_part or not callable_name:
        raise ValueError(f"{label} {target!r} must be 'relative/path.py:callable'")
    if not _CALLABLE_NAME_RE.match(callable_name):
        raise ValueError(
            f"{label} {target!r} callable {callable_name!r} must be a plain Python identifier"
        )
    return path_part, callable_name


class ManifestError(ValueError):
    """A ``plugin.yaml`` failed schema validation.

    Carries the manifest path so callers can produce a diagnostic naming the
    file (and, where known, the offending field) rather than a bare pydantic
    traceback.
    """

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = Path(path)
        self.message = message
        super().__init__(f"{self.path}: {message}")


def _reject_unknown_x_prefixed(data: Any, *, where: str) -> Any:
    """Drop top-level ``x-``-prefixed keys (reserved for vendor/user annotation)."""
    if not isinstance(data, dict):
        raise ManifestError("<manifest>", f"{where} must be a mapping, got {type(data).__name__}")
    return {k: v for k, v in data.items() if not k.startswith("x-")}


class ToolCapability(BaseModel):
    """``capabilities.tools[]`` — a bundle-relative ``module.py:callable`` reference."""

    model_config = ConfigDict(extra="forbid")

    name: str
    target: str

    @field_validator("target")
    @classmethod
    def _validate_target(cls, v: str) -> str:
        parse_tool_target(v, label="tool target")
        return v


class HookCommand(BaseModel):
    """A single ``hooks_external`` command entry: an argv-list external command reference.

    Declared here as pure data only — parsing a manifest never executes or
    wires up the referenced command; that is a separate mechanism this
    package does not implement.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = "command"
    command: list[str]

    @field_validator("command")
    @classmethod
    def _non_empty_argv(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("hook command argv must not be empty")
        return v


class HookMatcher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matcher: str
    hooks: list[HookCommand]


class ProviderCapability(BaseModel):
    """``capabilities.providers[]`` — a bundle-relative provider module, imported lazily."""

    model_config = ConfigDict(extra="forbid")

    module: str


class Capabilities(BaseModel):
    """``capabilities:`` block: tools, external hooks, agent profiles, playbooks,
    providers — plus pack files as data."""

    model_config = ConfigDict(extra="forbid")

    tools: list[ToolCapability] = Field(default_factory=list)
    hooks_external: dict[str, list[HookMatcher]] = Field(default_factory=dict)
    agents: list[str] = Field(default_factory=list)
    playbooks: list[str] = Field(default_factory=list)
    providers: list[ProviderCapability] = Field(default_factory=list)
    packs: list[str] = Field(default_factory=list)


class PluginManifest(BaseModel):
    """``plugin.yaml`` — the declarative capability manifest for a plugin bundle."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str = ""
    lionagi: str
    capabilities: Capabilities = Field(default_factory=Capabilities)

    @model_validator(mode="before")
    @classmethod
    def _drop_vendor_keys(cls, data: Any) -> Any:
        return _reject_unknown_x_prefixed(data, where="manifest")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(
                f"invalid plugin name {v!r}: must match {_NAME_RE.pattern!r} "
                "(lowercase alnum + hyphens, max 32 chars, starting with alnum)"
            )
        return v

    @field_validator("lionagi")
    @classmethod
    def _validate_specifier(cls, v: str) -> str:
        from packaging.specifiers import SpecifierSet

        try:
            SpecifierSet(v)
        except Exception as exc:  # noqa: BLE001 — surface as a plain validation error
            raise ValueError(f"invalid lionagi version specifier {v!r}: {exc}") from exc
        return v

    def is_compatible(self, installed_version: str) -> bool:
        """True if *installed_version* satisfies this manifest's ``lionagi:`` range."""
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        return Version(installed_version) in SpecifierSet(self.lionagi)


def parse_manifest(path: Path) -> PluginManifest:
    """Parse and validate a ``plugin.yaml`` file at *path*.

    Raises ``ManifestError`` (naming the file and, where pydantic can localize
    it, the offending field) on any schema violation. Never imports or
    executes anything the manifest references.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(path, f"invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ManifestError(path, f"could not read manifest: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(path, f"manifest must be a YAML mapping, got {type(raw).__name__}")

    try:
        return PluginManifest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError, re-wrapped with path context
        raise ManifestError(path, str(exc)) from exc

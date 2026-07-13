# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lionagi.ln.types import Enum, ModelConfig, Params
from lionagi.protocols._concepts import Composable

if TYPE_CHECKING:
    from lionagi.ln.types import Operable

__all__ = (
    "Pattern",
    "PatternKind",
    "Mode",
    "Role",
    "list_roles",
    "list_modes",
)

# CLOSED built-in set, one module per pattern — not user-definable; extend via packs (casts/pack.py).
_ROLES_PKG = "lionagi.casts.roles"
_MODES_PKG = "lionagi.casts.roles.modes"


class PatternKind(Enum):
    OTHER = "other"
    ROLE = "role"
    MODE = "mode"


@dataclass(init=False, frozen=True, slots=True)
class Pattern(Params, Composable):
    """Composable, frozen atom of agent configuration; Role and Mode subclass and override ``kind``."""

    _config = ModelConfig(
        none_as_sentinel=True,
        empty_as_sentinel=True,
    )

    name: str
    description: str

    @property
    def kind(self) -> PatternKind:
        return PatternKind.OTHER


def _module_stem(name: str) -> str:
    """Pattern name -> importable module stem (names may contain dashes)."""
    return name.replace("-", "_")


def _load_builtin(pkg: str, name: str, attr: str):
    """Import a built-in pattern module and return its ``ROLE``/``MODE``; None if absent or non-canonical."""
    target = f"{pkg}.{_module_stem(name)}"
    try:
        mod = import_module(target)
    except ModuleNotFoundError as e:
        if e.name == target:
            return None
        raise
    obj = getattr(mod, attr)
    return obj if obj.name == name else None


def _list_builtin_modules(pkg: str) -> set[str]:
    """Module stems of built-in patterns in *pkg* (excludes _private, TEMPLATE)."""
    spec = import_module(pkg)
    root = Path(spec.__file__).parent
    names: set[str] = set()
    for p in root.glob("*.py"):
        if p.stem.startswith("_") or p.stem == "TEMPLATE":
            continue
        names.add(p.stem)
    return names


@dataclass(init=False, frozen=True, slots=True)
class Mode(Pattern):
    """Cognitive overlay — shapes *how* an agent reasons."""

    behaviors: str = ""
    conflicts_with: frozenset = field(default_factory=frozenset)

    @property
    def kind(self) -> PatternKind:
        return PatternKind.MODE

    @classmethod
    def load(cls, name: str, /) -> Mode:
        """Load a built-in mode by canonical name."""
        obj = _load_builtin(_MODES_PKG, name, "MODE")
        if obj is None:
            raise ValueError(f"Unknown mode: {name!r}. Available: {_available_names(list_modes)}")
        return obj


@dataclass(init=False, frozen=True, slots=True)
class Role(Pattern):
    """Behavioral pattern: ``body`` composes into the system prompt; ``emits`` declares the emission contract."""

    body: str = ""
    emits: tuple = ()
    # ADR-0064: gate role's declared output contract, merged per-leg into the flow's
    # artifact_contract at DAG-build time (flow.py _build_dag). None = no artifact claim.
    artifact_defaults: dict | None = None

    @property
    def kind(self) -> PatternKind:
        return PatternKind.ROLE

    def to_dict(self, exclude: set[str] = None) -> dict[str, Any]:
        # Params.to_dict (not super()) — zero-arg super is unreliable under @dataclass(slots=True).
        d = Params.to_dict(self, exclude=exclude)
        if "emits" in d:
            d["emits"] = [m.__name__ for m in d["emits"]]
        return d

    def emission_operable(self) -> Operable | None:
        """Build the Operable for this role's emission contract; None if no emits; always includes EscalationRequest."""
        from lionagi.casts.emission import build_emission_operable
        from lionagi.ln.types import is_sentinel

        emits = getattr(self, "emits", ())
        if is_sentinel(emits, none_as_sentinel=True, empty_as_sentinel=True):
            return None
        return build_emission_operable(tuple(emits), name=f"{self.name}_emissions")

    @classmethod
    def load(cls, name: str, /) -> Role:
        """Load a built-in role by canonical name."""
        obj = _load_builtin(_ROLES_PKG, name, "ROLE")
        if obj is None:
            raise ValueError(f"Unknown role: {name!r}. Available: {_available_names(list_roles)}")
        return obj


def _available_names(lister) -> str:
    """Best-effort catalog for an error hint; a broken built-in never masks the original ValueError."""
    try:
        return ", ".join(lister())
    except Exception:
        return "<unavailable>"


def list_roles() -> list[str]:
    """Return sorted canonical names of all built-in roles."""
    stems = _list_builtin_modules(_ROLES_PKG)
    return sorted(import_module(f"{_ROLES_PKG}.{s}").ROLE.name for s in stems)


def list_modes() -> list[str]:
    """Return sorted canonical names of all built-in modes."""
    stems = _list_builtin_modules(_MODES_PKG)
    return sorted(import_module(f"{_MODES_PKG}.{s}").MODE.name for s in stems)

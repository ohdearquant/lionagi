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

# Roles/modes are a CLOSED built-in set — one inline module per pattern, each
# exposing a single ``ROLE`` / ``MODE``. Not user-definable; users extend via
# packs (casts/pack.py), not by adding roles.
_ROLES_PKG = "lionagi.casts.roles"
_MODES_PKG = "lionagi.casts.roles.modes"


class PatternKind(Enum):
    OTHER = "other"
    ROLE = "role"
    MODE = "mode"


@dataclass(init=False, frozen=True, slots=True)
class Pattern(Params, Composable):
    """Abstract, composable atom of agent configuration.

    A frozen value object with a name and description. Concrete patterns
    subclass it (Role, Mode) and override ``kind``.
    """

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
    """Import a built-in pattern module and return its ``ROLE``/``MODE`` object.

    Raises ModuleNotFoundError if there is no such built-in.
    """
    mod = import_module(f"{pkg}.{_module_stem(name)}")
    return getattr(mod, attr)


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
        """Load a built-in mode by name."""
        try:
            return _load_builtin(_MODES_PKG, name, "MODE")
        except ModuleNotFoundError:
            raise ValueError(f"Unknown mode: {name!r}") from None


@dataclass(init=False, frozen=True, slots=True)
class Role(Pattern):
    """Behavioral pattern — what an agent does and the discipline it follows.

    ``body`` composes into the system prompt; ``description`` is the dense,
    orchestrator-facing selection signal (not in the prompt). ``emits`` is the
    role's emission contract — payloads it produces (see ``casts.emission``).
    """

    body: str = ""
    emits: tuple = ()

    @property
    def kind(self) -> PatternKind:
        return PatternKind.ROLE

    def to_dict(self, exclude: set[str] = None) -> dict[str, Any]:
        # serialize ``emits`` (model classes) by name so the dict stays
        # hashable/JSON-friendly. Params.to_dict (not super()) — zero-arg super
        # is unreliable under @dataclass(slots=True).
        d = Params.to_dict(self, exclude=exclude)
        if "emits" in d:
            d["emits"] = [m.__name__ for m in d["emits"]]
        return d

    def emission_operable(self) -> Operable | None:
        """Build the :class:`Operable` for this role's emission contract.

        ``None`` when the role declares no emissions. Otherwise includes
        ``EscalationRequest`` (any emitting role may also escalate).
        """
        from lionagi.casts.emission import build_emission_operable
        from lionagi.ln.types import is_sentinel

        emits = getattr(self, "emits", ())
        if is_sentinel(emits, none_as_sentinel=True, empty_as_sentinel=True):
            return None
        return build_emission_operable(tuple(emits), name=f"{self.name}_emissions")

    @classmethod
    def load(cls, name: str, /) -> Role:
        """Load a built-in role by name."""
        try:
            return _load_builtin(_ROLES_PKG, name, "ROLE")
        except ModuleNotFoundError:
            raise ValueError(f"Unknown role: {name!r}") from None


def list_roles() -> list[str]:
    """Sorted canonical names of all built-in roles.

    Module stems use underscores; canonical names (which may contain dashes)
    are restored from each module's declared ``ROLE.name``.
    """
    stems = _list_builtin_modules(_ROLES_PKG)
    return sorted(_load_builtin(_ROLES_PKG, s, "ROLE").name for s in stems)


def list_modes() -> list[str]:
    """Sorted canonical names of all built-in modes.

    Module stems use underscores; canonical names (which may contain dashes)
    are restored from each module's declared ``MODE.name``.
    """
    stems = _list_builtin_modules(_MODES_PKG)
    return sorted(_load_builtin(_MODES_PKG, s, "MODE").name for s in stems)

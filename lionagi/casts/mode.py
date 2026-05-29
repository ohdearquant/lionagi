# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mode: composable cognitive overlays — *how* an agent reasons.

A Mode is a :class:`~lionagi.casts.pattern.Pattern` (``kind="mode"``) that shapes
reasoning only. Purity contract, enforced at construction *and* at the markdown
loader: a mode never grants capabilities or resources, carries authority, sets
boundaries, or holds ``extra`` metadata. It contributes one behavioral
instruction (the inherited ``prompt``) plus selection metadata — nothing that
reaches the runtime Branch. Authority, access, and artifacts belong to roles.

Modes stack onto an actor. ``conflicts_with`` is the hard rule the orchestrator
enforces; ``axis`` only groups related controls and is *not* the conflict
mechanism. The 14 built-ins are authored as markdown under ``roles/modes/*.md``
and loaded on demand.

Design rationale, the full roster, and the axis model:
``docs/adrs/ADR-0071-cognitive-mode-model.md``.

Usage::

    from lionagi.casts.mode import get_mode, validate_mode_stack

    validate_mode_stack([get_mode("fast"), get_mode("evidential")])  # ok -> []
    validate_mode_stack([get_mode("fast"), get_mode("slow")])        # ModeConflictError
"""

from __future__ import annotations

import re
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .pattern import Pattern

__all__ = (
    "ModeAxis",
    "Mode",
    "ModeConflictError",
    "MODES_DIR",
    "load_mode_file",
    "builtin_modes",
    "get_mode",
    "validate_mode_stack",
)

# Built-in mode definitions live alongside the role library.
MODES_DIR = Path(__file__).parent / "roles" / "modes"


class ModeAxis(str, Enum):
    """Cognitive dimension a mode operates on.

    Organizational grouping only — see the module docstring on why this is not
    the conflict mechanism.
    """

    TEMPO = "tempo"
    SEARCH = "search-topology"
    EPISTEMIC = "epistemic-accounting"
    FEASIBILITY = "feasibility"
    SKEPTICAL = "skeptical-stress"
    PERSPECTIVE = "perspective"
    SELF_MONITORING = "self-monitoring"


class ModeConflictError(ValueError):
    """Raised when a mode stack contains two hard-conflicting modes."""

    def __init__(self, a: str, b: str) -> None:
        self.mode_a = a
        self.mode_b = b
        super().__init__(
            f"Modes '{a}' and '{b}' conflict and cannot be composed in the same stack."
        )


class _ReadOnlyDict(dict):
    """A dict that refuses mutation — used so a frozen Mode's ``extra`` cannot
    acquire metadata in place (``frozen=True`` blocks reassignment, not the
    mutation of a contained ``dict``). Stays a real ``dict`` so it serializes
    and deep-copies natively."""

    def _readonly(self, *_a, **_k):
        raise TypeError("Mode.extra is read-only")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _readonly


class Mode(Pattern):
    """Cognitive overlay — a marked deviation from default reasoning.

    ``prompt`` (inherited from Pattern) carries the behavioral instruction that
    composes into the actor's system prompt. ``description`` is the one-line
    selection hint. The remaining fields are selection/composition metadata for
    the orchestrator — they never reach the runtime Branch.

    Kind is always ``"mode"``.
    """

    kind: Literal["mode"] = "mode"

    description: str = Field(
        default="",
        description="One-line cognitive-style summary, for orchestrator selection.",
    )

    axis: ModeAxis = Field(
        default=ModeAxis.TEMPO,
        description="Cognitive dimension this mode marks. Organizational, not a conflict rule.",
    )

    tier: Literal["core", "extended"] = Field(
        default="core",
        description="core: general-purpose roster. extended: niche overlays.",
    )

    phase_scope: Literal["pre", "during", "post", "continuous", "all"] = Field(
        default="all",
        description="When in a reasoning/DAG step the overlay applies.",
    )

    overhead: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Relative cognitive cost. Scheduling hint only; grants nothing.",
    )

    conflicts_with: frozenset[str] = Field(
        default_factory=frozenset,
        description="Mode names that cannot share a stack with this one (hard rule).",
    )

    composes_well_with: frozenset[str] = Field(
        default_factory=frozenset,
        description="Mode or role names this pairs well with (soft selection hint).",
    )

    when_to_use: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Selection triggers — conditions under which to apply this mode.",
    )

    when_not_to_use: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Over-use / failure conditions — when not to apply this mode.",
    )

    @model_validator(mode="after")
    def _enforce_mode_purity(self) -> Mode:
        """A mode constrains cognition only — it carries no authority or access."""
        violations = [
            name
            for name, value in (
                ("capabilities", self.capabilities),
                ("resources", self.resources),
                ("authority", self.authority),
                ("boundaries", self.boundaries),
                ("extra", self.extra),
            )
            if value
        ]
        if violations:
            raise ValueError(
                f"Mode '{self.name}' must not carry {violations}; modes constrain "
                "cognition only — move these to a role or pattern."
            )
        # extra is now empty; pin it read-only so it cannot be mutated in place.
        if not isinstance(self.extra, _ReadOnlyDict):
            object.__setattr__(self, "extra", _ReadOnlyDict())
        return self

    def model_copy(self, *, update: dict | None = None, deep: bool = False) -> Mode:
        """Re-validate on copy. Pydantic's ``update=`` bypasses validators, which
        would otherwise let a copy smuggle in non-cognitive fields the purity
        contract forbids. Every Mode field is immutable, so ``deep`` is moot —
        the reconstruction is always independent."""
        data = {**self.__dict__, "extra": dict(self.extra)}
        if update:
            data.update(update)
        return type(self).model_validate(data)

    @property
    def instruction(self) -> str:
        """The behavioral instruction (stored in the inherited ``prompt`` field)."""
        return self.prompt

    def __repr__(self) -> str:
        return f"Mode(name='{self.name}', axis='{self.axis.value}', tier='{self.tier}')"


# ──────────────────────── Markdown loading ─────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# The closed frontmatter contract. The loader is fail-closed: a mode file
# declaring anything outside this set (a forbidden field like `authority`, or a
# typo) is rejected rather than silently dropped — the .md files are the source
# of truth, so an invalid definition must not normalize into an apparently pure mode.
_MODE_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "axis",
        "tier",
        "phase_scope",
        "overhead",
        "conflicts_with",
        "composes_well_with",
        "when_to_use",
        "when_not_to_use",
    }
)


def _inline_field(body: str, label: str) -> str:
    """Extract a ``**Label**: value`` field, up to the next blank line or heading."""
    match = re.search(rf"\*\*{re.escape(label)}\*\*:\s*(.+?)(?:\n\n|\n#|\Z)", body, re.DOTALL)
    return match.group(1).strip() if match else ""


def _instruction_section(body: str) -> str:
    """Extract the prose under ``## Behavioral Instructions`` up to the next field/heading."""
    match = re.search(r"##\s*Behavioral Instructions\s*\n(.+?)(?:\n\*\*|\n##|\Z)", body, re.DOTALL)
    return match.group(1).strip() if match else ""


def load_mode_file(path: Path) -> Mode:
    """Parse a ``modes/*.md`` file (YAML frontmatter + markdown body) into a Mode."""
    text = path.read_text(encoding="utf-8")
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        raise ValueError(f"Mode file {path} is missing YAML frontmatter.")

    import yaml  # lazy: keep module import O(1) (mirrors charter.py)

    meta = yaml.safe_load(fm_match.group(1)) or {}
    body = text[fm_match.end() :]

    unknown = set(meta) - _MODE_FRONTMATTER_KEYS
    if unknown:
        raise ValueError(
            f"Mode file {path.name} has unsupported frontmatter keys: {sorted(unknown)}. "
            f"Allowed: {sorted(_MODE_FRONTMATTER_KEYS)}."
        )

    return Mode(
        name=meta["name"],
        prompt=_instruction_section(body),
        description=_inline_field(body, "Description"),
        axis=ModeAxis(meta.get("axis", "tempo")),
        tier=meta.get("tier", "core"),
        phase_scope=meta.get("phase_scope", "all"),
        overhead=meta.get("overhead", "medium"),
        conflicts_with=frozenset(meta.get("conflicts_with") or ()),
        composes_well_with=frozenset(meta.get("composes_well_with") or ()),
        when_to_use=tuple(meta.get("when_to_use") or ()),
        when_not_to_use=tuple(meta.get("when_not_to_use") or ()),
    )


@lru_cache(maxsize=1)
def _load_all() -> tuple[Mode, ...]:
    if not MODES_DIR.is_dir():
        return ()
    return tuple(load_mode_file(p) for p in sorted(MODES_DIR.glob("*.md")))


def builtin_modes() -> dict[str, Mode]:
    """Return the built-in mode registry as a fresh ``name -> Mode`` dict.

    Each call returns independent deep copies. A caller that mutates a returned
    mode cannot poison the cached canonical instances or any other caller.
    """
    return {m.name: m.model_copy(deep=True) for m in _load_all()}


def get_mode(name: str) -> Mode:
    """Look up a built-in mode by name. Raises KeyError if unknown."""
    modes = builtin_modes()
    if name not in modes:
        raise KeyError(f"No built-in mode '{name}'. Available: {sorted(modes)}")
    return modes[name]


def validate_mode_stack(modes: list[Mode]) -> list[str]:
    """Validate a stack of modes intended to compose onto one actor.

    Raises :class:`ModeConflictError` on the first hard conflict (the check is
    symmetric — a conflict declared on either mode counts). Returns a list of
    soft, advisory warnings (e.g. several modes drawn from the same axis), which
    do not block composition.
    """
    modes = list(modes)
    for i, a in enumerate(modes):
        for b in modes[i + 1 :]:
            if b.name in a.conflicts_with or a.name in b.conflicts_with:
                raise ModeConflictError(a.name, b.name)

    by_axis: dict[ModeAxis, list[str]] = {}
    for m in modes:
        by_axis.setdefault(m.axis, []).append(m.name)

    warnings: list[str] = []
    for axis, names in by_axis.items():
        if len(names) > 2:
            warnings.append(
                f"{len(names)} modes on the '{axis.value}' axis "
                f"({', '.join(sorted(names))}); consider whether all are needed."
            )
    return warnings

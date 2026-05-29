# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Mode: composable cognitive overlays — how an agent reasons.

A Mode is a kind of :class:`~lionagi.casts.pattern.Pattern` that constrains
*how* an actor thinks, distinct from a Role (what it does, its authority and
artifacts) and from capability/resource patterns (what it may touch). Modes are
pure cognitive overlays::

    role + mode(s) + capabilities + resources  ->  Profile  ->  Actor  ->  Branch

The mode contract (enforced at construction): a Mode never grants capabilities
or resources, carries authority, or produces artifacts. It contributes a
behavioral instruction to the system prompt and nothing else. Those other
concerns belong to roles and patterns.

Modes are *marked deviations* from a default reasoning policy — balanced tempo,
focused search, ordinary epistemic hygiene, pragmatic solutioning, cooperative
stance, role-governed output. Each mode marks one deviation, organized on a
small set of cognitive ``axes``.

Composition: modes stack. ``conflicts_with`` is the hard rule the orchestrator
enforces (e.g. ``fast`` cannot coexist with ``slow`` or ``systematic``). The
``axis`` field is *organizational* — it groups related controls and backs the
soft heuristic "do not stack several modes from the same axis"; it is NOT the
conflict mechanism. Same-axis modes (``evidential`` + ``probabilistic``)
routinely compose, while cross-axis modes (``fast`` + ``systematic``) conflict,
so conflicts are declared explicitly per mode, not inferred from the axis.

The 14 built-in modes are authored as markdown under ``roles/modes/*.md`` and
loaded on demand via :func:`builtin_modes`.

Usage::

    from lionagi.casts.mode import get_mode, validate_mode_stack

    fast = get_mode("fast")
    evidential = get_mode("evidential")
    validate_mode_stack([fast, evidential])            # ok -> []
    validate_mode_stack([get_mode("fast"), get_mode("slow")])  # ModeConflictError
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


class Mode(Pattern):
    """Cognitive overlay — a marked deviation from default reasoning.

    ``prompt`` (inherited from Pattern) carries the behavioral instruction that
    composes into the actor's system prompt. ``description`` is the one-line
    selection hint. The remaining fields are selection/composition metadata for
    the orchestrator — they never reach the runtime Branch.

    Kind is always ``"mode"``.
    """

    kind: str = Field(default="mode", frozen=True)

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
            )
            if value
        ]
        if violations:
            raise ValueError(
                f"Mode '{self.name}' must not carry {violations}; modes constrain "
                "cognition only — move these to a role or pattern."
            )
        return self

    @property
    def instruction(self) -> str:
        """The behavioral instruction (stored in the inherited ``prompt`` field)."""
        return self.prompt

    def __repr__(self) -> str:
        return f"Mode(name='{self.name}', axis='{self.axis.value}', tier='{self.tier}')"


# ──────────────────────── Markdown loading ─────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


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
    """Return the built-in mode registry as a fresh ``name -> Mode`` dict."""
    return {m.name: m for m in _load_all()}


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

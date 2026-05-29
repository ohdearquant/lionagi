# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = (
    "Pattern",
    "Role",
)


class Pattern(BaseModel):
    """Composable atom of agent configuration.

    NOT an Element — no UUID, no persistence. Patterns are templates,
    not entities. Actors are entities; Patterns configure them.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Pattern identifier.")

    kind: str = Field(
        default="generic",
        description=(
            "Pattern type: 'role', 'mode', 'tool', 'model', 'governance', 'domain', 'generic'."
        ),
    )

    capabilities: frozenset[str] = Field(
        default_factory=frozenset,
        description="Operations this pattern enables.",
    )

    resources: frozenset[str] = Field(
        default_factory=frozenset,
        description="Service/tool scopes this pattern grants.",
    )

    prompt: str = Field(
        default="",
        description="Behavioral instructions contributed by this pattern.",
    )

    effort: str | None = Field(
        default=None,
        description="Effort signal: low, medium, high.",
    )

    authority: tuple[str, ...] = Field(
        default_factory=tuple,
        description="What this pattern authorizes the actor to do.",
    )

    boundaries: tuple[str, ...] = Field(
        default_factory=tuple,
        description="What this pattern forbids.",
    )

    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Pattern-specific metadata (charter_ref, model spec, etc.).",
    )

    # ── Composition ─────────────────────────────────────────────────

    @classmethod
    def compose(cls, patterns: list[Pattern], *, name: str = "composed") -> Pattern:
        """Merge multiple patterns into one.

        Composition is additive:
        - capabilities, resources: union
        - prompt: concatenate (insertion order)
        - authority, boundaries: concatenate
        - effort: last non-None wins
        - extra: shallow merge (later keys override)
        """
        caps: set[str] = set()
        res: set[str] = set()
        prompts: list[str] = []
        auth: list[str] = []
        bounds: list[str] = []
        effort: str | None = None
        extra: dict[str, Any] = {}
        kinds: set[str] = set()

        for p in patterns:
            caps.update(p.capabilities)
            res.update(p.resources)
            if p.prompt:
                prompts.append(p.prompt)
            auth.extend(p.authority)
            bounds.extend(p.boundaries)
            if p.effort is not None:
                effort = p.effort
            extra.update(p.extra)
            kinds.add(p.kind)

        kind = "composed" if len(kinds) > 1 else next(iter(kinds), "generic")

        return cls(
            name=name,
            kind=kind,
            capabilities=frozenset(caps),
            resources=frozenset(res),
            prompt="\n\n".join(prompts),
            effort=effort,
            authority=tuple(auth),
            boundaries=tuple(bounds),
            extra=extra,
        )

    def __add__(self, other: Pattern) -> Pattern:
        return Pattern.compose([self, other], name=f"{self.name}+{other.name}")

    def __repr__(self) -> str:
        return f"Pattern(name='{self.name}', kind='{self.kind}')"


class Role(Pattern):
    """Behavioral pattern — mission, principles, anti-patterns, escalations.

    Loaded from roles/*.md with YAML frontmatter + markdown body.
    Kind is always 'role'.
    """

    kind: Literal["role"] = "role"

    mission: str = Field(default="", description="One-sentence behavioral mission.")

    principles: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Load-bearing behavioral rules.",
    )

    anti_patterns: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Failure modes specific to this role.",
    )

    escalations: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Conditions that trigger escalation to orchestrator.",
    )

    artifacts: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Concrete outputs this role produces.",
    )

    level: str = Field(
        default="L1",
        description="Behavioral purity: L1 (pure), L2 (artifact), L3 (domain), L4 (pack-specialized).",
    )

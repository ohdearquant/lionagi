"""OrchestrationConfig — one point in the orchestration variable space.

A frozen, hashable description of HOW to run a task: which pattern, roles,
modes, model, and grounding. The runner turns a config + a task into a
result; the same config run N times measures stability.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    """A named orchestration setup to benchmark.

    ``pattern``:
      - ``single``  : one agent does the whole task (the baseline).
      - ``fanout``  : N workers in parallel, optional synthesis.
      - ``flow``    : orchestrator plans a DAG; reactive spawning optional.

    ``roles`` are casts role names. ``critic_modes`` overlays cognitive modes
    on the critic specifically (e.g. ``("adversarial",)``) — the lever for the
    "does an adversarial critic cut false positives?" experiment.

    ``grounding`` is optional design-intent text injected into every worker's
    instruction — the lever for "does telling agents what's intended kill
    intended-behavior false positives?".
    """

    name: str
    pattern: str = "flow"  # single | fanout | flow
    roles: tuple[str, ...] = ("auditor", "critic", "synthesizer")
    # gpt-5.4-mini is the cheap-model representative: publicly available and
    # priced (codex spark is neither), so dollar comparisons are market-legible.
    model: str = "codex/gpt-5.4-mini"
    effort: str = "low"
    reactive: bool = False
    max_spawn: int = 5
    max_concurrent: int = 3
    critic_modes: tuple[str, ...] = ()
    grounding: str | None = None
    synthesis_role: str | None = "synthesizer"

    def key(self) -> str:
        """Stable identity for caching / result filenames."""
        parts = [
            self.name,
            self.pattern,
            "+".join(self.roles),
            self.model,
            self.effort,
            f"react={self.reactive}",
            f"cmodes={'+'.join(self.critic_modes) or '-'}",
            f"ground={'y' if self.grounding else 'n'}",
        ]
        return "__".join(parts)

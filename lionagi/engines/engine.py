# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Generic event-driven multi-agent engine over a lionagi Session (ADR-0075).

Domain-agnostic machinery only — *which* agents, *what* events they emit, *what*
observers react, and *what* post-stages run belong in subclasses:

- ``make_agent``      — build a casts-role Branch, grant it domain emissions
- ``run_team``        — sequential roster turns sharing prior output
- ``spawn`` / ``wait_quiescence`` — bounded recursive expansion
- ``events`` / ``by_type`` — query the reactive emission store (pile[Type])
- ``run``             — the pipeline lifecycle (subclass implements)

Agents emit domain events by being granted those emission types as capabilities
(``emits=``); when they run, the emissions reach the session bus and the
engine's observers fire — exactly the reactive-capability-bus path the DAG flow
uses for ``SpawnRequest``, here generalized to any domain event.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from lionagi.agent import AgentSpec, create_agent
from lionagi.casts.emission import build_emission_operable
from lionagi.protocols.generic.element import Element
from lionagi.session.session import Session

if TYPE_CHECKING:
    from lionagi.protocols.generic.pile import Pile
    from lionagi.session.branch import Branch

logger = logging.getLogger("lionagi.engines")
EventCallback = Callable[[dict[str, Any]], Any]


class EngineEvent(Element):
    """Base for engine domain events.

    An ``Element`` is structurally ``Observable`` (it has ``id: UUID`` + a
    timestamp), so a domain event subclassing this lives directly on the
    reactive bus and in the emission store — no ``Signal`` envelope. Observers
    key off the concrete subclass type (``engine.observe(FindingEmitted)``).
    """


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        try:
            return event.model_dump(mode="json")
        except Exception:
            return {}
    return {}


class Engine:
    """Event-driven multi-agent engine base.

    Parameters
    ----------
    session
        Session the agents share (created if omitted). Branches joined to it are
        auto-wired to its reactive observer, so their emissions reach the bus.
    model
        Default model spec for agents that don't pin their own.
    max_depth
        Hard cap on recursive expansion depth (subclasses enforce via ``depth``).
    max_concurrent
        Max concurrently-running spawned tasks.
    on_event
        Optional callback for streaming each recorded event (SSE-style).
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        model: str | None = None,
        max_depth: int = 3,
        max_concurrent: int = 5,
        on_event: EventCallback | None = None,
    ) -> None:
        self.session = session if session is not None else Session()
        self.model = model
        self.max_depth = max_depth
        self.on_event = on_event
        self._sem = asyncio.Semaphore(max_concurrent)
        self._active: set[asyncio.Task] = set()
        self._pending: deque = deque()

    # -- event store (the reactive emission store) ----------------------------

    @property
    def events(self) -> Pile:
        """The Pile of emitted domain events — query it: ``engine.events[Finding]``."""
        return self.session.observer.flow.items

    def by_type(self, event_type: type) -> list[Any]:
        """Stored events that are — or carry a field of — ``event_type``."""
        return self.session.observer.by_type(event_type)

    async def emit(self, event: Any) -> list[Any]:
        """Emit a domain event onto the bus (observers fire) and stream it."""
        results = await self.session.emit(event)
        self.notify(type(event).__name__, **_event_dict(event))
        return results

    def observe(self, event_type: Any, handler: Any = None, *, role: str | None = None) -> Any:
        """Register a reaction. Sugar over ``session.observe``; usable as a decorator."""
        return self.session.observe(event_type, handler, role=role)

    def notify(self, kind: str, **data: Any) -> None:
        if self.on_event:
            self.on_event({"type": kind, **data})

    # -- agent construction (casts) -------------------------------------------

    async def make_agent(
        self,
        role: str,
        *,
        name: str | None = None,
        modes: list[str] | None = None,
        model: str | None = None,
        tools: tuple[str, ...] = (),
        emits: tuple[type, ...] = (),
    ) -> Branch:
        """Build a casts-role agent, join it to the session, grant it emissions.

        ``emits`` are domain event types the agent may produce; granting them as
        capabilities is what lets its output reach the bus and fire observers.
        """
        spec = AgentSpec.compose(role, modes=modes, model=model or self.model, tools=tuple(tools))
        branch = await create_agent(spec, load_settings=False)
        if name:
            branch.name = name
        self.session.include_branches(branch)
        if emits:
            op = build_emission_operable(tuple(emits))
            if op is not None:
                branch.grant_capabilities(op)
        return branch

    # -- bounded recursion / quiescence ---------------------------------------

    def spawn(self, coro: Any) -> asyncio.Task | None:
        """Schedule a coroutine as a tracked background task (for recursion).

        If no loop is running yet, the coro is queued and started by the next
        ``drain_pending`` — mirroring the flow's reactive spawn discipline.
        """
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            self._pending.append(coro)
            return None
        self._active.add(task)
        task.add_done_callback(self._active.discard)
        return task

    def drain_pending(self) -> None:
        while self._pending:
            coro = self._pending.popleft()
            task = asyncio.ensure_future(coro)
            self._active.add(task)
            task.add_done_callback(self._active.discard)

    async def wait_quiescence(self) -> None:
        """Block until no spawned task remains (the termination condition)."""
        while self._active:
            await asyncio.gather(*list(self._active), return_exceptions=True)

    # -- team loop ------------------------------------------------------------

    async def run_team(
        self,
        team: list[Branch],
        instruction: str,
        *,
        carry_instruction: bool = False,
    ) -> str:
        """Run ``team`` agents in sequence, each building on the prior output.

        The first agent gets ``instruction``; later agents get the prior reply
        (and, when ``carry_instruction``, the original instruction too — useful
        when the instruction *is* the artifact under analysis, e.g. review).
        Each agent's emissions hit the bus mid-turn, so observers may spawn more
        work while the team runs. Returns the last reply text.
        """
        last = ""
        for i, branch in enumerate(team):
            if i == 0:
                turn = instruction
            elif carry_instruction:
                turn = f"{instruction}\n\n# Prior specialist output\n{last}"
            else:
                turn = f"Build on the prior work and continue:\n\n{last}"
            name = getattr(branch, "name", None) or f"agent-{i}"
            self.notify("agent_start", agent=name)
            try:
                async with self._sem:
                    res = await branch.operate(instruction=turn)
                last = str(res) if res is not None else ""
                self.notify("agent_done", agent=name, chars=len(last))
            except Exception as exc:  # an agent failure must not kill the team
                logger.warning("engine agent %s failed: %s", name, exc)
                self.notify("agent_error", agent=name, error=str(exc))
                last = f"[{name} failed: {exc}]"
            self.drain_pending()
        return last

    # -- lifecycle ------------------------------------------------------------

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the engine's pipeline. Subclass implements."""
        raise NotImplementedError("Engine subclass must implement run()")

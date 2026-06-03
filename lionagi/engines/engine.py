# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Generic event-driven multi-agent engine over a lionagi Session (ADR-0075).

Two objects, cleanly split:

- ``Engine`` — STATELESS config + reaction *logic*. Holds no session and no
  run-state, so one engine is reusable and runs many tasks (even concurrently).
- ``EngineRun`` — the per-``run()`` context: the ``Session``, dedup/active-task
  state, and the *operations* (emit, observe, by_type, make_agent, spawn,
  run_team, wait_quiescence). The session is the *run's* state, not the engine's.

``run(input, session=None)`` makes a fresh ``EngineRun`` (fresh session by
default; pass one to continue/share a conversation or memory). Agents emit
Observable domain events; the engine's observers react — exactly the
reactive-capability-bus path the DAG flow uses for ``SpawnRequest``, generalized
to any domain event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from lionagi.agent import AgentSpec, create_agent
from lionagi.casts.emission import build_emission_operable
from lionagi.session.session import Session
from lionagi.session.signal import NodeCompleted, NodeFailed, NodeQueued, NodeStarted

if TYPE_CHECKING:
    from lionagi.protocols.generic.pile import Pile
    from lionagi.session.branch import Branch

logger = logging.getLogger("lionagi.engines")
EventCallback = Callable[[dict[str, Any]], Any]


class EngineEvent(BaseModel):
    """Base for engine-only domain events — those with no casts-emission twin.

    A plain payload (e.g. :class:`~lionagi.engines.research.DepthRequested`). It
    needs no ``id`` of its own: ``session.emit`` envelopes any non-Observable
    payload in a :class:`~lionagi.session.signal.Signal`, so the emission store
    is uniformly ``Pile[Signal(data=event)]``. Events that DO have a casts twin
    subclass the emission directly (``FindingEmitted(Finding)``) and reuse its
    fields; observers key off the concrete subclass type either way.
    """


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        try:
            return event.model_dump(mode="json")
        except Exception:
            return {}
    return {}


class EngineRun:
    """One run's context: session + state + operations.

    Created per :meth:`Engine.run`. Owns the session, the dedup set, the
    in-flight task set, and the concurrency limiter — so concurrent runs of the
    same engine never collide. All the agent/event/recursion operations live
    here because they need this per-run state.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        session: Session | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.engine = engine
        self.session = session if session is not None else Session()
        self.on_event = on_event
        self._sem = asyncio.Semaphore(engine.max_concurrent)
        self._active: set[asyncio.Task] = set()
        self._pending: deque = deque()
        self._seen: set[str] = set()

    # -- event store (the reactive emission store) ----------------------------

    @property
    def events(self) -> Pile:
        """The Pile of emitted domain events — query it: ``run.events[Finding]``."""
        return self.session.observer.flow.items

    def by_type(self, event_type: type) -> list[Any]:
        """Domain-event payloads of ``event_type`` from the store.

        Domain events ride the bus inside a ``Signal`` — added by ``emit`` for a
        plain payload, or by ``operate`` for an agent exercising a grant — so
        this unwraps the envelope to its ``data``. A bare Observable whose own
        type matches (a lifecycle ``Signal``) is returned as-is.
        """
        out: list[Any] = []
        for e in self.session.observer.by_type(event_type):
            data = getattr(e, "data", None)
            out.append(data if isinstance(data, event_type) else e)
        return out

    async def emit(self, event: Any) -> list[Any]:
        """Emit a domain event onto the bus (observers fire) and stream it."""
        results = await self.session.emit(event)
        self.notify(type(event).__name__, **_event_dict(event))
        return results

    def observe(self, *keys: Any, handler: Any = None, role: str | None = None) -> Any:
        """Register a reaction. Sugar over ``session.observe``; usable as a decorator.

        Accepts one or more AND-composed conditions (type, Filter, or
        ``EventStatus``), with the handler positional/keyword/decorated."""
        return self.session.observe(*keys, handler=handler, role=role)

    def notify(self, kind: str, **data: Any) -> None:
        if self.on_event:
            self.on_event({"type": kind, **data})

    # -- dedup ----------------------------------------------------------------

    def seen(self, key: str) -> bool:
        """Return True if *key* (normalized) was seen before; otherwise mark + False."""
        norm = key.strip().lower()
        if norm in self._seen:
            return True
        self._seen.add(norm)
        return False

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
        """Build a casts-role agent, join it to the session, grant it emissions."""
        spec = AgentSpec.compose(
            role, modes=modes, model=model or self.engine.model, tools=tuple(tools)
        )
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
        """Schedule a coroutine as a tracked background task (for recursion)."""
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

    # -- DAG execution --------------------------------------------------------

    async def run_dag(
        self,
        graph: Any,
        *,
        reactive: bool = False,
        spawn_type: type | None = None,
        node_builder: Any = None,
        max_spawn: int = 50,
        max_concurrent: int = 5,
        verbose: bool = False,
        escalation_tier: str | None = None,
    ) -> dict[str, Any]:
        """Execute a prebuilt operation DAG on the run's session.

        The complement to :meth:`run_team`: where ``run_team`` sequences a
        roster, ``run_dag`` runs a dependency graph through the reactive
        executor — the second of the two execution shapes (ADR-0075 §4). As each
        node starts/finishes it emits a ``NodeQueued`` / ``NodeStarted`` /
        ``NodeCompleted`` / ``NodeFailed`` onto the bus, so persistence, Studio
        segments, and progress display subscribe via ``observe`` instead of a
        bespoke ``on_progress`` callback. With ``reactive`` a worker may emit a
        ``spawn_type`` payload to grow the live DAG (``node_builder`` turns it
        into a node). Returns the ``session.flow`` result dict.
        """
        emits: list[asyncio.Future] = []

        def _on_progress(op_id: str, name: str, status: str, elapsed: float) -> None:
            if status == "queued":
                sig: Any = NodeQueued(op_id=op_id, name=name)
            elif status == "started":
                sig = NodeStarted(op_id=op_id, name=name)
            elif status == "completed":
                sig = NodeCompleted(op_id=op_id, name=name, elapsed=elapsed)
            elif status == "failed":
                sig = NodeFailed(op_id=op_id, name=name, elapsed=elapsed)
            else:
                return
            # on_progress is sync (called from inside the executor); fan the
            # signal onto the async bus. Collected so the caller can await the
            # observers before reading state they populate. The suppress guards
            # the no-running-loop case (nothing would observe anyway).
            with contextlib.suppress(RuntimeError):
                emits.append(asyncio.ensure_future(self.session.emit(sig)))

        result = await self.session.flow(
            graph,
            reactive=reactive,
            spawn_type=spawn_type,
            node_builder=node_builder,
            max_spawn=max_spawn,
            max_concurrent=max_concurrent,
            verbose=verbose,
            on_progress=_on_progress,
            escalation_tier=escalation_tier,
        )
        if emits:
            await asyncio.gather(*emits, return_exceptions=True)
        return result


class Engine:
    """Stateless event-driven engine base — config + reaction logic.

    Holds no session and no run-state; one instance is reusable and may run many
    tasks concurrently. Subclasses define the domain: which agents, what events,
    which observers react, and the pipeline in ``_run``.

    Parameters
    ----------
    model
        Default model spec for agents that don't pin their own.
    max_depth
        Hard cap on recursive expansion depth (subclasses enforce via ``depth``).
    max_concurrent
        Max concurrently-running spawned tasks, per run.
    """

    run_context_cls: type[EngineRun] = EngineRun

    def __init__(
        self,
        *,
        model: str | None = None,
        max_depth: int = 3,
        max_concurrent: int = 5,
    ) -> None:
        self.model = model
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent

    def new_run(
        self,
        *,
        session: Session | None = None,
        on_event: EventCallback | None = None,
    ) -> EngineRun:
        """Create a fresh per-run context (fresh session unless one is passed)."""
        return self.run_context_cls(self, session=session, on_event=on_event)

    async def run(
        self,
        *args: Any,
        session: Session | None = None,
        on_event: EventCallback | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute the pipeline in a fresh run context. Returns the engine's result."""
        run = self.new_run(session=session, on_event=on_event)
        return await self._run(run, *args, **kwargs)

    async def _run(self, run: EngineRun, *args: Any, **kwargs: Any) -> Any:
        """The pipeline lifecycle, operating on a per-run context. Subclass implements."""
        raise NotImplementedError("Engine subclass must implement _run(run, ...)")

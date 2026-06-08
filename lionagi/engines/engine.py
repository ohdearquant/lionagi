# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Event-driven multi-agent engine: stateless Engine + per-run EngineRun."""

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
from lionagi.ln.concurrency import Semaphore, gather
from lionagi.session.session import Session
from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted

if TYPE_CHECKING:
    from lionagi.protocols.generic.pile import Pile
    from lionagi.session.branch import Branch

logger = logging.getLogger("lionagi.engines")
EventCallback = Callable[[dict[str, Any]], Any]


class EngineEvent(BaseModel):
    """Base for engine-only domain events with no casts-emission twin."""


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        try:
            return event.model_dump(mode="json")
        except Exception:
            return {}
    return {}


class EngineRun:
    """Per-run context: session, dedup set, in-flight tasks, concurrency limiter."""

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
        self._sem = Semaphore(engine.max_concurrent)
        self._active: set[asyncio.Task] = set()
        self._pending: deque = deque()
        self._seen: set[str] = set()

    @property
    def events(self) -> Pile:
        return self.session.observer.flow.items

    def by_type(self, event_type: type) -> list[Any]:
        out: list[Any] = []
        for e in self.session.observer.by_type(event_type):
            data = getattr(e, "data", None)
            out.append(data if isinstance(data, event_type) else e)
        return out

    async def emit(self, event: Any) -> list[Any]:
        results = await self.session.emit(event)
        self.notify(type(event).__name__, **_event_dict(event))
        return results

    def observe(self, *keys: Any, handler: Any = None, role: str | None = None) -> Any:
        return self.session.observe(*keys, handler=handler, role=role)

    def notify(self, kind: str, **data: Any) -> None:
        if self.on_event:
            self.on_event({"type": kind, **data})

    def seen(self, key: str) -> bool:
        norm = key.strip().lower()
        if norm in self._seen:
            return True
        self._seen.add(norm)
        return False

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

    def spawn(self, coro: Any) -> asyncio.Task | None:
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

    async def cancel_active(self) -> None:
        """Cancel all in-flight spawned tasks and await completion."""
        if not self._active:
            return
        for t in list(self._active):
            t.cancel()
        await gather(*list(self._active), return_exceptions=True)
        # Callbacks remove tasks from _active as they settle.
        self._active.clear()

    async def wait_quiescence(self) -> None:
        """Block until no spawned task remains; re-raise accumulated failures."""
        task_errors: list[BaseException] = []
        while self._active:
            results = await gather(*list(self._active), return_exceptions=True)
            task_errors.extend(
                r
                for r in results
                if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)
            )
        if task_errors:
            for exc in task_errors:
                logger.error("engine spawned task failed: %s", exc)
            from lionagi.ln.concurrency._compat import ExceptionGroup as _ExceptionGroup

            if len(task_errors) == 1:
                raise task_errors[0] from None
            raise _ExceptionGroup("engine spawned task(s) failed", task_errors)

    async def run_team(
        self,
        team: list[Branch],
        instruction: str,
        *,
        carry_instruction: bool = False,
    ) -> str:
        """Run agents in sequence, each building on the prior output."""
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
    ) -> dict[str, Any]:
        """Execute a prebuilt operation DAG on the run's session."""
        emits: list[asyncio.Future] = []

        def _on_progress(op_id: str, name: str, status: str, elapsed: float) -> None:
            if status == "started":
                sig: Any = NodeStarted(op_id=op_id, name=name)
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
        )
        if emits:
            await gather(*emits, return_exceptions=True)
        return result


class Engine:
    """Stateless event-driven engine base; subclasses implement _run()."""

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
        return self.run_context_cls(self, session=session, on_event=on_event)

    async def run(
        self,
        *args: Any,
        session: Session | None = None,
        on_event: EventCallback | None = None,
        **kwargs: Any,
    ) -> Any:
        run = self.new_run(session=session, on_event=on_event)
        return await self._run(run, *args, **kwargs)

    async def _run(self, run: EngineRun, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Engine subclass must implement _run(run, ...)")

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Event-driven multi-agent engine: stateless Engine + per-run EngineRun."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from lionagi.agent import AgentSpec, create_agent
from lionagi.ln.concurrency import Semaphore, gather
from lionagi.ln.concurrency._compat import (
    ExceptionGroup,
    get_exception_group_exceptions,
    is_exception_group,
)
from lionagi.ln.types import TypeFilter
from lionagi.session.session import Session
from lionagi.session.signal import Signal

if TYPE_CHECKING:
    from lionagi.protocols.generic.pile import Pile
    from lionagi.session.branch import Branch

logger = logging.getLogger("lionagi.engines")
EventCallback = Callable[[dict[str, Any]], Any]

# Sentinel used by Engine.run() to distinguish "partial export produced a
# result" from "partial export returned None intentionally".
_UNSET: Any = object()

# Maximum wall-clock seconds allowed for the post-cancellation partial export.
# Kept short so a hung synthesis LLM call cannot extend the run unboundedly.
_PARTIAL_EXPORT_TIMEOUT_S: float = 120.0


class ChainEvent(BaseModel):
    """Mixin: engine-assigned chain id for audit-trail reconstruction."""

    eid: str = Field(default="", description="Leave empty — the engine assigns this id.")


class EngineEvent(BaseModel):
    """Base for engine-only domain events with no casts-emission twin."""

    model_config = ConfigDict(extra="forbid")


class JudgeVerdict(EngineEvent):
    """The quality gate's call on whether a work item is worth expanding."""

    subject: str = Field(default="", description="The id of the item being judged.")
    allow: bool = Field(default=True, description="True to expand, false to stop this branch.")
    reason: str = Field(default="", description="Why — one concrete sentence.")


class EngineBudgetError(RuntimeError):
    """The run's hard agent budget is exhausted; no further agents may be made."""


def _is_all_budget_error(exc: BaseException) -> bool:
    """True iff every leaf exception is an EngineBudgetError, recursing into nested groups."""
    if isinstance(exc, EngineBudgetError):
        return True
    if is_exception_group(exc):
        return all(_is_all_budget_error(e) for e in get_exception_group_exceptions(exc))
    return False


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        try:
            return event.model_dump(mode="json")
        except Exception:
            return {}
    return {}


def _safe_event_dict(event: Any) -> dict[str, Any]:
    """Like _event_dict but renames a 'kind' key to 'event_kind' to avoid clashing with notify(kind, **data)."""
    d = _event_dict(event)
    if "kind" in d:
        d["event_kind"] = d.pop("kind")
    return d


def _judge_instruction(eid: str, subject: str, context: str) -> str:
    return (
        "You are the quality gate of an autonomous pipeline. Decide whether this "
        "work item deserves further expansion (it will spawn more agents).\n\n"
        f"# Root objective\n{context or '(none stated)'}\n\n"
        f"# Item ({eid})\n{subject}\n\n"
        "Reject if it is off-topic for the root objective, duplicative, trivial, "
        "or unsafe. Otherwise pass. Emit a judge_verdict with "
        f"subject='{eid}', allow (true|false), reason. If you cannot emit, reply "
        "with exactly PASS or REJECT."
    )


def _repair_instruction(schema_hint: str) -> str:
    return (
        "Your previous response produced no valid emission, so the pipeline "
        "received nothing. Emit now, inside a fenced ```json code block. Common "
        "failures: JSON not fenced, wrong top-level key, misspelled field names, "
        "extra fields not in the schema (forbidden), prose instead of JSON. "
        f"{schema_hint} Emit only the fenced block(s); no other text is needed."
    )


def _minimal_valid_json(m: type) -> dict:
    """Build a minimal valid serialized dict for Pydantic model *m*, filling required fields with placeholder values."""
    from pydantic import BaseModel

    kwargs: dict = {}
    for fname, field in m.model_fields.items():
        if not field.is_required():
            continue
        ann = field.annotation
        origin = getattr(ann, "__origin__", None)
        if ann is str:
            kwargs[fname] = "string"
        elif ann is int:
            kwargs[fname] = 0
        elif ann is float:
            kwargs[fname] = 0.0
        elif ann is bool:
            kwargs[fname] = False
        elif origin is list:
            kwargs[fname] = []
        elif origin is dict:
            kwargs[fname] = {}
        else:
            kwargs[fname] = "value"
    if issubclass(m, BaseModel):
        return m(**kwargs).model_dump()
    return kwargs  # pragma: no cover


def _cli_repair_instruction(schema_hint: str, emits: tuple[type, ...]) -> str:
    """Repair instruction for CLI workers; supplies a complete fenced-JSON example because CLI workers emit prose, not a malformed schema."""
    import json as _json

    from lionagi.casts.emission import field_name_for

    obj: dict = {}
    for m in emits:
        key = field_name_for(m)
        obj[key] = _minimal_valid_json(m)
    example = f"```json\n{_json.dumps(obj, indent=2)}\n```" if obj else ""
    return (
        "Your previous response contained no fenced JSON block, so the pipeline "
        "received nothing. Reply with ONLY a fenced ```json block containing the "
        "emission object — no prose, no tool calls, no other text.\n\n"
        f"{schema_hint}\n\n"
        f"Example structure:\n{example}"
    ).strip()


def emission_keys(emits: tuple[type, ...]) -> str:
    """Render the top-level emission key(s) for a repair hint."""
    from lionagi.casts.emission import field_name_for

    names = ", ".join(f"'{field_name_for(m)}'" for m in emits)
    return f"Expected top-level key(s): {names}." if names else ""


class EngineRun:
    """Per-run context: session, dedup set, in-flight tasks, semaphore, and hard budget (agents + deadline)."""

    def __init__(
        self,
        engine: Engine,
        *,
        session: Session | None = None,
        on_event: EventCallback | None = None,
        on_branch_created: Callable[[Any], None] | None = None,
    ) -> None:
        self.engine = engine
        self.session = session if session is not None else Session()
        self.on_event = on_event
        self._on_branch_created = on_branch_created
        self.root: str = ""
        self.agents_made: int = 0
        self._sem = Semaphore(engine.max_concurrent)
        self._active: set[asyncio.Task] = set()
        self._pending: deque = deque()
        self._seen: set[str] = set()
        self._t0 = monotonic()
        self._deadline = None if engine.deadline_s is None else self._t0 + engine.deadline_s
        self._budget_notified = False
        # Holds the asyncio.Task wrapping the engine's _run() coroutine.
        # Set by Engine.run() before awaiting; the deadline watchdog cancels it
        # so in-flight sequential work (operate_with_repair, branch.operate) is
        # interrupted promptly rather than continuing past the deadline.
        self._run_task: asyncio.Task | None = None
        # Collects emission-missing diagnostics so the CLI can write them to
        # the engine_runs.error column even when the overall status is "completed".
        # Each entry is a string like "<agent> x<attempts>".
        self._emission_failures: list[str] = []
        # Collects terminal sub-agent failures (e.g. missing API key) so a run
        # where every agent errored can be surfaced as failed instead of green.
        self._agent_errors: list[str] = []

    @property
    def events(self) -> Pile:
        return self.session.observer.flow.items

    def by_type(self, event_type: type) -> list[Any]:
        """Return stored payloads matching *event_type*, unwrapping Signal envelopes and capability bundles."""
        obs = self.session.observer
        flt = TypeFilter(event_type)
        out: list[Any] = []
        for e in obs.flow.items:
            payload = e.data if isinstance(e, Signal) else e
            out.extend(obs._match(flt, e, payload))
        return out

    # -- resource budget --------------------------------------------------------

    def budget_left(self) -> bool:
        """True while the run is within agent count and deadline bounds."""
        if self.agents_made >= self.engine.max_agents:
            return False
        return not (self._deadline is not None and monotonic() >= self._deadline)

    def _notify_budget_once(self, reason: str) -> None:
        if not self._budget_notified:
            self._budget_notified = True
            self.notify(
                "budget_exhausted",
                reason=reason,
                agents_made=self.agents_made,
                elapsed=round(monotonic() - self._t0, 1),
            )

    async def emit(self, event: Any) -> list[Any]:
        results = await self.session.emit(event)
        self.notify(type(event).__name__, **_event_dict(event))
        return results

    def observe(self, *keys: Any, handler: Any = None, role: str | None = None) -> Any:
        return self.session.observe(*keys, handler=handler, role=role)

    def notify(self, kind: str, **data: Any) -> None:
        if kind == "agent_error":
            self._agent_errors.append(f"{data.get('agent')}: {data.get('error')}")
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
        permissions: Any = None,
        cwd: str | None = None,
        secure: bool = True,
        exempt: bool = False,
        mcp_servers: list[str] | None = None,
        extra_prompt: str | None = None,
    ) -> Branch:
        # ``exempt`` is for terminal stages (synthesis/verdict) that must run
        # even when the expansion budget is gone — degrade, don't lose the run.
        if not exempt and not self.budget_left():
            self._notify_budget_once("make_agent")
            raise EngineBudgetError(
                f"agent budget exhausted ({self.agents_made}/{self.engine.max_agents})"
            )
        self.agents_made += 1
        spec = AgentSpec.compose(
            role,
            modes=modes,
            model=model or self.engine.model,
            tools=tuple(tools),
            permissions=permissions,
            emits=tuple(emits) if emits else None,
            cwd=cwd,
            system_prompt=extra_prompt,
        )
        if mcp_servers is not None:
            spec.mcp_servers = mcp_servers
        if secure and tools:
            from lionagi.agent.spec import _wire_secure_guards

            _wire_secure_guards(spec, cwd)
        # create_agent is the single grant site: emits is threaded through the
        # spec above, so capabilities are granted once during construction.
        branch = await create_agent(spec, load_settings=False)
        if name:
            branch.name = name
        self.session.include_branches(branch)
        if self._on_branch_created is not None:
            self._on_branch_created(branch)
        return branch

    async def operate_with_repair(
        self,
        branch: Branch,
        instruction: str,
        *,
        arrived: Callable[[], bool],
        emits: tuple[type, ...] = (),
        retries: int = 1,
    ) -> Any:
        """Operate then re-prompt up to *retries* times while *arrived*() is false; CLI workers get a full fenced-JSON example, API workers get key hints."""
        res = await branch.operate(instruction=instruction)
        attempt = 0
        # Determine repair template once: CLI workers need the full example form.
        is_cli = bool(getattr(getattr(branch, "chat_model", None), "is_cli", False))
        hint = emission_keys(emits)
        while not arrived() and attempt < retries:
            attempt += 1
            self.notify(
                "emission_repair",
                agent=getattr(branch, "name", "") or "",
                attempt=attempt,
                cli_worker=is_cli,
            )
            if is_cli:
                repair_msg = _cli_repair_instruction(hint, emits)
            else:
                repair_msg = _repair_instruction(hint)
            res = await branch.operate(instruction=repair_msg)
        if retries and not arrived():
            _agent_name = getattr(branch, "name", "") or ""
            _attempts = attempt + 1
            self.notify(
                "emission_missing",
                agent=_agent_name,
                attempts=_attempts,
            )
            self._emission_failures.append(
                f"{_agent_name} x{_attempts}" if _agent_name else f"x{_attempts}"
            )
        return res

    def spawn(self, coro: Any) -> asyncio.Task | None:
        if not self.budget_left():
            self._notify_budget_once("spawn")
            with contextlib.suppress(Exception):
                coro.close()
            return None
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
        """Cancel and await all in-flight spawned tasks.

        Waits up to ``engine.cancel_timeout_s`` for tasks to finish after
        cancellation is requested.  Tasks that do not settle within that window
        (e.g. they catch CancelledError and loop) are abandoned: a loud warning
        is logged naming the count, and cancel_active() returns so the caller's
        lifetime guarantee is preserved.  Cooperative tasks that finish before
        the deadline are awaited normally — the timeout path only fires when at
        least one task remains after the window expires.
        """
        if not self._active:
            return
        tasks = list(self._active)
        for t in tasks:
            t.cancel()
        timeout = self.engine.cancel_timeout_s
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            count = len(pending)
            names = [t.get_name() if hasattr(t, "get_name") else repr(t) for t in pending]
            logger.warning(
                "cancel_active: %d task(s) did not finish within %.1fs and were abandoned: %s",
                count,
                timeout,
                names,
            )
            # Issue a final cancel so the loop can GC them eventually.
            for t in pending:
                t.cancel()
        # Callbacks will discard done tasks; clear the rest explicitly.
        self._active.clear()

    async def _deadline_watchdog(self) -> None:
        """Sleep until the deadline, then cancel _run_task (and spawned tasks via Engine.run's finally block)."""
        if self._deadline is None:
            return
        delay = self._deadline - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._notify_budget_once("deadline_watchdog")
        # Cancel _run_task first so the in-flight sequential stage
        # (branch.operate / operate_with_repair) is interrupted promptly.
        # Spawned tasks (_active) are drained in Engine.run()'s finally block.
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    async def wait_quiescence(self) -> None:
        """Block until all spawned tasks settle; re-raise any non-cancellation, non-budget failures.

        A spawned task hitting EngineBudgetError is a benign "expansion
        stopped" signal (discretionary work declined, not a crash) — the same
        grace already given to asyncio.CancelledError here.
        """
        task_errors: list[BaseException] = []
        while self._active:
            results = await gather(*list(self._active), return_exceptions=True)
            task_errors.extend(
                r
                for r in results
                if isinstance(r, BaseException)
                and not isinstance(r, asyncio.CancelledError | EngineBudgetError)
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
        """Run agents sequentially, each building on the prior agent's output."""
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
        executor_ref: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a prebuilt operation DAG on the run's session and return operation results."""
        from .flow_signals import flow_progress_signals  # noqa: PLC0415

        async with flow_progress_signals(self.session, graph) as on_progress:
            result = await self.session.flow(
                graph,
                context=context,
                reactive=reactive,
                spawn_type=spawn_type,
                node_builder=node_builder,
                max_spawn=max_spawn,
                max_concurrent=max_concurrent,
                verbose=verbose,
                on_progress=on_progress,
                executor_ref=executor_ref,
            )
        return result


class ChainRun(EngineRun):
    """Shared base for chain-style runs: typed event store, eid stamping, and collect/emit/find helpers."""

    #: Set by each subclass: the chain-event base class for this pipeline.
    _chain_event_cls: type = object
    #: Set by each subclass: the ``_EVENT_PREFIX`` dict for this pipeline.
    _event_prefix_map: dict = {}

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self.store: dict[type, list[Any]] = {t: [] for t in self._event_prefix_map}
        self._eid_counts: dict[str, int] = {}
        self._index: dict[str, Any] = {}

    def collect(self, event: Any) -> Any:
        """Stamp the engine-assigned eid, store the event, and notify on_event; the single notification path for all chain events."""
        prefix = self._event_prefix_map.get(type(event), "N")
        n = self._eid_counts.get(prefix, 0) + 1
        self._eid_counts[prefix] = n
        event.eid = f"{prefix}-{n}"
        self.store.setdefault(type(event), []).append(event)
        self._index[event.eid] = event
        self.notify(type(event).__name__, **_safe_event_dict(event))
        return event

    async def emit(self, event: Any) -> list[Any]:
        """Emit onto the session bus; suppresses base notify for chain events to avoid double-delivery."""
        results = await self.session.emit(event)
        if not isinstance(event, self._chain_event_cls):
            self.notify(type(event).__name__, **_event_dict(event))
        return results

    def find(self, eid: str) -> Any | None:
        return self._index.get(eid)

    def events_of(self, event_type: type) -> list[Any]:
        return self.store.get(event_type, [])


class EngineResult(str):
    """Return type of Engine.run(): a str (back-compat) carrying the run's structured outcome.

    ``str(result)`` and ``result.text`` are the same synthesized text. ``.run``
    is a live EngineRun handle — do not retain it past reading the result, it
    keeps the whole Session (and its branches) alive.
    """

    text: str
    skipped: list[str]
    degraded: bool
    degrade_reason: str
    run: EngineRun
    _events_by_type: Callable[[type], list[Any]]

    def __new__(
        cls,
        text: str,
        *,
        events_by_type: Callable[[type], list[Any]],
        skipped: list[str],
        degraded: bool,
        run: EngineRun,
        degrade_reason: str = "",
    ) -> EngineResult:
        self = super().__new__(cls, text)
        self.text = str(self)
        self._events_by_type = events_by_type
        self.skipped = list(skipped)
        self.degraded = bool(degraded)
        self.degrade_reason = degrade_reason
        self.run = run
        return self

    def events_by_type(self, event_type: type) -> list[Any]:
        """Snapshot of the run's stored events matching *event_type*."""
        return self._events_by_type(event_type)


class Engine:
    """Stateless event-driven engine base; subclasses implement ``_run()``. See docs/reference/engines.md for parameter details."""

    run_context_cls: type[EngineRun] = EngineRun

    def __init__(
        self,
        *,
        model: str | None = None,
        models: dict[str, str] | None = None,
        max_depth: int = 3,
        max_concurrent: int = 5,
        max_agents: int = 50,
        deadline_s: float | None = None,
        judge_model: str | None = None,
        judge_role: str = "critic",
        cancel_timeout_s: float = 30.0,
    ) -> None:
        self.model = model
        self.models = dict(models) if models else {}
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.max_agents = max_agents
        self.deadline_s = deadline_s
        self.judge_model = judge_model
        self.judge_role = judge_role
        self.cancel_timeout_s = cancel_timeout_s

    def model_for(self, stage: str) -> str | None:
        return self.models.get(stage) or self.model

    async def judge(self, run: EngineRun, eid: str, subject: str) -> bool:
        """Quality gate before an expansion point; returns True to expand, False to stop. No-op when judge_model is unset. Errors fail open."""
        if not self.judge_model:
            return True
        try:
            async with run._sem:
                agent = await run.make_agent(
                    self.judge_role,
                    name=f"judge-{eid}",
                    model=self.judge_model,
                    emits=(JudgeVerdict,),
                )
                res = await agent.operate(instruction=_judge_instruction(eid, subject, run.root))
            for v in run.by_type(JudgeVerdict):
                if v.subject == eid:
                    if not v.allow:
                        run.notify("gated", eid=eid, reason=v.reason)
                    return v.allow
            allow = "reject" not in str(res or "").lower()
            if not allow:
                run.notify("gated", eid=eid, reason="text-reject")
            return allow
        except EngineBudgetError:
            return False
        except Exception as exc:
            run.notify("judge_error", eid=eid, error=str(exc))
            return True

    def new_run(
        self,
        *,
        session: Session | None = None,
        on_event: EventCallback | None = None,
        on_branch_created: Callable[[Any], None] | None = None,
    ) -> EngineRun:
        return self.run_context_cls(
            self, session=session, on_event=on_event, on_branch_created=on_branch_created
        )

    async def _degrade_export(self, run: EngineRun, args: tuple, kwargs: dict) -> Any:
        """Cancel in-flight spawned tasks, then run _partial_export shielded + timeout-bounded.

        Shared by the deadline (CancelledError) and root-budget (EngineBudgetError)
        degrade paths in run(). Returns _UNSET if the export itself failed or
        timed out (logged, not raised) — CancelledError from an external cancel
        during the shielded phase still propagates.
        """
        # Cancel any background tasks spawned via run.spawn() so synthesis
        # sees a stable snapshot and no work burns tokens past budget
        # exhaustion.  cancel_active() is a no-op if _active is already empty.
        if run._active:
            await run.cancel_active()
        # Synthesize and export under asyncio.shield so the await runs outside
        # the cancelled scope.  A hard timeout bounds the phase so a hung
        # synthesis call cannot extend the run indefinitely.
        partial_coro = self._partial_export(run, *args, **kwargs)
        partial_task = asyncio.ensure_future(partial_coro)
        try:
            return await asyncio.wait_for(
                asyncio.shield(partial_task),
                timeout=_PARTIAL_EXPORT_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            # The caller cancelled Engine.run() while we were in the
            # partial-export phase.  wait_for converts its own internal
            # timeout into TimeoutError, so a CancelledError here is always
            # external — clean up partial_task and re-raise so the caller's
            # cancellation is honoured.
            if not partial_task.done():
                partial_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await partial_task
            raise
        except (asyncio.TimeoutError, Exception) as exc:
            logger.warning("partial export after degrade failed: %s", exc)
            if not partial_task.done():
                partial_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await partial_task
            return _UNSET

    def _wrap_result(self, result: Any, run: EngineRun, *, degrade_reason: str) -> Any:
        """Wrap a str _run()/_partial_export() result into EngineResult; pass through anything else unchanged (e.g. CodingEngine's structured CodeResultRecorded)."""
        if not isinstance(result, str) or isinstance(result, EngineResult):
            return result
        return EngineResult(
            result,
            events_by_type=run.by_type,
            skipped=list(run._emission_failures),
            degraded=bool(degrade_reason),
            degrade_reason=degrade_reason,
            run=run,
        )

    async def run(
        self,
        *args: Any,
        session: Session | None = None,
        on_event: EventCallback | None = None,
        on_branch_created: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute the engine pipeline; on internal budget cancellation calls _partial_export instead of raising. External cancellation propagates as CancelledError."""
        run = self.new_run(session=session, on_event=on_event, on_branch_created=on_branch_created)
        # Reset per-run diagnostics on the engine instance so a reused engine
        # never carries emission failures from a previous run into the next one.
        self._emission_failures: list[str] = []
        self._agent_errors: list[str] = []
        self._total_agent_failure: bool = False
        watchdog: asyncio.Task | None = None
        if run._deadline is not None:
            watchdog = asyncio.ensure_future(run._deadline_watchdog())
        # Wrap _run() in a task so the deadline watchdog can cancel it.
        # This is what makes the deadline bound *all* in-flight work — not just
        # spawned background tasks, but also sequential awaits inside _run().
        run_task = asyncio.ensure_future(self._run(run, *args, **kwargs))
        run._run_task = run_task
        result: Any = _UNSET
        partial_result: Any = _UNSET
        degrade_reason: str = ""
        try:
            result = await run_task
            # R5: a run that reached the success path may still have silently
            # dropped a discretionary budget-capped subtree (wait_quiescence
            # filters EngineBudgetError out of task_errors) — flag it rather
            # than returning a clean-looking result that hides the truncation.
            if run._budget_notified:
                degrade_reason = "budget"
        except asyncio.CancelledError:
            # Distinguish internal cancellation (engine's own deadline/budget
            # watchdog) from external cancellation (the caller cancelled run()).
            # The watchdog sets _budget_notified before cancelling run_task.
            # On Python >=3.11, task.cancelling() > 0 additionally detects a
            # simultaneous external cancel (caller + watchdog fire at the same
            # instant).  On Python 3.10, task.cancelling() does not exist, so
            # we fall back to _budget_notified alone.  The concrete consequence
            # on 3.10: if the caller cancels at the exact instant the budget is
            # hit, _budget_notified wins and partial export runs instead of
            # raising — an acceptable edge-case given the platform limitation.
            current = asyncio.current_task()
            caller_cancelled = (
                current is not None and hasattr(current, "cancelling") and current.cancelling() > 0
            )
            if run._budget_notified and not caller_cancelled:
                # Internal cancellation only — the deadline watchdog is the
                # only thing that cancels run_task, so this is a deadline hit.
                degrade_reason = "deadline"
                partial_result = await self._degrade_export(run, args, kwargs)
            else:
                # External cancellation — surface it after cleanup (below).
                raise
        except (EngineBudgetError, ExceptionGroup) as exc:
            # A root-level (non-spawned) make_agent() call hit the budget and
            # raised straight out of _run() — e.g. a review's dimension
            # fan-out gather, or a sequential plan/implement stage. Route to
            # partial-export instead of letting it crash the run. Masking
            # guard: a non-budget leaf anywhere in the group (including
            # nested groups) must not be laundered into a partial — re-raise
            # so the real error surfaces.
            if not _is_all_budget_error(exc):
                raise
            degrade_reason = "budget"
            partial_result = await self._degrade_export(run, args, kwargs)
        finally:
            # Copy per-run emission diagnostics back onto the engine instance so
            # the CLI read site (getattr(engine, "_emission_failures", [])) sees
            # the real list regardless of which return/exception path was taken.
            # Uses a fresh list copy so no shared-reference aliasing between runs.
            self._emission_failures = list(run._emission_failures)
            # Copy per-run agent-failure diagnostics the same way; flag total
            # failure only when every agent made for this run terminally
            # errored, so partial/soft-empty runs are never over-flagged.
            self._agent_errors = list(run._agent_errors)
            self._total_agent_failure = (
                run.agents_made > 0 and len(run._agent_errors) >= run.agents_made
            )
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
            # Drain any tasks still in _active so nothing leaks past run().
            # Runs whether _run() succeeded, failed, or was cancelled by the
            # deadline watchdog.  The drain is shielded so it completes even
            # if the caller cancels run() mid-cleanup, but that external
            # CancelledError is re-raised — swallowing it would hand the
            # caller a stale _run() result and break structured cancellation.
            if run._active:
                drain = asyncio.ensure_future(run.cancel_active())
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    if drain.cancelled():
                        raise
                    # External cancellation of run() itself.  The drain keeps
                    # running under shield — wait it out (absorbing repeated
                    # cancellations) so no run-owned task outlives run() from
                    # the caller's perspective, then surface the cancellation.
                    while not drain.done():
                        try:
                            await asyncio.shield(drain)
                        except asyncio.CancelledError:
                            continue
                    if not drain.cancelled() and drain.exception() is not None:
                        logger.exception(
                            "engine active-task drain failed",
                            exc_info=drain.exception(),
                        )
                    raise
                except Exception:
                    logger.exception("engine active-task drain failed")
        if partial_result is not _UNSET:
            result = partial_result
        if result is _UNSET:
            result = None
        return self._wrap_result(result, run, degrade_reason=degrade_reason)

    async def _partial_export(self, run: EngineRun, *args: Any, **kwargs: Any) -> Any:
        """Called under asyncio.shield after budget cancellation; override in subclasses to return a partial result. Default returns None."""
        return None

    async def _run(self, run: EngineRun, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Engine subclass must implement _run(run, ...)")

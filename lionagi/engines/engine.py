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
from lionagi.ln.types import TypeFilter
from lionagi.session.session import Session
from lionagi.session.signal import NodeCompleted, NodeFailed, NodeStarted, Signal

if TYPE_CHECKING:
    from lionagi.protocols.generic.pile import Pile
    from lionagi.session.branch import Branch

logger = logging.getLogger("lionagi.engines")
EventCallback = Callable[[dict[str, Any]], Any]


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


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        try:
            return event.model_dump(mode="json")
        except Exception:
            return {}
    return {}


def _safe_event_dict(event: Any) -> dict[str, Any]:
    """Like _event_dict but renames keys that clash with notify(kind, **data).

    The ``kind`` parameter name is reserved by ``EngineRun.notify``; any event
    field named ``kind`` (e.g. ``EvidenceCollected.kind``) must be renamed to
    avoid a TypeError when the dict is splatted into the call."""
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
    """Build a minimal valid serialised dict for a Pydantic model *m*.

    Fills required fields with type-appropriate placeholder values so the
    returned dict passes ``m.model_validate()``.  Optional fields retain their
    defaults.  Used to produce a syntactically-valid JSON example in the CLI
    repair instruction."""
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
    """Repair instruction for CLI workers (claude_code, codex).

    CLI workers stream free-form output — their failure mode is prose with no
    fenced JSON block, not a malformed schema.  The repair turn supplies a
    complete syntactically-valid fenced-JSON example so the worker can copy the
    structure exactly, since CLI endpoints may not receive the Operable schema
    the same way API workers do.

    The example object uses single braces and real field values so that a
    worker who copies it literally produces a block that ``json.loads()``
    accepts without modification.
    """
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
    """Per-run context: session, dedup set, in-flight tasks, concurrency limiter,
    and the hard resource budget (agent count + wall-clock deadline)."""

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

    @property
    def events(self) -> Pile:
        return self.session.observer.flow.items

    def by_type(self, event_type: type) -> list[Any]:
        """Stored payloads matching *event_type* — unwraps Signal envelopes AND
        capability bundles (an agent's emission arrives as a StructuredOutput
        whose bundle carries the typed event as a field)."""
        obs = self.session.observer
        flt = TypeFilter(event_type)
        out: list[Any] = []
        for e in obs.flow.items:
            payload = e.data if isinstance(e, Signal) else e
            out.extend(obs._match(flt, e, payload))
        return out

    # -- resource budget --------------------------------------------------------

    def budget_left(self) -> bool:
        """True while the run may still make agents (count + deadline)."""
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
        )
        if secure and tools:
            from pathlib import Path

            from lionagi.agent.hooks import guard_destructive, guard_paths

            spec.pre("bash", guard_destructive)
            workspace_root = str(Path(cwd) if cwd else Path.cwd())
            path_guard = guard_paths(allowed_paths=[workspace_root])
            spec.pre("reader", path_guard)
            spec.pre("editor", path_guard)
        # create_agent is the single grant site: emits is threaded through the
        # spec above, so capabilities are granted once during construction.
        branch = await create_agent(spec, load_settings=False)
        if name:
            branch.name = name
        self.session.include_branches(branch)
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
        """Operate, then re-prompt up to *retries* times while *arrived*() is
        false — the repair loop that keeps weak models in the pipeline.

        For API workers the repair turn names the expected emission keys so the
        model can fix fence/field mistakes.  For CLI workers (claude_code, codex)
        the failure mode is different — the entire turn arrives as prose with no
        fenced block — so the repair turn supplies a complete fenced-JSON example
        instead of just key hints.  CLI workers are detected via
        ``branch.chat_model.is_cli`` so no string-prefix matching is needed.
        """
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
            self.notify(
                "emission_missing",
                agent=getattr(branch, "name", "") or "",
                attempts=attempt + 1,
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
        """Cancel all in-flight spawned tasks and await completion."""
        if not self._active:
            return
        for t in list(self._active):
            t.cancel()
        await gather(*list(self._active), return_exceptions=True)
        # Callbacks remove tasks from _active as they settle.
        self._active.clear()

    async def _deadline_watchdog(self) -> None:
        """Sleep until the run deadline, then cancel all in-flight work.

        Cancels both the task wrapping ``_run()`` (``_run_task``) and any
        spawned background tasks (``_active``).  Cancelling ``_run_task``
        first propagates ``CancelledError`` into whatever ``_run`` is
        awaiting — including sequential ``branch.operate()`` calls inside
        ``operate_with_repair``, ``_plan``, ``_implement``, ``_fix_loop``,
        and ``_verify`` — so the deadline bounds the *entire* in-flight
        pipeline, not just background-spawned tasks.  Spawned tasks in
        ``_active`` are cleaned up by ``Engine.run()``'s finally block.

        The watchdog is scheduled by ``Engine.run()`` around ``_run()`` and
        is cancelled when ``_run()`` completes (or raises), so the watchdog
        never outlives the run.
        """
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
    """Stateless event-driven engine base; subclasses implement _run().

    Resource protection (per run):

    max_agents
        Hard cap on agents created — the primary recursion bound. When
        exhausted, reactions stop spawning (graceful: the run still
        synthesizes what it has) and ``make_agent`` raises
        :class:`EngineBudgetError`.
    deadline_s
        Optional wall-clock cap; expansion stops once passed.
    max_depth / dedup
        Semantic bounds — engines also gate on depth/cycle generation and
        normalized-topic dedup.

    Quality / direction control:

    judge_model + judge_role
        When ``judge_model`` is set, :meth:`judge` runs a cheap gate agent at
        expansion points: it sees the run's root objective and the candidate
        item, and passes or rejects it (off-topic, duplicative, trivial,
        unsafe). Fail-open with a ``judge_error`` notify — the hard budget
        remains the backstop.
    models
        Per-stage model overrides (``{"extract": "ollama/qwen3", "conclude":
        "claude_code/sonnet"}``) — route cheap models to volume stages and
        capable ones to judgement stages. Any agent process lionagi supports
        is a valid worker: API chat models, CLI agents (``claude_code/...``,
        ``codex/...``, ``pi/...``), or local ones.
    """

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
    ) -> None:
        self.model = model
        self.models = dict(models) if models else {}
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.max_agents = max_agents
        self.deadline_s = deadline_s
        self.judge_model = judge_model
        self.judge_role = judge_role

    def model_for(self, stage: str) -> str | None:
        return self.models.get(stage) or self.model

    async def judge(self, run: EngineRun, eid: str, subject: str) -> bool:
        """Quality gate before an expansion point. True = expand.

        No-op (True) when ``judge_model`` is unset. The judge sees the run's
        root objective (direction control) and emits a ``JudgeVerdict``; a
        weak judge that cannot emit may answer PASS/REJECT in text. Errors
        fail open with a ``judge_error`` notify — budget still bounds.
        """
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
        watchdog: asyncio.Task | None = None
        if run._deadline is not None:
            watchdog = asyncio.ensure_future(run._deadline_watchdog())
        # Wrap _run() in a task so the deadline watchdog can cancel it.
        # This is what makes the deadline bound *all* in-flight work — not just
        # spawned background tasks, but also sequential awaits inside _run().
        run_task = asyncio.ensure_future(self._run(run, *args, **kwargs))
        run._run_task = run_task
        try:
            return await run_task
        finally:
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
                    if drain.cancelled() or not drain.done():
                        raise  # external cancellation of run() itself
                except Exception:
                    logger.exception("engine active-task drain failed")

    async def _run(self, run: EngineRun, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("Engine subclass must implement _run(run, ...)")

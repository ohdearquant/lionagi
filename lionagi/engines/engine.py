# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Event-driven multi-agent engine: stateless Engine + per-run EngineRun."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
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


_ROLE_PROFILE_CACHE: dict[tuple[str, str], tuple[str | None, str | None]] = {}


def _profile_cache_key(role: str) -> tuple[str, str]:
    # Profile resolution is project-local (load_agent_profile searches from
    # Path.cwd() outward), so the cache key must include the resolved project
    # dir — a role-only key lets a long-lived process retain the first
    # project's routing after a cwd change.
    return (role, os.getcwd())


def role_profile_route(role: str) -> tuple[str | None, str | None]:
    """(model, effort) from the role's agent profile (``.lionagi/agents/<role>.md``),
    (None, None) when no profile exists. Configuring a role's profile routes every
    engine stage that uses the role; explicit engine/stage settings still win."""
    if not isinstance(role, str) or not role:
        return (None, None)
    key = _profile_cache_key(role)
    if key in _ROLE_PROFILE_CACHE:
        return _ROLE_PROFILE_CACHE[key]
    try:
        from lionagi.cli._providers import load_agent_profile  # noqa: PLC0415

        prof = load_agent_profile(role)
    except FileNotFoundError:
        # No profile configured for this role. Do not cache: a profile added
        # later (or a cwd change back to a project that has one) must be
        # picked up on the very next call, not masked by a stale (None, None).
        logger.debug("role_profile_route(%r): no agent profile found", role)
        return (None, None)
    except Exception as exc:
        # Malformed profile or a transient filesystem error — same
        # do-not-cache rule, distinguished only in the log line so a parse
        # failure isn't confused with "no profile configured".
        logger.warning("role_profile_route(%r): profile failed to parse: %s", role, exc)
        return (None, None)
    route = (prof.model, prof.effort)
    _ROLE_PROFILE_CACHE[key] = route
    return route


_ROLE_INJECTION_CACHE: dict[tuple[str, str], Any] = {}


def role_profile_injection(role: str) -> Any:
    """The role's agent-profile ``khive_injection`` opt-in, or None when the
    profile is absent or silent. Kept separate from :func:`role_profile_route`
    so model/effort routing and context-injection policy stay independently
    overridable."""
    if not isinstance(role, str) or not role:
        return None
    key = _profile_cache_key(role)
    if key in _ROLE_INJECTION_CACHE:
        return _ROLE_INJECTION_CACHE[key]
    try:
        from lionagi.cli._providers import load_agent_profile  # noqa: PLC0415

        value = getattr(load_agent_profile(role), "khive_injection", None)
    except FileNotFoundError:
        # Do not cache — see role_profile_route's identical rule.
        logger.debug("role_profile_injection(%r): no agent profile found", role)
        return None
    except Exception as exc:
        logger.warning("role_profile_injection(%r): profile failed to parse: %s", role, exc)
        return None
    _ROLE_INJECTION_CACHE[key] = value
    return value


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


def _emission_example(emits: tuple[type, ...]) -> str:
    """Complete fenced-JSON example for the given emission types."""
    import json as _json

    from lionagi.casts.emission import field_name_for

    obj: dict = {}
    for m in emits:
        key = field_name_for(m)
        obj[key] = _minimal_valid_json(m)
    return f"```json\n{_json.dumps(obj, indent=2)}\n```" if obj else ""


def _cli_emission_primer(schema_hint: str, emits: tuple[type, ...]) -> str:
    """Appended to a CLI worker's FIRST instruction: CLI workers default to prose,
    so the fenced-JSON contract and a complete example go up front rather than
    only in the repair re-prompt."""
    example = _emission_example(emits)
    if not example:
        return ""
    return (
        "\n\n# Output contract\n"
        "Your final reply MUST contain a fenced ```json block with the emission "
        f"object. {schema_hint}\n\nExample structure:\n{example}"
    )


def _cli_repair_instruction(schema_hint: str, emits: tuple[type, ...]) -> str:
    """Repair instruction for CLI workers; supplies a complete fenced-JSON example because CLI workers emit prose, not a malformed schema."""
    example = _emission_example(emits)
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


def _namespaced_injection(injection: Any, namespace: str) -> Any:
    """Stamp a run-derived namespace onto a khive_injection config unless the
    caller already pinned one — closes the cross-run/cross-project memory
    exposure a default (or profile-level) opt-in would otherwise have."""
    if injection is True:
        return {"namespace": namespace}
    if isinstance(injection, dict):
        if injection.get("namespace"):
            return injection
        return {**injection, "namespace": namespace}

    from lionagi.tools.khive_injection import KhiveInjectionPolicy  # noqa: PLC0415

    if isinstance(injection, KhiveInjectionPolicy):
        if injection.namespace:
            return injection
        import dataclasses

        return dataclasses.replace(injection, namespace=namespace)
    return injection


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
        # Unique per run: stamped into every stage agent's khive-injection
        # namespace so recall/writeback never crosses runs or projects.
        self.run_id: str = uuid.uuid4().hex[:12]
        self.agents_made: int = 0
        self._sem = Semaphore(engine.max_concurrent)
        self._active: set[asyncio.Task] = set()
        self._pending: deque = deque()
        self._seen: set[str] = set()
        self._t0 = monotonic()
        self._deadline = None if engine.deadline_s is None else self._t0 + engine.deadline_s
        self._budget_notified = False
        # Wraps the engine's _run() coroutine; the deadline watchdog cancels this
        # to interrupt in-flight sequential work promptly, not just spawned tasks.
        self._run_task: asyncio.Task | None = None
        # Emission-missing diagnostics for the CLI's engine_runs.error column,
        # e.g. "<agent> x<attempts>", even when overall status is "completed".
        self._emission_failures: list[str] = []
        # Terminal sub-agent failures (e.g. missing API key), so a run where
        # every agent errored can be surfaced as failed instead of green.
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
        effort: str | None = None,
        tools: tuple[str, ...] = (),
        emits: tuple[type, ...] = (),
        permissions: Any = None,
        cwd: str | None = None,
        secure: bool = True,
        exempt: bool = False,
        mcp_servers: list[str] | None = None,
        extra_prompt: str | None = None,
        khive_injection: Any = None,
    ) -> Branch:
        # exempt = terminal stages (synthesis/verdict) that must run even when
        # the expansion budget is gone — degrade, don't lose the run.
        if not exempt and not self.budget_left():
            self._notify_budget_once("make_agent")
            raise EngineBudgetError(
                f"agent budget exhausted ({self.agents_made}/{self.engine.max_agents})"
            )
        self.agents_made += 1
        if cwd is None:
            cwd = self.engine.agent_cwd
        if extra_prompt is None:
            extra_prompt = self.engine.agent_extra_prompt
        # Resolution order: explicit call > engine-wide > the role's agent
        # profile. An effort baked into the model spec's suffix outranks the
        # profile default too — only apply prof_effort when the resolved
        # model has no suffix of its own, so a profile can't silently
        # override an explicit `codex/gpt-5.6-luna-high`-style effort.
        prof_model, prof_effort = role_profile_route(role)
        resolved_model = model or self.engine.model or prof_model
        resolved_effort = effort or self.engine.effort
        if not resolved_effort and resolved_model:
            from lionagi.service.providers import parse_model_spec  # noqa: PLC0415

            if not parse_model_spec(resolved_model).effort:
                resolved_effort = prof_effort
        # Same precedence for khive injection; an explicit False at any level
        # disables and stops the profile fallback.
        injection = khive_injection
        if injection is None:
            injection = self.engine.khive_injection
        if injection is None:
            injection = role_profile_injection(role)
        if injection is not None and injection is not False:
            namespace = f"{type(self.engine).__name__.lower()}:{self.run_id}"
            injection = _namespaced_injection(injection, namespace)
        spec = AgentSpec.compose(
            role,
            modes=modes,
            model=resolved_model,
            effort=resolved_effort,
            tools=tuple(tools),
            permissions=permissions,
            emits=tuple(emits) if emits else None,
            cwd=cwd,
            system_prompt=extra_prompt,
            khive_injection=injection,
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
        actions: bool = False,
    ) -> Any:
        """Operate then re-prompt up to *retries* times while *arrived*() is false; CLI workers get a full fenced-JSON example, API workers get key hints.

        ``actions`` must be True when the branch was given tools (e.g. bash)
        that the stage instruction requires it to actually call — CLI
        providers execute their own tools regardless, but a non-CLI (API)
        model only gets lionagi's registered tool schemas, and therefore only
        invokes them, when ``branch.operate(actions=True)``.
        """
        # CLI workers emit prose, not fenced JSON — they need the full example form.
        is_cli = bool(getattr(getattr(branch, "chat_model", None), "is_cli", False))
        hint = emission_keys(emits)
        if is_cli:
            # Front-load the contract: waiting for the repair pass costs a whole
            # extra CLI process per worker that defaults to prose.
            instruction = f"{instruction}{_cli_emission_primer(hint, emits)}"
        # actions=False is branch.operate()'s own default: omit the kwarg
        # entirely in that (common) case rather than passing it explicitly,
        # so a minimal test double's operate(self, *, instruction) still works.
        operate_kwargs: dict[str, Any] = {"actions": True} if actions else {}
        res = await branch.operate(instruction=instruction, **operate_kwargs)
        attempt = 0
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
            res = await branch.operate(instruction=repair_msg, **operate_kwargs)
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
        """Cancel and await all in-flight spawned tasks; tasks that don't settle
        within ``engine.cancel_timeout_s`` are abandoned with a logged warning."""
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
        """Sleep until the deadline, then cancel _run_task."""
        if self._deadline is None:
            return
        delay = self._deadline - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._notify_budget_once("deadline_watchdog")
        # Cancel _run_task to interrupt the in-flight sequential stage promptly;
        # spawned tasks (_active) are drained separately in Engine.run()'s finally block.
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    async def wait_quiescence(self) -> None:
        """Block until all spawned tasks settle; re-raise non-cancellation, non-budget
        failures. EngineBudgetError is benign (discretionary work declined) and swallowed."""
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
        on_branch_created: Any = None,
        spawn_branch_setup: Any = None,
        on_op_complete: Any = None,
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
                on_branch_created=on_branch_created,
                spawn_branch_setup=spawn_branch_setup,
                on_op_complete=on_op_complete,
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
    """Return type of Engine.run(): a str (back-compat) carrying the run's structured
    outcome. ``.run`` is a live handle — don't retain it, it keeps the Session alive."""

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
        effort: str | None = None,
        efforts: dict[str, str] | None = None,
        max_depth: int = 3,
        max_concurrent: int = 5,
        max_agents: int = 50,
        deadline_s: float | None = None,
        judge_model: str | None = None,
        judge_role: str = "critic",
        cancel_timeout_s: float = 30.0,
        agent_cwd: str | None = None,
        agent_extra_prompt: str | None = None,
        khive_injection: Any = None,
    ) -> None:
        # Run-wide agent defaults: pin every agent to a working directory (e.g. a
        # provisioned worktree) and/or a shared standards prompt; per-call
        # make_agent(cwd=..., extra_prompt=...) still wins.
        self.agent_cwd = agent_cwd
        self.agent_extra_prompt = agent_extra_prompt
        # Run-wide khive context-injection default for every stage agent
        # (True/mapping/policy enable, False disables even a profile opt-in,
        # None defers to each stage role's agent profile).
        self.khive_injection = khive_injection
        self.model = model
        self.models = dict(models) if models else {}
        self.effort = effort
        self.efforts = dict(efforts) if efforts else {}
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.max_agents = max_agents
        self.deadline_s = deadline_s
        self.judge_model = judge_model
        self.judge_role = judge_role
        self.cancel_timeout_s = cancel_timeout_s

    def model_for(self, stage: str) -> str | None:
        return self.models.get(stage) or self.model

    def effort_for(self, stage: str) -> str | None:
        return self.efforts.get(stage) or self.effort

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
                    # Judge legs are cheap yes/no gates that fire per item;
                    # a recall round-trip per verdict is pure overhead.
                    khive_injection=False,
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
        """Cancel in-flight spawned tasks, then run _partial_export shielded + timeout-
        bounded. Returns _UNSET on failure/timeout; an external cancel still propagates."""
        # Cancel background tasks so synthesis sees a stable snapshot and no
        # work burns tokens past budget exhaustion (no-op if _active is empty).
        if run._active:
            await run.cancel_active()
        # asyncio.shield keeps the export running outside the cancelled scope;
        # the hard timeout bounds it so a hung synthesis call can't extend the run.
        partial_coro = self._partial_export(run, *args, **kwargs)
        partial_task = asyncio.ensure_future(partial_coro)
        try:
            return await asyncio.wait_for(
                asyncio.shield(partial_task),
                timeout=_PARTIAL_EXPORT_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            # wait_for turns its own timeout into TimeoutError, so CancelledError
            # here is always an external cancel — clean up and re-raise.
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
        skipped = list(run._emission_failures)
        # A dimension/agent whose emission never arrived is a skipped part of
        # the result regardless of whether a deadline/budget also fired — a
        # result blind to a skipped agent is degraded by construction, so
        # this must never depend solely on degrade_reason being set.
        if not degrade_reason and skipped:
            degrade_reason = "emission_failure: " + "; ".join(skipped)
        return EngineResult(
            result,
            events_by_type=run.by_type,
            skipped=skipped,
            degraded=bool(degrade_reason) or bool(skipped),
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
        # Reset so a reused engine never carries diagnostics from a prior run.
        self._emission_failures: list[str] = []
        self._agent_errors: list[str] = []
        self._total_agent_failure: bool = False
        watchdog: asyncio.Task | None = None
        if run._deadline is not None:
            watchdog = asyncio.ensure_future(run._deadline_watchdog())
        # Wrapping _run() in a task lets the deadline bound *all* in-flight work —
        # sequential awaits inside _run(), not just spawned background tasks.
        run_task = asyncio.ensure_future(self._run(run, *args, **kwargs))
        run._run_task = run_task
        result: Any = _UNSET
        partial_result: Any = _UNSET
        degrade_reason: str = ""
        try:
            result = await run_task
            # A "successful" run may still have silently dropped a discretionary
            # budget-capped subtree — flag it so a clean-looking result doesn't hide it.
            if run._budget_notified:
                degrade_reason = "budget"
        except asyncio.CancelledError:
            # Distinguish internal (watchdog) vs external (caller) cancellation via
            # task.cancelling() (3.11+; 3.10 falls back to _budget_notified alone).
            current = asyncio.current_task()
            caller_cancelled = (
                current is not None and hasattr(current, "cancelling") and current.cancelling() > 0
            )
            if run._budget_notified and not caller_cancelled:
                # The watchdog is the only thing that cancels run_task, so
                # internal-only cancellation here means a deadline hit.
                degrade_reason = "deadline"
                partial_result = await self._degrade_export(run, args, kwargs)
            else:
                # External cancellation — surface it after cleanup (below).
                raise
        except (EngineBudgetError, ExceptionGroup) as exc:
            # A root-level make_agent() budget hit routes to partial-export; a
            # non-budget leaf anywhere in the group must not be laundered into one.
            if not _is_all_budget_error(exc):
                raise
            degrade_reason = "budget"
            partial_result = await self._degrade_export(run, args, kwargs)
        finally:
            # Copy diagnostics onto the engine instance (fresh list) so the CLI's
            # getattr(engine, "_emission_failures", []) sees them on every exit path.
            self._emission_failures = list(run._emission_failures)
            # Flag total failure only when every agent made for this run
            # terminally errored, so partial/soft-empty runs aren't over-flagged.
            self._agent_errors = list(run._agent_errors)
            self._total_agent_failure = (
                run.agents_made > 0 and len(run._agent_errors) >= run.agents_made
            )
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watchdog
            # Drain tasks still in _active regardless of how run() exited; shielded so
            # cleanup finishes even under a caller cancel (which still re-raises below).
            if run._active:
                drain = asyncio.ensure_future(run.cancel_active())
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    if drain.cancelled():
                        raise
                    # External cancel: keep waiting the shielded drain (absorbing
                    # repeat cancels) so no run-owned task outlives run().
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

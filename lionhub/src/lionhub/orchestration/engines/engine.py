# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Event-driven multi-agent engine: stateless Engine + per-run EngineRun."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from time import monotonic
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from lionagi._errors import CLIInputRefusedError, ConfigurationError, InputTokenCapError
from lionagi._provider_errors import NestedAgentRefusedError
from lionagi.agent import AgentSpec, create_agent
from lionagi.agent.factory import _AgentSettingError
from lionagi.ln import short_id
from lionagi.ln.concurrency import Semaphore, gather, move_on_after, sleep
from lionagi.ln.concurrency._compat import (
    ExceptionGroup,
    get_exception_group_exceptions,
    is_exception_group,
)
from lionagi.ln.types import RoleFilter, TypeFilter
from lionagi.protocols._concepts import Observable
from lionagi.session.observer import SessionObserver
from lionagi.session.session import Session
from lionagi.session.signal import Signal

if TYPE_CHECKING:
    from lionagi.protocols.generic.pile import Pile
    from lionagi.session.branch import Branch
    from lionhub.providers._cli_subprocess import SpawnedProcess

logger = logging.getLogger("lionagi.engines")
EventCallback = Callable[[dict[str, Any]], Any]

_JUDGE_REFUSALS = (
    CLIInputRefusedError,
    InputTokenCapError,
    NestedAgentRefusedError,
    _AgentSettingError,
)

# Sentinel used by Engine.run() to distinguish "partial export produced a
# result" from "partial export returned None intentionally".
_UNSET: Any = object()

# Maximum wall-clock seconds allowed for the post-cancellation partial export.
# Kept short so a hung synthesis LLM call cannot extend the run unboundedly.
_PARTIAL_EXPORT_TIMEOUT_S: float = 120.0

# Engine cancellation is a backstop for a worker that refuses to unwind its
# provider generator. Ordinary provider teardown retains its longer graceful
# window; once the owning task has ignored cancellation, the engine gives the
# run-owned process group one short bounded TERM window before KILL.
_RUN_PROCESS_TERM_GRACE_S: float = 1.0

#: How many times ``cancel_active`` rescans for processes spawned during its own
#: teardown. Small on purpose: each pass costs a group scan, and needing more
#: than a couple means a task is spawning during cancellation, which is worth a
#: warning rather than an unbounded retry.
_CANCEL_RESCAN_PASSES: int = 3


@dataclass(frozen=True, slots=True)
class CancellationRecord:
    """One bounded ``cancel_active`` pass, with task/process facts separated."""

    tasks_cancelled: int
    tasks_abandoned: int
    processes_reaped: int
    # A reap that raised leaves the child running, which is the condition this
    # cleanup exists to eliminate. Counting it separately is what lets a reader
    # tell "nothing needed reaping" from "every reap failed" -- both of which
    # leave processes_reaped at 0.
    processes_abandoned: int = 0


@dataclass(slots=True)
class _TrackedCLIProcess:
    """Runtime-only run ownership for one CLI process."""

    process: Any
    spawned: SpawnedProcess
    # Set once end_child_group has returned for this entry. That call is the
    # authority on the group being finished, so it -- not the leader's exit --
    # is what retires the entry. Without it a swept group whose members are
    # still visible to a scan would be re-reaped on every pass.
    reaped: bool = False


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


# Cap on the reply text an emission event carries when the engine records it
# (``Engine(emission_reply_text=True)``). A reply that did not emit is usually
# short prose or a malformed fence; the cap bounds the rare runaway one and the
# event says it truncated (``text_truncated`` + ``text_chars``).
RAW_RESPONSE_TEXT_CAP = 16_000


def _read_anthropic_messages(body: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages body: ``stop_reason`` and a ``content`` list of typed blocks. Also the reading for a body of unknown shape."""
    blocks = body.get("content")
    return {
        "stop_reason": body.get("stop_reason"),
        "block_types": (
            [b.get("type") for b in blocks if isinstance(b, dict)]
            if isinstance(blocks, list)
            else None
        ),
    }


def _read_chat_completion(body: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat-completions body: the reason lives on ``choices[0].finish_reason``; there are no typed blocks."""
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    return {
        "stop_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
        "block_types": None,
    }


def _read_openai_responses(body: dict[str, Any]) -> dict[str, Any]:
    """OpenAI Responses body: ``status`` (``incomplete_details.reason`` when the reply was cut short) and an ``output`` list of typed items."""
    details = body.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    items = body.get("output")
    return {
        "stop_reason": reason or body.get("status"),
        "block_types": (
            [i.get("type") for i in items if isinstance(i, dict)]
            if isinstance(items, list)
            else None
        ),
    }


def _read_reply_shape(body: dict[str, Any]) -> dict[str, Any]:
    """The stop reason and block types of a provider reply body, by the shape of the body.

    A new provider shape adds a reader above and a branch here; ``usage``, ``model``
    and ``id`` are top-level keys in all three shapes and are read by the caller.
    """
    if body.get("object") == "response" or (
        isinstance(body.get("output"), list) and "status" in body
    ):
        return _read_openai_responses(body)
    if body.get("stop_reason") is None and isinstance(body.get("choices"), list):
        return _read_chat_completion(body)
    return _read_anthropic_messages(body)


def _configured_max_tokens(chat_model: Any) -> int | None:
    endpoint = getattr(chat_model, "endpoint", None)
    config = getattr(endpoint, "config", None)
    kwargs = getattr(config, "kwargs", None)
    value = kwargs.get("max_tokens") if isinstance(kwargs, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _raw_leg_response(branch: Any, *, include_text: bool = False) -> dict[str, Any] | None:
    """What a leg's last reply held, for an emission event: stop reason, usage, model, block types, and the reply text when asked.

    Without it an ``emission_missing`` records only that nothing parsed, never why
    (truncated at ``max_tokens``, thinking blocks and no text, a fence that is not
    JSON). Best effort by contract: the diagnostics must never kill the run they
    describe, and a branch with no recorded reply (a test double, a leg that died
    before replying) yields None, so the event simply carries no ``raw_response``.

    ``max_tokens_configured`` is the output-token limit the leg's model was built with
    (``AgentSpec.max_tokens``), None when none was set.

    The reply text can quote the diff or context the leg was given, and an event
    reaches every ``on_event`` sink, so ``text`` is present only when
    ``include_text`` is set (``Engine(emission_reply_text=True)``). ``text_chars``
    is always present; ``text`` and ``text_truncated`` appear together.

    ``http_status`` is 200 for an API leg and None for a CLI worker. A call the
    provider answered with anything but 200 raises before a message exists
    (service/connections/endpoint.py, both the plain and the streaming path), so a
    recorded API reply was by construction a 200.
    """
    try:
        last = branch.msgs.last_response
        if last is None:
            return None
        text = last.response or ""
        raw = last.model_response
    except Exception:
        return None
    first = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(first, dict):
        first = {}
    chat_model = getattr(branch, "chat_model", None)
    is_cli = bool(getattr(chat_model, "is_cli", False))
    out: dict[str, Any] = {"text_chars": len(text)}
    if include_text:
        out["text"] = text[:RAW_RESPONSE_TEXT_CAP]
        out["text_truncated"] = len(text) > RAW_RESPONSE_TEXT_CAP
    out.update(_read_reply_shape(first))
    out.update(
        {
            "usage": first.get("usage"),
            "model": first.get("model"),
            "response_id": first.get("id"),
            "http_status": None if is_cli else 200,
            # The limit the leg's model was built with; None names none, so the provider's
            # default applied. A reply that stopped at max_tokens is read against this.
            "max_tokens_configured": _configured_max_tokens(chat_model),
        }
    )
    return out


_ROLE_PROFILE_CACHE: dict[tuple[str, str], tuple[str | None, str | None]] = {}


def _profile_cache_key(role: str) -> tuple[str, str]:
    # Profile resolution is project-local; a role-only key would retain the
    # first project's routing after a cwd change in a long-lived process.
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
        # Not cached: a profile added later must be picked up on the next call.
        logger.debug("role_profile_route(%r): no agent profile found", role)
        return (None, None)
    except Exception as exc:
        # Same do-not-cache rule; distinguished only in the log line.
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
    if not names:
        return ""
    return (
        f"Expected top-level key(s): {names}. Each key holds one object; to report "
        "several, emit one object per finding, or a list of objects under the key."
    )


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


@dataclass(slots=True)
class _EmissionOrigin:
    owner: object | None
    branch: Any
    event: Any
    task: Any
    active: bool = True
    collections: dict[Any, list[tuple[Any, int]]] = dataclass_field(default_factory=dict)


class _EngineDispatch:
    """Keep branch provenance at the actual observer entry, never in payloads."""

    def __init__(self, emit: Callable) -> None:
        self._emit = emit
        self.current: ContextVar[_EmissionOrigin | None] = ContextVar(
            "engine_emission_origin", default=None
        )

    async def __call__(self, event: Any) -> list[Any]:
        # Session emissions, including nested replays of the same envelope,
        # cannot borrow the branch whose handlers or gate invoked them.
        return await self._dispatch(event, None, None)

    async def _from_branch(self, event: Any, owner: object, branch: Any) -> list[Any]:
        return await self._dispatch(event, owner, branch)

    async def _dispatch(self, event: Any, owner: object | None, branch: Any) -> list[Any]:
        if not isinstance(event, Observable):
            event = Signal(data=event)
        origin = _EmissionOrigin(owner, branch, event, asyncio.current_task())
        token = self.current.set(origin)
        try:
            return await self._emit(event)
        finally:
            origin.active = False
            self.current.reset(token)


def _engine_dispatch(observer: Any) -> _EngineDispatch | None:
    # Unknown observers keep their existing protocol and carry no authority.
    if not isinstance(observer, SessionObserver):
        return None
    if not isinstance(observer.emit, _EngineDispatch):
        observer.emit = _EngineDispatch(observer.emit)
    return observer.emit


class _BranchObserver:
    def __init__(self, observer: SessionObserver, owner: object, branch: Any) -> None:
        self.observer, self.owner, self.branch = observer, owner, branch

    def __getattr__(self, name: str) -> Any:
        return getattr(self.observer, name)

    async def emit(self, event: Any) -> list[Any]:
        dispatch = _engine_dispatch(self.observer)
        return await dispatch._from_branch(event, self.owner, self.branch)


class _BranchEmitter:
    def __init__(self, emit: Callable, owner: object, branch: Any) -> None:
        self.emit, self.owner, self.branch = emit, owner, branch

    async def __call__(self, event: Any) -> list[Any]:
        observer = getattr(self.branch, "_observer", None)
        if isinstance(observer, _BranchObserver):
            observer = observer.observer
        if isinstance(observer, SessionObserver):
            # include_branches restores the observer, so bind at each emission.
            self.branch._observer = _BranchObserver(observer, self.owner, self.branch)
        return await self.emit(event)


@dataclass(frozen=True, slots=True)
class _AgentEmission:
    value: Any
    branch: Any
    position: int | None


class _AgentEventFilter(RoleFilter):
    """Use the existing typed traversal, with the post-gate envelope intact."""

    def __init__(self, run: Any, event_type: type, capture: Callable | None = None) -> None:
        self.run, self.event_type, self.capture = run, event_type, capture

    def matches(self, event: Any) -> list[Any]:
        observer = self.run.session.observer
        payload = event.data if isinstance(event, Signal) else event
        origin = self.run._emission_origin(event)
        branch = (
            origin.branch
            if origin is not None and origin.owner is self.run._emission_owner
            else None
        )
        collected = iter(
            origin.collections.get(self.run._emission_owner, ()) if origin is not None else ()
        )
        receipts = []
        for value in observer._match(TypeFilter(self.event_type), event, payload):
            position = next((i for stored, i in collected if stored is value), None)
            receipts.append(_AgentEmission(value, branch, position))
        if self.capture is not None:
            for receipt in receipts:
                self.capture(receipt)
            # Capturing a judge decision must not add a handler result.
            return []
        return receipts

    def __repr__(self) -> str:
        return f"_AgentEventFilter({self.event_type.__name__})"


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
        self._emission_owner = object()
        _engine_dispatch(self.session.observer)
        self.on_event = on_event
        self._on_branch_created = on_branch_created
        self.root: str = ""
        # Unique per run: stamped into every stage agent's khive-injection
        # namespace so recall/writeback never crosses runs or projects.
        self.run_id: str = short_id(12)
        self.agents_made: int = 0
        # Each branch make_agent built while either setting was on: the engine's
        # agent_env and containment as make_agent read them before building it, the
        # branch, and the chat and parse models it was built with. With either set,
        # run_dag runs a node only on such a branch, built under the settings now in
        # force, still the one the session holds under its id, and still on those
        # models.
        self._agent_branches: dict[
            str, tuple[tuple[dict[str, str] | None, bool], Any, Any, Any]
        ] = {}
        self._sem = Semaphore(engine.max_concurrent)
        self._active: set[asyncio.Task] = set()
        # Populated at the shared CLI spawn boundary through a run-scoped
        # context observer. The live Process handle is intentionally never
        # serialized; SpawnedProcess carries the verified identity used by the
        # existing witness-gated group teardown.
        self._cli_processes: dict[int, _TrackedCLIProcess] = {}
        # Set once the run has drained, so a spawn arriving afterwards is ended
        # rather than filed against a run that will never sweep again.
        self._cli_tracking_closed: bool = False
        self.cancellation_records: list[CancellationRecord] = []
        # Failures of spawned tasks, recorded as each one settles. The drain
        # cannot be the only collector: a task that finishes before the drain
        # starts is already out of _active by then, and its failure would have
        # nowhere to be seen.
        self._settled_errors: list[BaseException] = []
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
        # One row per emission that only arrived on the fallback leg:
        # {"agent", "model", "attempt", "after_attempts"}. These dimensions are NOT in
        # _emission_failures (the fallback produced their output), so a reader that
        # wants to say which leg produced an emission reads this list.
        self.emission_fallbacks: list[dict[str, Any]] = []
        # Terminal sub-agent failures (e.g. missing API key), so a run where
        # every agent errored can be surfaced as failed instead of green.
        self._agent_errors: list[str] = []

    @property
    def events(self) -> Pile:
        return self.session.observer.flow.items

    def by_type(self, event_type: type) -> list[Any]:
        """Stored payloads matching *event_type*, unwrapped; reads ``self.events`` so a run class can scope what counts as its own."""
        obs = self.session.observer
        flt = TypeFilter(event_type)
        out: list[Any] = []
        for e in self.events:
            payload = e.data if isinstance(e, Signal) else e
            out.extend(obs._match(flt, e, payload))
        return out

    def _emission_origin(self, event: Any) -> _EmissionOrigin | None:
        dispatch = self.session.observer.emit
        if not isinstance(dispatch, _EngineDispatch):
            return None
        origin = dispatch.current.get()
        if (
            origin is not None
            and origin.active
            and origin.event is event
            and origin.task is asyncio.current_task()
        ):
            return origin
        return None

    def _agent_events(self, event_type: type) -> _AgentEventFilter:
        return _AgentEventFilter(self, event_type)

    def _bind_agent_emitter(self, branch: Any) -> None:
        emit = getattr(branch, "emit", None)
        if emit is None:
            return
        if isinstance(emit, _BranchEmitter):
            if emit.owner is self._emission_owner:
                return
            emit = emit.emit
        object.__setattr__(branch, "emit", _BranchEmitter(emit, self._emission_owner, branch))

    @contextlib.contextmanager
    def _agent_verdicts(self, branch: Any, event_type: type):
        verdicts: list[Any] = []

        def capture(receipt: _AgentEmission) -> None:
            if receipt.branch is branch:
                verdicts.append(receipt.value.model_copy(deep=True))

        def unused(_event: Any, _ctx: Any) -> None:
            pass

        observer = self.session.observer
        observer.observe(_AgentEventFilter(self, event_type, capture), handler=unused)
        try:
            yield verdicts
        finally:
            observer.unobserve(unused)

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
            self.on_event({"type": kind, "engine_instance_id": self.run_id, **data})

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
        max_tokens: int | None = None,
        tools: tuple[str, ...] = (),
        emits: tuple[type, ...] = (),
        permissions: Any = None,
        cwd: str | None = None,
        secure: bool = True,
        exempt: bool = False,
        mcp_servers: list[str] | None = None,
        mcp_config_path: str | None = None,
        extra_prompt: str | None = None,
        env: Mapping[str, str] | None = None,
        khive_injection: Any = None,
        command_env_allowlist: Sequence[str] = (),
        _command_env_names_out: Callable[[frozenset[str]], None] | None = None,
        _effort_origin: str | None = None,
        _model_origin: str | None = None,
    ) -> Branch:
        # exempt = terminal stages (synthesis/verdict) that must run even when
        # the expansion budget is gone — degrade, don't lose the run.
        if not exempt and not self.budget_left():
            self._notify_budget_once("make_agent")
            raise EngineBudgetError(
                f"agent budget exhausted ({self.agents_made}/{self.engine.max_agents})"
            )
        self.agents_made += 1
        # One reading of the engine's agent settings, taken before any await: the
        # build below and the record run_dag checks both use it, so a setting
        # changed while create_agent awaits cannot be recorded as the build's.
        built_under = (
            dict(self.engine.agent_env) if self.engine.agent_env else None,
            bool(self.engine.contain_account_integrations),
        )
        if cwd is None:
            cwd = self.engine.agent_cwd
        if extra_prompt is None:
            extra_prompt = self.engine.agent_extra_prompt
        if mcp_config_path is None:
            mcp_config_path = self.engine.agent_mcp_config_path
        if env is None:
            env = built_under[0]
        # Resolution order: explicit call > engine-wide > role profile. An
        # effort suffix baked into the model spec outranks prof_effort too.
        prof_model, prof_effort = role_profile_route(role)
        resolved_model = model or self.engine.model or prof_model
        model_origin = (
            (_model_origin or "explicit")
            if model
            else "explicit"
            if self.engine.model
            else "profile"
        )
        resolved_effort = effort or self.engine.effort
        effort_origin = _effort_origin if effort and _effort_origin else "explicit"
        resolved_max_tokens = max_tokens if max_tokens is not None else self.engine.max_tokens
        if not resolved_effort and resolved_model:
            from lionagi.agent.factory import _parse_agent_model_spec  # noqa: PLC0415

            if not _parse_agent_model_spec(resolved_model, origin=model_origin).effort:
                resolved_effort = prof_effort
                effort_origin = "profile"
        # Same precedence for khive injection; explicit False stops the profile fallback.
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
            # Read off the engine rather than defaulted here: create_agent is
            # the single grant site, and a grant this path could not express
            # was the whole defect -- a headless CLI cannot prompt for a tool
            # permission, so a leg spawned without this is denied every tool
            # call and reports success with nothing in it.
            yolo=self.engine.yolo,
            contain_account_integrations=built_under[1],
            prompt_caching=bool(self.engine.prompt_caching),
            max_tokens=resolved_max_tokens,
            command_env_allowlist=command_env_allowlist,
        )
        spec._effort_origin = effort_origin
        spec._model_origin = model_origin
        if mcp_servers is not None:
            spec.mcp_servers = mcp_servers
        if mcp_config_path is not None:
            spec.mcp_config_path = mcp_config_path
        if secure and tools:
            from lionagi.agent.spec import _wire_secure_guards

            _wire_secure_guards(spec, cwd)
        # create_agent is the single grant site; capabilities are granted once here.
        # AgentSpec has no env field, so the overlay goes to create_agent, which
        # hands it to the CLI endpoint it builds: the carrier that reaches the child.
        branch = await create_agent(
            spec,
            load_settings=False,
            agent_env=env,
            _command_env_names_out=_command_env_names_out,
        )
        if built_under[0] is not None or built_under[1]:
            self._agent_branches[str(branch.id)] = (
                built_under,
                branch,
                branch.chat_model,
                branch.parse_model,
            )
        if name:
            branch.name = name
        self.session.include_branches(branch)
        if self._on_branch_created is not None:
            self._on_branch_created(branch)
        self._bind_agent_emitter(branch)
        return branch

    # -- emission diagnostics ---------------------------------------------------

    @property
    def emission_failures(self) -> list[str]:
        """Markers of the agents and stages whose emission never arrived, in the order they were recorded (a copy)."""
        return list(self._emission_failures)

    def record_emission_failure(self, marker: str) -> None:
        """Record *marker* once; recording the same marker again is a no-op."""
        if marker not in self._emission_failures:
            self._emission_failures.append(marker)

    def _notify_emission(
        self, kind: str, *, agent: str, leg: Any | None = None, **fields: Any
    ) -> None:
        """Send one emission event; the single place their payloads are assembled.

        ``leg`` is the branch whose newest reply explains the event. Its
        ``raw_response`` is built only while an ``on_event`` observer is attached,
        since nothing else reads it, and carries the reply text only when the
        engine records it (``Engine(emission_reply_text=True)``).

        Numbering, one name per meaning: ``emission_repair.repair_index`` counts the
        repair prompts sent to the leg, so the leg's operate call number is
        ``repair_index + 1``; ``emission_recovered.operate_call`` is the operate call
        number within the fallback leg that produced the emission;
        ``emission_missing.attempts`` counts the operate calls across both legs.
        ``attempt`` is kept on the first two with the same value as the name that
        replaced it, for readers written against it.
        """
        if leg is not None and self.on_event:
            raw = _raw_leg_response(leg, include_text=self.engine.emission_reply_text)
            if raw is not None:
                fields["raw_response"] = raw
        self.notify(kind, agent=agent, **fields)

    async def _attempt_emission(
        self,
        branch: Branch,
        instruction: str,
        *,
        arrived: Callable[[], bool],
        emits: tuple[type, ...],
        retries: int,
        actions: bool,
        calls: list[int] | None = None,
    ) -> tuple[Any, int]:
        """One leg's turn: operate, then re-prompt up to *retries* times while *arrived*() is false. Returns (last reply, repair attempts made).

        ``calls`` is a one-slot counter bumped before every operate call, so a caller
        that cancels this turn still knows how many calls were started.
        """
        # CLI workers emit prose, not fenced JSON — front-load the full example
        # form; waiting for the repair pass costs a whole extra CLI process.
        is_cli = bool(getattr(getattr(branch, "chat_model", None), "is_cli", False))
        hint = emission_keys(emits)
        if is_cli:
            instruction = f"{instruction}{_cli_emission_primer(hint, emits)}"
        # Omit actions entirely when False so a minimal test double still works.
        operate_kwargs: dict[str, Any] = {"actions": True} if actions else {}
        if calls is not None:
            calls[0] += 1
        res = await branch.operate(instruction=instruction, **operate_kwargs)
        attempt = 0
        while not arrived() and attempt < retries:
            agent_name = getattr(branch, "name", "") or ""
            budget = getattr(branch, "token_budget", None)
            if getattr(budget, "is_critical", False):
                self._notify_emission(
                    "emission_repair_skipped",
                    agent=agent_name,
                    reason="context_critical",
                    used=getattr(budget, "used", None),
                    limit=getattr(budget, "limit", None),
                )
                break
            attempt += 1
            # The leg's newest reply is the one that just failed to emit; it is
            # recorded here, before the repair prompt replaces it.
            self._notify_emission(
                "emission_repair",
                agent=agent_name,
                leg=branch,
                attempt=attempt,
                repair_index=attempt,
                cli_worker=is_cli,
            )
            if is_cli:
                repair_msg = _cli_repair_instruction(hint, emits)
            else:
                repair_msg = _repair_instruction(hint)
            if calls is not None:
                calls[0] += 1
            res = await branch.operate(instruction=repair_msg, **operate_kwargs)
        return res, attempt

    async def _run_fallback(
        self,
        fallback: Callable[[], Awaitable[Branch]],
        fallback_model: str | None,
        *,
        agent: str,
        first_leg: Branch,
        first_reply: Any,
        instruction: str,
        arrived: Callable[[], bool],
        emits: tuple[type, ...],
        retries: int,
        actions: bool,
        after_attempts: int,
    ) -> tuple[Any, Branch, int, bool] | None:
        """One fresh fallback leg's turn. None when no leg ran (the factory raised ``EngineBudgetError``); else ``(reply, leg, operate calls made, recovered)``.

        When the leg emits, the recovery is recorded here (``emission_fallbacks`` and
        an ``emission_recovered`` event). With ``Engine.fallback_deadline_s`` set, a
        leg still running at that limit is abandoned (``emission_fallback_timeout``)
        and its operate calls are counted as started, a call cut short included.
        If the emission had already arrived by then it is recovered all the same (the
        leg is returned, the reply stays the first leg's, since the abandoned call
        returned none); otherwise the reply and leg stay the first leg's and the
        emission is not recovered.
        """
        try:
            leg = await fallback()
        except EngineBudgetError:
            self._notify_emission(
                "emission_fallback_skipped",
                agent=agent,
                reason="agent_budget",
                model=fallback_model,
            )
            return None
        self._notify_emission(
            "emission_fallback", agent=agent, model=fallback_model, after_attempts=after_attempts
        )
        limit = self.engine.fallback_deadline_s
        made = [0]
        reply = first_reply
        with move_on_after(limit) as scope:
            reply, _ = await self._attempt_emission(
                leg,
                instruction,
                arrived=arrived,
                emits=emits,
                retries=retries,
                actions=actions,
                calls=made,
            )
        if scope.cancelled_caught:
            self._notify_emission(
                "emission_fallback_timeout",
                agent=agent,
                model=fallback_model,
                after_attempts=after_attempts,
                limit_s=limit,
            )
        recovered = arrived()
        if scope.cancelled_caught and not recovered:
            return first_reply, first_leg, made[0], False
        if recovered:
            operate_call = made[0]
            self.emission_fallbacks.append(
                {
                    "agent": agent,
                    "model": fallback_model,
                    "attempt": operate_call,
                    "after_attempts": after_attempts,
                }
            )
            self._notify_emission(
                "emission_recovered",
                agent=agent,
                via="fallback",
                model=fallback_model,
                attempt=operate_call,
                operate_call=operate_call,
                after_attempts=after_attempts,
            )
        return reply, leg, made[0], recovered

    async def operate_with_repair(
        self,
        branch: Branch,
        instruction: str,
        *,
        arrived: Callable[[], bool],
        emits: tuple[type, ...] = (),
        retries: int = 1,
        actions: bool = False,
        fallback: Callable[[], Awaitable[Branch]] | None = None,
        fallback_model: str | None = None,
    ) -> Any:
        """Operate then re-prompt up to *retries* times while *arrived*() is false; CLI workers get a full fenced-JSON example, API workers get key hints.

        ``actions`` must be True when the branch was given tools (e.g. bash)
        that the stage instruction requires it to actually call — CLI
        providers execute their own tools regardless, but a non-CLI (API)
        model only gets lionagi's registered tool schemas, and therefore only
        invokes them, when ``branch.operate(actions=True)``.

        ``fallback`` is a factory for ONE fresh leg (``fallback_model`` names its
        model, for the events and the record). When the first leg is still silent
        after its repairs, that leg gets the same instruction and the same
        operate-then-repair turn BEFORE the emission is recorded as missing. A fresh
        leg, not a re-prompt of the first: repair replays the whole conversation, so
        asking the same branch again only grows its context. If the fallback leg
        emits, nothing is recorded as missing: ``emission_fallbacks`` and an
        ``emission_recovered`` event name which leg produced it. If it does not,
        ``emission_missing`` counts every attempt across both legs. A factory that
        raises ``EngineBudgetError`` is skipped (``emission_fallback_skipped``) and
        the emission is recorded missing as before; ``Engine.fallback_deadline_s``
        bounds the fallback leg's time (``_run_fallback``).
        """
        res, attempt = await self._attempt_emission(
            branch, instruction, arrived=arrived, emits=emits, retries=retries, actions=actions
        )
        agent_name = getattr(branch, "name", "") or ""
        attempts = attempt + 1
        last_leg = branch
        if retries and not arrived() and fallback is not None:
            ran = await self._run_fallback(
                fallback,
                fallback_model,
                agent=agent_name,
                first_leg=branch,
                first_reply=res,
                instruction=instruction,
                arrived=arrived,
                emits=emits,
                retries=retries,
                actions=actions,
                after_attempts=attempts,
            )
            if ran is not None:
                res, last_leg, calls, recovered = ran
                if recovered:
                    return res
                attempts += calls
        if retries and not arrived():
            self._notify_emission(
                "emission_missing", agent=agent_name, leg=last_leg, attempts=attempts
            )
            self._emission_failures.append(
                f"{agent_name} x{attempts}" if agent_name else f"x{attempts}"
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
        task.add_done_callback(self._task_settled)
        return task

    def _task_settled(self, task: asyncio.Task) -> None:
        """Take a finished spawned task off the active set, keeping its failure.

        Discarding alone loses the failure of anything that finishes before the
        drain runs, and that is not the rare case: a refusal the provider issues
        without doing any work -- a bad key, an unsupported model -- arrives
        almost immediately, so the failures most certain to describe the whole
        run are the ones most certain to be gone before anyone looks. The drain
        then finds an empty set and the run reaches a verdict as though nothing
        had failed.

        Reading ``exception()`` here is also what marks the failure retrieved,
        so a real defect stops surfacing as an unretrieved-task warning from the
        event loop long after the run reported success.
        """
        self._active.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._settled_errors.append(exc)

    def _track_cli_process(self, process: Any, spawned: SpawnedProcess) -> None:
        """Associate a shared-provider CLI spawn with this run.

        A task that swallowed cancellation keeps this run's observer in its
        context and can spawn after the run has already drained. Recording that
        spawn would file it where nothing will ever look again, so a closed run
        ends the group here instead. Refusing to record without ending it would
        leak the same child more quietly.
        """

        if getattr(process, "pid", None) != spawned.pid:
            raise RuntimeError("CLI spawn identity does not match its process handle")
        if self._cli_tracking_closed:
            logger.warning(
                "CLI process pid=%s was spawned into run %s after it drained; "
                "ending its group instead of tracking it",
                spawned.pid,
                self.run_id,
            )
            self._end_cli_group_now(process, spawned)
            return
        self._cli_processes[id(process)] = _TrackedCLIProcess(
            process=process,
            spawned=spawned,
        )

    @staticmethod
    def _end_cli_group_now(process: Any, spawned: SpawnedProcess) -> None:
        """Best-effort synchronous group teardown, for callers that cannot await.

        With no witness the group is not signalled, since nothing proves it is
        still ours, but the direct child is killed through its own handle.
        """

        from lionhub.providers._cli_subprocess import (  # noqa: PLC0415
            _end_group_with_evidence,
            _witnesses_for_spawn,
        )

        try:
            witnesses = _witnesses_for_spawn(process, spawned)
            _end_group_with_evidence(process, witnesses, expected_marker=spawned._group_marker)
        except Exception:
            logger.exception("failed to end CLI group for pid=%s", spawned.pid)

    def _live_cli_processes(self) -> list[_TrackedCLIProcess]:
        """Tracked processes that still need a reap, dropping the finished ones.

        Pruning here rather than in a per-process watcher keeps the sweep on a
        path the run already walks. It also has to happen by removing the entry
        outright: the key is ``id(process)``, an address that is only unique
        while something holds the object, so a weakened reference under a
        retained key could later collide with an unrelated process.

        An exited leader is not by itself a finished group. A descendant that
        ignores SIGTERM outlives the child that anchored the group, and
        ``end_child_group`` handles exactly that case through frozen spawn-time
        witnesses -- but only if it is still called, so pruning on the leader's
        ``returncode`` alone would drop the entry before the one routine that
        can end the survivors ever ran. The group's own membership is therefore
        what decides: no surviving witness means there is nothing left to end.
        """
        from lionhub.providers._cli_subprocess import _witnesses_for_spawn  # noqa: PLC0415

        live: list[_TrackedCLIProcess] = []
        dead: list[int] = []
        for key, tracked in self._cli_processes.items():
            if tracked.reaped:
                dead.append(key)
                continue
            if getattr(tracked.process, "returncode", None) is None:
                live.append(tracked)
                continue
            try:
                survivors = _witnesses_for_spawn(tracked.process, tracked.spawned)
            except Exception:
                # An unreadable group is not an empty one; keep it for the reap
                # rather than pruning on a failed look.
                logger.exception("could not read the process group for pid=%s", tracked.spawned.pid)
                live.append(tracked)
                continue
            if survivors:
                live.append(tracked)
            else:
                dead.append(key)
        for key in dead:
            del self._cli_processes[key]
        return live

    async def _reap_cli_processes(self, processes: list[_TrackedCLIProcess]) -> tuple[int, int]:
        """End still-live, run-owned CLI groups.

        Returns ``(reaped, abandoned)``. The two are separate because a reap
        that raised leaves the child alive, and folding that into the reaped
        count would report it as the same thing as a child that had already
        exited on its own.
        """

        from lionhub.providers._cli_subprocess import (  # noqa: PLC0415
            _ChildGroupCleanup,
            end_child_group,
        )

        async def _reap_one(tracked: _TrackedCLIProcess) -> str:
            # An exited leader is not a finished group, so this deliberately
            # does not short-circuit on ``returncode``. end_child_group already
            # distinguishes the two cases -- it signals the group while the
            # child still anchors it, and falls back to frozen spawn-time
            # witnesses once the leader is gone -- and skipping the call here
            # is what let a descendant outlive the reap.
            leader_exited = getattr(tracked.process, "returncode", None) is not None
            try:
                cleanup = await end_child_group(
                    tracked.process,
                    grace=_RUN_PROCESS_TERM_GRACE_S,
                    spawned=tracked.spawned,
                )
            except Exception:
                logger.exception(
                    "cancel_active: failed to reap run-owned CLI process pid=%s",
                    tracked.spawned.pid,
                )
                return "abandoned"
            if cleanup is not _ChildGroupCleanup.CONFIRMED_EMPTY:
                logger.warning("cancel_active: run-owned CLI group cleanup remains unconfirmed")
                return "abandoned"
            # Only confirmed group absence retires the tracked entry. A failed
            # or unconfirmed cleanup remains available to the existing reap.
            tracked.reaped = True
            # A leader that had already exited is reported separately: the
            # sweep still ran, but counting it as a cancellation would overstate
            # what this run actually had to stop.
            return "already_exited" if leader_exited else "reaped"

        if not processes:
            return 0, 0
        outcomes = await asyncio.gather(*(_reap_one(item) for item in processes))
        return (
            sum(outcome == "reaped" for outcome in outcomes),
            sum(outcome == "abandoned" for outcome in outcomes),
        )

    def drain_pending(self) -> None:
        while self._pending:
            coro = self._pending.popleft()
            task = asyncio.ensure_future(coro)
            self._active.add(task)
            task.add_done_callback(self._task_settled)

    async def cancel_active(self) -> None:
        """Cancel and await all in-flight spawned tasks; tasks that don't settle
        within ``engine.cancel_timeout_s`` are abandoned. Run-owned CLI process
        groups are reaped independently so a non-cooperative task cannot keep
        its child alive."""
        tasks = list(self._active)
        processes = self._live_cli_processes()
        if not tasks and not processes:
            return
        for t in tasks:
            t.cancel()

        # Start process cleanup immediately after task cancellation rather than
        # after the task timeout. A task that swallows CancelledError is exactly
        # the case where waiting first would leave the child burning work.
        process_reaper = (
            asyncio.ensure_future(self._reap_cli_processes(processes)) if processes else None
        )
        timeout = self.engine.cancel_timeout_s
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
        else:
            pending = set()
        processes_reaped, processes_abandoned = (
            await process_reaper if process_reaper is not None else (0, 0)
        )

        # The snapshot above was taken before the tasks were cancelled, so it
        # cannot contain a process a task spawned on its way down -- and a task
        # that swallows CancelledError is free to spawn one. Rescan until a pass
        # finds no NEW arrivals, so cancel_active does not return leaving a child
        # it never saw.
        #
        # Strictly new arrivals: an entry this call already tried is not tried
        # again here. A reap that raised has been counted as abandoned once and
        # left tracked deliberately, so re-attempting it would report the same
        # stuck child several times and spend the whole budget on it. Bounded
        # because a task spawning on every sweep would otherwise loop forever,
        # and reaching the cap is logged rather than swallowed.
        #
        # Each attempted handle is held, not just its id. A reaped entry is
        # pruned from the map, and once nothing holds its handle a new spawn can
        # be given the same address; a bare id would then read the newcomer as
        # already attempted and leave it running.
        attempted = {id(item.process): item.process for item in processes}
        for _ in range(_CANCEL_RESCAN_PASSES):
            stragglers = [
                item for item in self._live_cli_processes() if id(item.process) not in attempted
            ]
            if not stragglers:
                break
            attempted.update((id(item.process), item.process) for item in stragglers)
            late_reaped, late_abandoned = await self._reap_cli_processes(stragglers)
            processes_reaped += late_reaped
            processes_abandoned += late_abandoned
        else:
            # The cap was reached, so a task is still spawning. Shut the
            # tracking door FIRST, before the scan and before the reap awaits.
            # From here ``_track_cli_process`` ends a new spawn's group on the
            # spot instead of filing it in a map that nothing will sweep again,
            # so the scan below is the complete and final set: that method is
            # the map's only writer, and it cannot add while the door is shut.
            #
            # Closing after the reap does not work, and neither does leaving it
            # to the run's own teardown, because closing tracking only governs
            # FUTURE spawns -- it does nothing about an entry that is already in
            # the map. That is how a child spawned during the final reap's await
            # stayed both tracked and alive after cancellation returned.
            #
            # Restored afterwards rather than left closed. ``cancel_active`` is
            # not always terminal: ``_degrade_export`` cancels and then runs the
            # partial export under the same spawn observer, and those spawns are
            # legitimate and must still be tracked.
            tracking_was_closed = self._cli_tracking_closed
            self._cli_tracking_closed = True
            try:
                late = [
                    item for item in self._live_cli_processes() if id(item.process) not in attempted
                ]
                if late:
                    logger.warning(
                        "cancel_active: %d CLI process(es) were still arriving after %d rescan "
                        "passes; a task is spawning faster than the reaper can clear",
                        len(late),
                        _CANCEL_RESCAN_PASSES,
                    )
                    # Reap them anyway. The cap is there to stop an endless
                    # spawn-and-rescan loop, and one final pass over the entries
                    # already in hand does not reopen that, because nothing is
                    # rescanned after it. Logging alone left these tracked and
                    # running, which is how cancel_active returned with live
                    # run-owned children -- the outcome the rescan exists to avoid.
                    late_reaped, late_abandoned = await self._reap_cli_processes(late)
                    processes_reaped += late_reaped
                    processes_abandoned += late_abandoned
            finally:
                self._cli_tracking_closed = tracking_was_closed

        record = CancellationRecord(
            tasks_cancelled=len(tasks),
            tasks_abandoned=len(pending),
            processes_reaped=processes_reaped,
            processes_abandoned=processes_abandoned,
        )
        self.cancellation_records.append(record)
        self.notify(
            "active_cancelled",
            tasks_cancelled=record.tasks_cancelled,
            tasks_abandoned=record.tasks_abandoned,
            processes_reaped=record.processes_reaped,
            processes_abandoned=record.processes_abandoned,
        )

        if pending or processes_reaped or processes_abandoned:
            names = [t.get_name() if hasattr(t, "get_name") else repr(t) for t in pending]
            logger.warning(
                "cancel_active: tasks_cancelled=%d tasks_abandoned=%d "
                "processes_reaped=%d processes_abandoned=%d after %.1fs; abandoned=%s",
                len(tasks),
                len(pending),
                processes_reaped,
                processes_abandoned,
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
            await sleep(delay)
        self._notify_budget_once("deadline_watchdog")
        # Cancel _run_task to interrupt the in-flight sequential stage promptly;
        # spawned tasks (_active) are drained separately in Engine.run()'s finally block.
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    async def wait_quiescence(self) -> None:
        """Block until all spawned tasks settle; re-raise non-cancellation, non-budget
        failures. EngineBudgetError is benign (discretionary work declined) and swallowed."""
        while self._active:
            await gather(*list(self._active), return_exceptions=True)
            # Completion schedules the done callbacks rather than running them,
            # so yield once and let the tasks just awaited record themselves
            # before the set is re-tested.
            await sleep(0)

        # Every spawned task records its own failure as it settles, whether that
        # happened during this drain or before it started. Collecting from one
        # place is what makes the two cases indistinguishable here, which is the
        # point: the drain should not be able to see one and miss the other.
        task_errors = [
            exc
            for exc in self._settled_errors
            if not isinstance(exc, asyncio.CancelledError | EngineBudgetError)
        ]
        self._settled_errors.clear()
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
        skip_signal_ops: set[Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a prebuilt operation DAG on the run's session and return operation results.

        ``skip_signal_ops`` names ops that already ran in an earlier pass, so this
        pass does not signal them a second time. Default signals every node.

        With the engine's ``agent_env`` or ``contain_account_integrations`` set,
        every node must name a branch ``make_agent`` built while the engine's
        settings were what they are now, that the session still holds under that
        id, and whose chat and parse models are still the ones it was built with.
        A node with no branch runs on the session's default branch or a clone of
        it, a node whose branch was removed runs on the default branch, and a
        spawned node may be placed on either, so ``reactive`` is refused. Every
        node must name a branch operation (``chat``, ``operate``, ``communicate``,
        ``parse``, ``ReAct``, ``ReActStream``, ``interpret``, ``act``, ``run`` or
        ``chat_and_record``), and no node may pass a model: not a value other
        than ``None`` under ``chat_model``, ``parse_model``, ``imodel``,
        ``interpret_model`` or ``analysis_model``, nor an ``iModel`` under any
        other keyword, found inside mappings, lists, tuples, sets, frozensets and
        the declared fields and extras of pydantic models. The node's calls run on
        such a model, and ``run`` leaves the model it is given on the branch for
        every later node: it sets the model before it refuses one that is not a
        CLI model. Nor may a node pass, among its own keywords, in
        ``interpret_kwargs`` or ``response_kwargs``, or in an ``imodel_kw`` in any
        of them: ``api_call``, a prebuilt call the model sends as it was built;
        ``create_event_type`` other than ``APICalling``, an event type whose
        request this check cannot see being built; ``env`` with ``agent_env`` set, which replaces the overlay for that call;
        or, with ``contain_account_integrations`` set, that keyword as anything but
        ``True`` or a non-empty ``account_connectors``, either of which lifts the
        containment for that call. Not every operation passes each of these
        keyword sets on to a model call; a keyword is refused in any of them all
        the same. Each of these raises ``ConfigurationError`` before any node
        runs. The check is made once, before the first node: a model the caller's
        own code puts on a built branch, or a keyword it sets on a later node,
        while the DAG runs is not seen. A codex ``profile``, or a config override
        under ``profile`` or ``profiles``, is not refused here: under containment
        the codex request refuses it when it builds the command line, after the
        nodes before it have run.

        The settings reach only the branches ``make_agent`` builds, through the
        model it builds for them. The run's ``session`` used directly
        (``session.flow``, a branch the caller built) does not carry them; a change
        made inside that model after it was built (its endpoint, its config, or a
        hook registered on it, such as a ``PreEventCreate`` hook returning an event
        of its own) is not checked; and a branch built with an explicit ``make_agent(env=...)``
        runs with that env, as the code that built it wrote, while its record
        holds the engine's settings, so this check accepts it.
        """
        from .flow_signals import flow_progress_signals  # noqa: PLC0415

        settings = [
            name
            for name, value in (
                ("contain_account_integrations", self.engine.contain_account_integrations),
                ("agent_env", self.engine.agent_env),
            )
            if value
        ]
        if settings:
            from lionagi._errors import ConfigurationError  # noqa: PLC0415
            from lionagi.operations.node import Operation  # noqa: PLC0415

            if reactive:
                raise ConfigurationError(
                    f"run_dag(reactive=True) is not supported with {' or '.join(settings)}: "
                    "a spawned node may run on a branch make_agent did not build, which "
                    "does not carry the setting."
                )
            in_force = (
                dict(self.engine.agent_env) if self.engine.agent_env else None,
                bool(self.engine.contain_account_integrations),
            )

            from typing import get_args  # noqa: PLC0415

            from lionagi.operations.node import BranchOperations  # noqa: PLC0415
            from lionagi.service.connections.api_calling import APICalling  # noqa: PLC0415
            from lionagi.service.imodel import iModel  # noqa: PLC0415

            def _carries(op: Operation) -> bool:
                record = self._agent_branches.get(str(op.branch_id)) if op.branch_id else None
                if record is None or record[0] != in_force:
                    return False
                _, built, chat, parse = record
                # The lookup the flow makes; a branch it cannot find runs on the default.
                try:
                    held = self.session.branches[op.branch_id]
                except Exception:
                    return False
                # Another branch included under the same id, or another chat or parse
                # model put on the built branch, does not carry the settings.
                return held is built and built.chat_model is chat and built.parse_model is parse

            def _passed(op: Operation) -> Mapping[str, Any]:
                params = op.parameters
                if isinstance(params, Mapping):
                    return params
                return {
                    name: getattr(params, name, None)
                    for name in getattr(params, "model_fields_set", ())
                }

            def _call_keywords(op: Operation) -> list[Mapping[str, Any]]:
                # The node's keywords, and the sets an operation may hand on to a model
                # call: interpret_kwargs, response_kwargs, and an imodel_kw in any of them.
                passed = _passed(op)
                found = [passed]
                for name in ("interpret_kwargs", "response_kwargs", "imodel_kw"):
                    inner = passed.get(name)
                    if isinstance(inner, Mapping):
                        found.append(inner)
                        nested = inner.get("imodel_kw")
                        if isinstance(nested, Mapping):
                            found.append(nested)
                return found

            def _lifts_containment(keywords: Mapping[str, Any]) -> bool:
                return (
                    "contain_account_integrations" in keywords
                    and keywords["contain_account_integrations"] is not True
                ) or bool(keywords.get("account_connectors"))

            def _holds_a_model(value: Any) -> bool:
                # An operation passes keywords it does not name on to the model call,
                # so a model under any keyword is looked for, inside mappings, lists,
                # tuples, sets, frozensets and the declared fields and extras of
                # pydantic models, at any depth.
                stack, seen = [value], set()
                while stack:
                    item = stack.pop()
                    if isinstance(item, iModel):
                        return True
                    if id(item) in seen:
                        continue
                    seen.add(id(item))
                    if isinstance(item, Mapping):
                        stack.extend(item.values())
                    elif isinstance(item, list | tuple | set | frozenset):
                        stack.extend(item)
                    elif isinstance(item, BaseModel):
                        stack.extend(vars(item).values())
                        stack.extend((item.model_extra or {}).values())
                return False

            # Every keyword a branch operation takes a model under.
            model_keys = (
                "chat_model",
                "parse_model",
                "imodel",
                "interpret_model",
                "analysis_model",
            )
            branch_operations = get_args(BranchOperations)
            operations = [n for n in graph.internal_nodes.values() if isinstance(n, Operation)]
            unbuilt = [op for op in operations if not _carries(op)]
            unknown = [op for op in operations if op.operation not in branch_operations]
            modelled = [
                op
                for op in operations
                if any(_passed(op).get(key) is not None for key in model_keys)
                or _holds_a_model(op.request)
            ]
            # A prebuilt call reaches the CLI as it was built, without the settings, and
            # this check cannot see how an event type other than APICalling builds its
            # request; None for api_call means "build one".
            prebuilt = [
                op
                for op in operations
                if any(
                    kw.get("api_call") is not None
                    or kw.get("create_event_type", APICalling) is not APICalling
                    for kw in _call_keywords(op)
                )
            ]
            enved = [
                op
                for op in operations
                if in_force[0] is not None and any("env" in kw for kw in _call_keywords(op))
            ]
            uncontained = [
                op
                for op in operations
                if in_force[1] and any(_lifts_containment(kw) for kw in _call_keywords(op))
            ]
            if unbuilt or unknown or modelled or prebuilt or enved or uncontained:
                total = len(operations)
                problems = []
                if unbuilt:
                    problems.append(
                        f"{len(unbuilt)} of {total} nodes name no branch make_agent built "
                        "under the settings now in force, or one the session no longer holds "
                        "under that id, or one whose chat or parse model was replaced"
                    )
                if unknown:
                    problems.append(
                        f"{len(unknown)} of {total} nodes name an operation that is not a "
                        "branch operation, whose model this check cannot see"
                    )
                if modelled:
                    problems.append(
                        f"{len(modelled)} of {total} nodes pass a model, which the node's "
                        "calls run on and run() leaves on the branch"
                    )
                if prebuilt:
                    problems.append(
                        f"{len(prebuilt)} of {total} nodes pass api_call or create_event_type, "
                        "a prebuilt call the model sends as it was built or an event type "
                        "whose request this check cannot see being built"
                    )
                if enved:
                    problems.append(
                        f"{len(enved)} of {total} nodes pass env, which replaces the "
                        "agent_env overlay for the node's calls"
                    )
                if uncontained:
                    problems.append(
                        f"{len(uncontained)} of {total} nodes pass "
                        "contain_account_integrations other than True or name "
                        "account_connectors, which lifts the containment for the node's calls"
                    )
                raise ConfigurationError(
                    f"run_dag with {' or '.join(settings)} runs only on branches make_agent "
                    "built under the settings now in force, on the models it built: "
                    f"{'; '.join(problems)}. Each could run without the setting."
                )

        async with flow_progress_signals(
            self.session, graph, skip_ops=skip_signal_ops
        ) as on_progress:
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

    def _on_collect(self, event: Any) -> None:
        """Record private occurrence data before notification can reenter collection."""

    def collect(self, event: Any) -> Any:
        """Stamp the engine-assigned eid, store the event, and notify on_event; the single notification path for all chain events."""
        prefix = self._event_prefix_map.get(type(event), "N")
        n = self._eid_counts.get(prefix, 0) + 1
        self._eid_counts[prefix] = n
        event.eid = f"{prefix}-{n}"
        self.store.setdefault(type(event), []).append(event)
        self._index[event.eid] = event
        self._on_collect(event)
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


# How many links of a judge error's chain the refusal search reads before it gives up. A real
# chain is a few links long: the model layer wraps a provider's error once or twice.
_JUDGE_CHAIN_LIMIT = 100


def _judge_refusal(
    error: BaseException,
) -> tuple[BaseException | None, BaseException | None, bool]:
    """Find a request refusal while preserving the existing containment precedence."""
    from lionagi._errors import (  # noqa: PLC0415
        ContainmentRefusedError,
        _ExclusiveMcpSelectionError,
    )

    # Retain the first request or policy refusal while looking for a containment or
    # exclusive-selection refusal, whose existing precedence must be preserved.
    # Follow whatever each link holds, nearest first. Keep each visited object
    # alive until the walk ends so a link built on a later read cannot reuse its id.
    pending: deque[object] = deque([error])
    seen: dict[int, object] = {}
    refusal = None
    visited = 0
    while pending and visited < _JUDGE_CHAIN_LIMIT:
        cause = pending.popleft()
        visited += 1
        if id(cause) in seen:
            continue
        seen[id(cause)] = cause
        if issubclass(type(cause), (ContainmentRefusedError, _ExclusiveMcpSelectionError)):
            return cause, None, True
        if refusal is None and issubclass(type(cause), _JUDGE_REFUSALS):
            refusal = cause
        for name in ("__context__", "__cause__"):
            # A read that fails ends that branch. An interrupt, an exit or a cancellation
            # raised by the read is not caught.
            try:
                link = getattr(cause, name, None)
            except Exception:  # noqa: S112
                continue
            if link is not None and visited + len(pending) < _JUDGE_CHAIN_LIMIT:
                pending.append(link)
        try:
            if is_exception_group(cause):
                members = iter(get_exception_group_exceptions(cause))
                while visited + len(pending) < _JUDGE_CHAIN_LIMIT:
                    try:
                        pending.append(next(members))
                    except StopIteration:
                        break
        except Exception:  # noqa: S110 — unreadable group members are skipped
            pass
    if refusal is not None:
        try:
            return refusal, refusal.__cause__, False
        except Exception:  # noqa: BLE001 — unreadable links are skipped
            return refusal, None, False
    return None, None, False


def check_max_tokens(name: str, value: Any) -> int:
    """*value* as an output-token limit: a positive int (a bool is not one)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {value!r}")
    return value


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
        agent_mcp_config_path: str | None = None,
        agent_env: Mapping[str, str] | None = None,
        khive_injection: Any = None,
        yolo: bool = False,
        contain_account_integrations: bool = False,
        prompt_caching: bool = False,
        max_tokens: int | None = None,
        emission_reply_text: bool = False,
        fallback_deadline_s: float | None = None,
    ) -> None:
        # Run-wide agent defaults: pin every agent to a working directory (e.g. a
        # provisioned worktree) and/or a shared standards prompt; per-call
        # make_agent(cwd=..., extra_prompt=...) still wins.
        self.agent_cwd = agent_cwd
        self.agent_extra_prompt = agent_extra_prompt
        # Which .mcp.json this engine's agents resolve. Left unset, every agent
        # falls through to the user-level ~/.lionagi/.mcp.json, which is a
        # machine-global file other tools write: a run then depends on a config
        # it never named and cannot see change underneath it, and an unrelated
        # write to that file breaks every engine on the machine at once. Naming
        # it also makes a bad path fail loudly at resolution instead of silently
        # falling back to the global one. Per-call
        # make_agent(mcp_config_path=...) still wins.
        self.agent_mcp_config_path = agent_mcp_config_path
        # Environment overlay for every CLI agent this engine spawns, merged over
        # os.environ by create_agent; per-call make_agent(env=...) still wins.
        # PlanningEngine refuses it: its workers are not built by make_agent.
        # A caller running several engines concurrently in one process cannot
        # give each its own value through os.environ, which every run shares;
        # this field is the per-engine carrier.
        if agent_env is not None and not isinstance(agent_env, Mapping):
            from lionagi._errors import ConfigurationError

            raise ConfigurationError(
                f"agent_env must be a mapping of strings to strings, got {type(agent_env).__name__}"
            )
        self.agent_env = dict(agent_env) if agent_env else None
        # Auto-approve tool permission requests for every agent this engine
        # spawns, applied per provider from the same table the CLI and profile
        # paths use. Default False: auto-approving tool execution is not
        # something a caller should receive without asking for it. What this
        # switch buys is that asking is now POSSIBLE from here -- without it the
        # table is unreachable on this path, and a CLI that cannot prompt for a
        # permission has no way to be granted one.
        self.yolo = yolo
        # Turn the signed-in account's apps and plugins off for every codex
        # agent this engine spawns. Codex attaches them from the account,
        # whatever MCP servers a request carries, and a connector acts as the
        # account. Default False keeps the agent's request as the account and
        # config set it. PlanningEngine refuses it: its workers are not built by
        # make_agent.
        self.contain_account_integrations = contain_account_integrations
        # Turn the endpoint's prompt_caching option (ADR-0127) on for every agent
        # model make_agent builds. An endpoint that does not declare prefix caching
        # (a CLI agent) sends its requests unchanged. Workers cloned from those
        # agents share an API model by reference, so they cache too.
        self.prompt_caching = prompt_caching
        # Output-token limit for every API agent model make_agent builds, unless the
        # call names its own. None leaves the provider's default (Anthropic: 8192).
        # A CLI agent has no such limit and refuses it at build.
        self.max_tokens = None if max_tokens is None else check_max_tokens("max_tokens", max_tokens)
        # Put the leg's last reply text (capped at RAW_RESPONSE_TEXT_CAP) on the
        # emission_repair / emission_missing events. Off by default: the text can
        # quote the diff or context the leg was given, and every on_event sink
        # receives the event. The scalar facts (stop reason, usage, model, block
        # types) are always on the event. A host that persists failed replies for
        # diagnosis turns it on.
        self.emission_reply_text = emission_reply_text
        # Time limit, in seconds, on the one fallback leg of operate_with_repair
        # (a stage's ``review_fallback`` model). The run's own deadline_s is shared
        # by every stage; this bounds the fallback alone. None leaves it unbounded.
        self.fallback_deadline_s = fallback_deadline_s
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
        # Set from each run (see run()); empty until one has run.
        self._emission_failures: list[str] = []

    def model_for(self, stage: str) -> str | None:
        return self.models.get(stage) or self.model

    def optional_model_for(self, stage: str) -> str | None:
        """The model set for *stage*, or None when it is unset.

        Unlike ``model_for`` this never falls back to the engine's base model. Use it for a
        stage whose absence means "skip it" (``review_fallback``), where that fallback would
        turn "unset" into a retry on the model that just failed.
        """
        return self.models.get(stage) or None

    @property
    def emission_failures(self) -> list[str]:
        """Emission-failure markers of the most recent run (a copy); empty before any run."""
        return list(self._emission_failures)

    def effort_for(self, stage: str) -> str | None:
        return self.efforts.get(stage) or self.effort

    async def judge(self, run: EngineRun, eid: str, subject: str) -> bool:
        """Return whether to expand an item, or raise when the judge cannot be trusted.

        With no judge configured this returns True; budget exhaustion returns False.
        Judge failures fail open except for request or policy refusals on the bounded
        exception chain, and failures under account containment. An
        ordinary error reporting a denial does not change that denial; an unreadable
        callback error chain or a containment or settings refusal still raises.
        """
        from lionagi._errors import ContainmentRefusedError  # noqa: PLC0415

        if not self.judge_model:
            return True
        refusal = None
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
                run._bind_agent_emitter(agent)
                with run._agent_verdicts(agent, JudgeVerdict) as verdicts:
                    res = await agent.operate(
                        instruction=_judge_instruction(eid, subject, run.root)
                    )
            for v in verdicts:
                if v.subject == eid:
                    allow, reason = v.allow, v.reason
                    break
            else:
                allow = isinstance(res, str) and res.strip().lower() == "pass"
                reason = "text-reject"
        except EngineBudgetError:
            return False
        except _AgentSettingError:
            raise
        except Exception as exc:
            # Request and policy refusals recur for the same input and must not be
            # mistaken for an ordinary infrastructure failure. Keep the traversal in a
            # separate frame so this traceback does not retain the outer error's links.
            refusal, refusal_cause, containment_refusal = _judge_refusal(exc)
            if containment_refusal:
                raise refusal from refusal.__cause__
            if refusal is None:
                # No refusal on the chain. A chain that loops back on itself, or one longer than
                # the limit, lands here too, instead of walking forever. Under containment the
                # refusal can arrive as any error type (an unreadable directory, a config the
                # request model rejects), so an error is never read as a pass.
                run.notify("judge_error", eid=eid, error=str(exc))
                if self.contain_account_integrations:
                    raise
                return True

        # Raise after the handler so the outer error is not attached as context.
        if refusal is not None:
            raise refusal from refusal_cause

        if not allow:
            try:
                run.notify("gated", eid=eid, reason=reason)
            except Exception as exc:
                # A reporting error cannot reverse a decision, but a containment or
                # configuration refusal still stops the caller. Read both links,
                # including hidden contexts and exception-group members.
                pending = deque([exc])
                seen = {}
                group_members = 0
                while pending:
                    cause = pending.popleft()
                    if cause is None or id(cause) in seen:
                        continue
                    if len(seen) >= _JUDGE_CHAIN_LIMIT:
                        # An unread chain cannot establish that it contains no refusal.
                        raise
                    seen[id(cause)] = cause
                    if issubclass(type(cause), (ContainmentRefusedError, _AgentSettingError)):
                        raise cause  # noqa: B904 — retain the refusal's own cause
                    incomplete = False
                    try:
                        if is_exception_group(cause):
                            for member in get_exception_group_exceptions(cause):
                                group_members += 1
                                if group_members > _JUDGE_CHAIN_LIMIT:
                                    incomplete = True
                                    break
                                pending.append(member)
                        for name in ("__context__", "__cause__"):
                            pending.append(getattr(cause, name, None))
                    except Exception:  # noqa: BLE001 — fail closed when a link is unreadable
                        incomplete = True
                    if incomplete:
                        raise
                # Exception text and traceback may carry secrets of an event sink.
                logger.error("gated callback failed (%s): [redacted]", type(exc).__name__)
        return allow

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
        if run._active or run._live_cli_processes():
            await run.cancel_active()
        # asyncio.shield keeps the export running outside the cancelled scope;
        # the hard timeout bounds it so a hung synthesis call can't extend the run.
        partial_coro = self._partial_export(run, *args, **kwargs)
        from lionhub.providers._cli_subprocess import (  # noqa: PLC0415
            _observe_run_cli_spawns,
        )

        with _observe_run_cli_spawns(run._track_cli_process):
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
        skipped = run.emission_failures
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
        self._last_run_id = run.run_id
        # Reset so a reused engine never carries diagnostics from a prior run.
        self._emission_failures: list[str] = []
        self._agent_errors: list[str] = []
        self._total_agent_failure: bool = False
        watchdog: asyncio.Task | None = None
        if run._deadline is not None:
            watchdog = asyncio.ensure_future(run._deadline_watchdog())
        # Wrapping _run() in a task lets the deadline bound *all* in-flight work —
        # sequential awaits inside _run(), not just spawned background tasks.
        from lionhub.providers._cli_subprocess import (  # noqa: PLC0415
            _observe_run_cli_spawns,
        )

        # Context is copied into the run task and every task it creates, then
        # reset immediately in the caller. Other concurrent runs therefore
        # cannot acquire this run's process ownership callback.
        with _observe_run_cli_spawns(run._track_cli_process):
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
            # Copy diagnostics onto the engine instance (fresh list) so the CLI reading
            # engine.emission_failures sees them on every exit path.
            self._emission_failures = run.emission_failures
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
            # Tracking closes afterwards rather than before, and on every exit path
            # including the re-raises: while the drain is still running there is a
            # loop to await on, so a late spawn is better caught by the drain's own
            # rescan and ended with a grace period. Only once nothing will sweep
            # again does an arriving spawn have to be stopped where it stands.
            try:
                if run._active or run._live_cli_processes():
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
            finally:
                run._cli_tracking_closed = True
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

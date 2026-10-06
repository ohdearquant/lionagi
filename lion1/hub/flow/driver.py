"""A flow run (ADR-0026 D3): the flow file's steps, each a run of its own under its profile, its text a
template of the submission's variables and the earlier steps' results, its row naming its run and the
steps it read. The flow's own run row is written first; the steps run in order and a fanout's items
together; the flow closes with the named step's output, or `stopped` naming the step that failed. A
profile is not read here: `cast` hands each step its actor, profile and backend, the command's own."""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lion1 import Actor, Profile, RunRequest, Store
from lion1.checkpoint import row_json
from lion1.record import Kind

__all__ = ("FlowError", "Step", "Flow", "Ended", "flow_from", "read", "render", "run_flow")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # whole: a step is named in other steps' templates
_FLOW_KEYS = frozenset({"output", "parallel", "step"})
_STEP_KEYS = frozenset({"name", "profile", "say", "each"})
_PART = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")  # an escaped brace, a name in braces, a lone brace
_PARTS = ("text", "output")

Cast = Callable[[str], AbstractAsyncContextManager[tuple[Actor, Profile, Any]]]


class FlowError(ValueError):
    """A flow refused before its run row is written: its file, a step, or a name a template reads."""


@dataclass(frozen=True)
class Step:
    name: str
    profile: str
    say: str
    each: str | None = None  # a variable, or an earlier step's `.output`, holding the list it fans out over


@dataclass(frozen=True)
class Flow:
    name: str
    steps: tuple[Step, ...]
    output: str  # the step whose output is the flow's
    parallel: int = 4  # a fanout's items running at once


@dataclass(frozen=True)
class Ended:
    id: str
    outcome: str  # success, or stopped
    output: Any  # the output step's output; for a stopped flow, the step that failed and why
    failed: str | None = None  # the failed step's row name: `<step>` or `<step>[k]`


@dataclass(frozen=True)
class _Item:
    name: str
    outcome: str
    text: str | None
    output: Any
    error: str | None = None


def flow_from(directory: Path, name: str) -> Flow:
    """`[flow.<name>]` of the directory's `flows.toml`, read now (an edit counts at the next submission,
    S4): `output` and `parallel`, and its `step` array, each with `name`, `profile`, `say` and optionally
    `each`. A key this record does not name refuses, with its name."""
    path = Path(directory) / "flows.toml"
    try:
        doc = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise FlowError(f"{path}: no flow file") from None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise FlowError(f"{path}: {e}") from None
    flows = doc.get("flow")
    if not isinstance(flows, dict) or not isinstance(flows.get(name), dict):
        there = ", ".join(sorted(flows)) if isinstance(flows, dict) and flows else "none"
        raise FlowError(f"{path}: no [flow.{name}]; there is {there}")
    table = flows[name]
    if unknown := sorted(set(table) - _FLOW_KEYS):
        raise FlowError(f"{path}: [flow.{name}] {unknown}: a flow holds output, parallel and step")
    raw = table.get("step")
    if not isinstance(raw, list) or not raw or not all(isinstance(s, dict) for s in raw):
        raise FlowError(f"{path}: [flow.{name}] holds no [[flow.{name}.step]] table")
    steps: list[Step] = []
    for i, s in enumerate(raw):
        where = f"{path}: [flow.{name}] step {i + 1}"
        if unknown := sorted(set(s) - _STEP_KEYS):
            raise FlowError(f"{where} {unknown}: a step holds name, profile, say and each")
        for key in ("name", "profile", "say"):
            if not isinstance(s.get(key), str) or not s[key]:
                raise FlowError(f"{where}: `{key}` is text, and required")
        if "each" in s and (not isinstance(s["each"], str) or not s["each"]):
            raise FlowError(f"{where}: `each` names a variable or an earlier step's .output")
        if not _NAME.fullmatch(s["name"]):
            raise FlowError(f"{where}: name {s['name']!r}: a letter or _, then letters, digits and _")
        if s["name"] in (t.name for t in steps):
            raise FlowError(f"{where}: name {s['name']!r} is an earlier step's too")
        steps.append(Step(s["name"], s["profile"], s["say"], s.get("each")))
    output = table.get("output", steps[-1].name)
    if output not in (s.name for s in steps):
        raise FlowError(f"{path}: [flow.{name}] output {output!r} names no step")
    parallel = table.get("parallel", 4)
    if isinstance(parallel, bool) or not isinstance(parallel, int) or parallel < 1:
        raise FlowError(f"{path}: [flow.{name}] parallel {parallel!r}: a whole number, 1 or more")
    return Flow(name, tuple(steps), output, parallel)


def _refs(step: Step) -> list[str]:
    """The names `step`'s template reads, in order; a lone brace refuses."""
    names = []
    for m in _PART.finditer(step.say):
        if m.group(0) in ("{{", "}}"):
            continue
        if m.group(1) is None:
            raise FlowError(
                f"step {step.name}: a lone {m.group(0)!r} in its text; write {{{{ or }}}} for one"
            )
        names.append(m.group(1))
    return names


def read(flow: Flow, variables: dict[str, str]) -> list[tuple[str, ...]]:
    """What each step reads, its row's `inputs_from`, checked whole before any row is written (C3): a
    name in braces is a variable given at submission, an earlier step's `.text` or `.output`, or a fanout
    step's `item`; `each` is a variable holding a JSON list or an earlier step's `.output`. A name that
    resolves to nothing refuses the flow, naming the step and the name."""
    if "item" in variables:
        raise FlowError("the variable `item` is a fanout step's own item; give it another name")
    earlier: list[str] = []
    reads: list[tuple[str, ...]] = []
    for step in flow.steps:
        named: set[str] = set()
        for ref in _refs(step):
            source, dot, part = ref.partition(".")
            if dot and source in earlier and part in _PARTS:
                named.add(source)
            elif ref == "item" and step.each is not None:
                continue
            elif not dot and ref in variables:
                continue
            else:
                raise FlowError(
                    f"step {step.name}: {{{ref}}} names nothing: no variable given, no earlier step's "
                    ".text or .output"
                    + ("" if step.each is not None else ", and only a fanout step has an item")
                )
        if step.each is not None:
            source, dot, part = step.each.partition(".")
            if dot and source in earlier and part == "output":
                named.add(source)
            elif not dot and step.each in variables:
                try:
                    items = json.loads(variables[step.each])
                except json.JSONDecodeError:
                    items = None
                if not isinstance(items, list):
                    raise FlowError(f"step {step.name}: each {step.each}: the variable holds no JSON list")
            else:
                raise FlowError(
                    f"step {step.name}: each {step.each} names nothing: no variable given, no earlier "
                    "step's .output"
                )
        reads.append(tuple(s for s in earlier if s in named))
        earlier.append(step.name)
    return reads


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else row_json(value)


def render(text: str, variables: dict[str, str], done: dict[str, tuple[Any, Any]], item: Any = None) -> str:
    """`text` with each name in braces replaced: a variable as given, a step's `.text` as written, its
    `.output` as JSON, the item as written when it is text and as JSON otherwise; `{{` and `}}` are one
    brace. `read` has checked every name."""

    def one(m: re.Match) -> str:
        whole, ref = m.group(0), m.group(1)
        if ref is None:
            return whole[0]
        source, dot, part = ref.partition(".")
        if dot:
            texts, output = done[source]
            return _shown(texts) if part == "text" else row_json(output)
        if ref == "item" and ref not in variables:
            return _shown(item)
        return variables[ref]

    return _PART.sub(one, text)


def _last_text(run: Any) -> str | None:
    return next((str(e.content) for e in reversed(run.record.entries) if e.kind is Kind.TEXT), None)


async def _one(
    cast: Cast,
    step: Step,
    text: str,
    row: dict[str, Any],
    opened: Callable[[str, str], None],
) -> _Item:
    """One run under the step's profile, its step row written with its run row (ADR-0026 D4). A run that
    raises is closed by the actor as `stopped`; it is this step's failure, never the flow's crash. So is
    an error after the run ended (its box's end): the item keeps the run's outcome beside the error."""
    run = None
    error = None
    try:
        async with cast(step.profile) as (actor, profile, backend):
            off = actor.bus.subscribe(
                "run.started", lambda ev: opened(f"step {row['name']}", ev.payload["run"])
            )
            try:
                request = RunRequest(inputs=(text,), trigger=(row["flow_run_id"],), step=row)
                run = await actor.run(profile, request, backend)
            finally:
                off()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    if run is None:
        return _Item(row["name"], "stopped", None, None, error)
    outcome = type(run.outcome).__name__.lower() if run.outcome is not None else "stopped"
    return _Item(row["name"], outcome, _last_text(run), run.output, error)


async def run_flow(
    store: Store,
    flow: Flow,
    variables: dict[str, str],
    cast: Cast,
    *,
    opened: Callable[[str, str], None] = lambda what, run_id: None,
    trigger: list[str] | None = None,
) -> Ended:
    """Run `flow` (C1 to C3): `read` refuses before any row; then the flow's run row; then each step,
    `opened` told each id as its run opens, a fanout's items `flow.parallel` at once. The first step that
    ends other than in success stops the flow, and no later step opens."""
    reads = read(flow, variables)
    flow_id = uuid.uuid4().hex[:8]
    done: dict[str, tuple[Any, Any]] = {}
    outcome, output, failed = "stopped", None, None
    pending: BaseException | None = None
    idx = 0
    step: Step | None = None
    opening = asyncio.ensure_future(store.open_flow(flow_id, flow.name, "flow", trigger=trigger))
    try:
        await _carried(opening, flow_id, "open", None)
        opened(f"flow {flow.name}", flow_id)
        for step, read_from in zip(flow.steps, reads, strict=True):

            def row(name: str, k: int, read_from: tuple[str, ...] = read_from) -> dict[str, Any]:
                return {"flow_run_id": flow_id, "idx": k, "name": name, "inputs_from": list(read_from)}

            if step.each is None:
                items = [
                    await _one(cast, step, render(step.say, variables, done), row(step.name, idx), opened)
                ]
                idx += 1
            else:
                source, dot, _ = step.each.partition(".")
                values = done[source][1] if dot else json.loads(variables[step.each])
                if not isinstance(values, list):
                    failed = step.name
                    output = {"failed": step.name, "error": f"each {step.each} is not a list"}
                    break
                width = asyncio.Semaphore(flow.parallel)

                async def item(
                    k: int, value: Any, step: Step = step, base: int = idx, width: asyncio.Semaphore = width
                ) -> _Item:
                    async with width:
                        text = render(step.say, variables, done, item=value)
                        return await _one(cast, step, text, row(f"{step.name}[{k}]", base + k), opened)

                items = list(await asyncio.gather(*(item(k, v) for k, v in enumerate(values))))
                idx += len(values)
            bad = next((i for i in items if i.outcome != "success" or i.error), None)
            if bad is not None:
                failed = bad.name
                output = {"failed": bad.name, "outcome": bad.outcome} | (
                    {"error": bad.error} if bad.error else {}
                )
                break
            if step.each is None:
                done[step.name] = (items[0].text, items[0].output)
            else:
                done[step.name] = ([i.text for i in items], [i.output for i in items])
        else:
            outcome, output = "success", done[flow.output][1]
    except BaseException as e:  # a cancel or an interrupt: the flow closes stopped at the step it was on
        if not _answered(opening):  # no row the store said it opened: the open's own end goes on as it is
            raise
        failed = failed or (step.name if step is not None else None)
        output = {"failed": failed, "error": type(e).__name__}
        pending = e
        raise
    finally:
        if _answered(opening):
            close = asyncio.ensure_future(store.close_flow(flow_id, outcome, output))
            await _carried(close, flow_id, "close", pending)
    return Ended(flow_id, outcome, output, failed)


def _answered(op: asyncio.Future[None]) -> bool:
    return op.done() and not op.cancelled() and op.exception() is None


async def _carried(op: asyncio.Future[None], flow_id: str, what: str, pending: BaseException | None) -> None:
    """The flow's open or close (ADR-0026 S21). An interrupt that lands in it, the command's first, lets
    it finish and goes on after it; a second cuts it, and says the row may stay open: a cancel that comes
    after a `KeyboardInterrupt`, which only a second SIGINT raises, is one. One that fails as the flow
    ends on an error or an interrupt is noted on that, which goes on in its place."""
    try:
        try:
            await asyncio.shield(op)
        except asyncio.CancelledError as e:
            task = asyncio.current_task()
            if isinstance(pending, KeyboardInterrupt) or (task is not None and task.cancelling() > 1):
                raise
            pending = e
            await asyncio.shield(op)
            raise
    except (asyncio.CancelledError, KeyboardInterrupt) as e:
        cut = pending if isinstance(pending, KeyboardInterrupt) else e  # the second goes on, not the first
        if not op.done():
            cut.add_note(f"flow {flow_id}: its {what} was cut, so its row may stay open")
        if cut is e:
            raise
        raise cut from None  # the first's late cancellation did not cause the second
    except Exception as e:
        if pending is None:
            raise
        pending.add_note(
            f"flow {flow_id}: its {what} failed ({type(e).__name__}: {e}); its row may stay open"
        )
        raise pending from e

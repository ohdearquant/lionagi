"""A conversation with an actor: one run that stays open, the other side's messages arriving as
INPUT through the actor's inbox, every entry handed to `say` as the record grows.

The record is the memory. Every message and every turn is in it, the model reads it whole through
its view, and the view folds itself once past the request's budget; nothing is summarized into a
cursor between messages. A turn without a command is the reply: the loop waits for the next message
instead of nagging about idleness (`RunRequest.wait`). A turn with commands runs them and the model
reads the results before it replies; the other side sees each round as it lands.

`entry_row` and `load_log` are the log's two directions: one JSON object per entry, and a log read
back as a run's history (`RunRequest.history`), so a conversation continues from its own record.
They live in `lionagi.checkpoint`, beside `entry_of`, so a served run's log is the same; this module
re-exports them.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from lionagi.actor import Actor, Handle, Inbound, Profile, Run, RunRequest
from lionagi.checkpoint import entry_row, load_log
from lionagi.record import Entry, Kind

__all__ = ("END", "chat", "line", "entry_row", "load_log")

END = ("/quit", "/exit", "/q")  # what the person types to end the conversation


def _no_out(run: Run, output: Any) -> str:
    """The conversation's gate: a turn with OUT{} is refused, the person ends the conversation."""
    return "this conversation has no OUT{}: the person ends it, not you; reply in text"


async def chat(
    actor: Actor,
    profile: Profile,
    backend: Any,
    ask: Callable[[], Awaitable[str | None]],
    *,
    say: Callable[[Entry], None] = lambda e: print(line(e)),
    first: str | None = None,
    max_rounds: int = 200,
    origin: str = "console",
    history: tuple[Entry, ...] = (),
    notice: str | None = None,
) -> Run:
    """Run one conversation. `ask` returns the next message, or None to end; `say` gets each record
    entry once, in order, as the run grows, the replayed `history` included, and whatever the record
    holds when the run ends however it ends. The first message is asked for when `first` is None. A
    `notice` reaches the model as an inbound INPUT from `origin` in the first round: what the console
    knows and the model must hear (a previous session that ended with commands unsettled)."""
    printed = -1  # the last seq said; the record starts at 0
    runs: list[Run] = []

    def flush(run: Run) -> None:
        nonlocal printed
        for e in run.record:
            if e.seq > printed:
                say(e)
                printed = e.seq

    def inbound(text: str) -> None:
        actor.inbox.append(
            Inbound(
                handle=Handle(uuid.uuid4().hex[:8]),
                content=text,
                origin_actor=origin,
                origin_run="-",
                origin_alias=None,
            )
        )

    async def wait(run: Run) -> bool:
        flush(run)
        text = await ask()
        if text is None or text.strip() in END:
            return False
        inbound(text)
        return True

    def started(ev) -> None:
        runs.append(actor.runs[ev.payload["run"]])
        flush(runs[0])

    offs = (
        actor.bus.subscribe("run.started", started),
        actor.bus.subscribe("round.started", lambda ev: flush(actor.runs[ev.payload["run"]])),
        # each command's request and result publish the entries as they land: a slow tool no longer
        # holds the round's turn and its settled siblings back from the log until the next round
        actor.bus.subscribe("command.", lambda ev: flush(actor.runs[ev.payload["run"]])),
    )
    try:
        opening = first if first is not None else await ask()
        if opening is None or opening.strip() in END:
            raise SystemExit(0)
        if notice:
            inbound(notice)
        request = RunRequest(
            inputs=(opening,), max_rounds=max_rounds, wait=wait, history=history, accept=_no_out
        )
        run = await actor.run(profile, request, backend)
    finally:
        for off in offs:
            off()
        if runs:  # the record is the log's, whole, however the run ended
            flush(runs[0])
    return run


def line(e: Entry) -> str:
    """The record's own line for an entry: `[seq name round] content`, a notice cut at 600 chars."""
    text = str(e.content)
    if e.kind is Kind.SYSTEM:
        text = text[:600]
    head = f"[{e.seq:>3} {e.name:>6} r{e.round}]"
    if e.kind is Kind.INPUT and e.meta.get("inbound"):
        head += f" {e.meta['origin']}:"
    return head + " " + text.replace("\n", "\n" + " " * 18)

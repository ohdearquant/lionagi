"""khive as a command: ops through the client handed in, so what the model may call is that client's
allow-list and the refusal text is the client's own. The bound is the client's: `Bounded` (with the
agent) refuses by verb name; the raw `Khive` is the transport, asserts the actor and refuses nothing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from lion1 import Actor, Context

__all__ = ("KhiveOps", "khive_ops", "Send", "send")


class KhiveOps(BaseModel):
    """Run khive ops as the console's actor: reads, its own mailbox, memory.remember at a low salience;
    every other verb is refused by name."""

    ops: str = Field(
        description='the ops string, e.g. comm.inbox(limit=5) or [gtd.next(), memory.recall(query="...")]'
    )


def khive_ops(actor: Actor, client: Any) -> set[str]:
    """Register `khive_ops` over `client` (a `Bounded`, or anything with `exec(ops)`)."""

    @actor.handler(KhiveOps)
    async def run(req: KhiveOps, ctx: Context) -> Any:
        return await client.exec(req.ops)

    return {"khive_ops"}


class Send(BaseModel):
    """Mail the actor that submitted this job: a finding it should read before the job ends, or why the
    job cannot go on. The job's result reaches it on its own when the job ends."""

    text: str


def send(actor: Actor, client: Any, to: str, *, thread: str | None = None) -> set[str]:
    """Register `send` over `client` (a `Khive`): one recipient, `to`, on `thread` when the submission
    named one (ADR-0026 D2). Its privilege is `comm.send`, which the actor and the profile must hold."""

    @actor.handler(Send, requires={"comm.send"})
    async def run(req: Send, ctx: Context) -> str:
        sent = await client.send(to, req.text, thread_id=thread, self_send=to == client.actor)
        return f"sent {sent.get('id')} to {to}"

    return {"send"}

"""khive as a command: ops through the client handed in, so what the model may call is that client's
allow-list and the refusal text is the client's own. The bound is the client's: `Bounded` (with the
agent) refuses by verb name; the raw `Khive` is the transport, asserts the actor and refuses nothing."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from lion1 import Actor, Context

__all__ = ("KhiveOps", "khive_ops")


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

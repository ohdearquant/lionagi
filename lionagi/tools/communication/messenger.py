# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LionMessenger: exchange-bound communication tool for branch-to-branch messaging."""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from lionagi.protocols.action.tool import Tool

from ..base import LionTool

if TYPE_CHECKING:
    from lionagi.session.branch import Branch
    from lionagi.session.exchange import Exchange

__all__ = ("LionMessenger",)

logger = logging.getLogger(__name__)


class MessengerAction(str, Enum):
    send = "send"
    receive = "receive"
    done = "done"
    finished = "finished"
    wakeup = "wakeup"
    help = "help"


class MessengerRequest(BaseModel):
    action: MessengerAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'send': Send a message to one or more recipients.\n"
            "- 'receive': Read and consume pending messages from teammates.\n"
            "- 'done': Signal you've finished your part (can be woken later).\n"
            "- 'finished': Permanently done, cannot be woken.\n"
            "- 'wakeup': Wake a teammate who is in done state.\n"
            "- 'help': Signal you need input or authority you don't have — for when you "
            "don't know which peer to ask, or already tried peers and are still stuck. "
            "Fires and continues; never blocks waiting for a reply."
        ),
    )
    to: str | list[str] | None = Field(
        None,
        description="Recipient name(s). Required for 'send' and 'wakeup'.",
    )
    content: str | None = Field(
        None,
        description="Message content (send/wakeup), reason (done/finished), or the help reason (help).",
    )
    urgency: Literal["fyi", "blocked"] | None = Field(
        None,
        description=(
            "Only for 'help'. 'fyi' (default): soft, you are continuing. "
            "'blocked': hard, you cannot proceed without input."
        ),
    )


class LionMessenger(LionTool):
    """Exchange-bound messaging tool; call bind(branch, roster) to produce a branch-scoped Tool."""

    is_lion_system_tool = True
    system_tool_name = "messenger"

    def __init__(self, exchange: Exchange):
        super().__init__()
        self.exchange = exchange
        self._callbacks: dict[str, Any] = {}

    def on(self, event: str, callback):
        """Register callbacks for state events (done, finished, wakeup)."""
        self._callbacks[event] = callback

    def _fire(self, event: str, **kwargs):
        cb = self._callbacks.get(event)
        if cb:
            if event == "help":
                # Best-effort: a raising coordinator callback must never
                # surface as an unhandled exception on the emitting worker's
                # tool-call turn — the whole point of fire-and-continue.
                try:
                    cb(**kwargs)
                except Exception:
                    logger.warning(
                        "LionMessenger: callback for event=%r raised; ignoring (fire-and-continue)",
                        event,
                        exc_info=True,
                    )
            else:
                cb(**kwargs)
        else:
            # A mis-wired coordinator (nobody called .on(event, ...)) must be
            # discoverable during bring-up, not silently inert — debug-level
            # so it doesn't spam normal runs where some events are unused.
            logger.debug(
                "LionMessenger: no callback registered for event=%r (kwargs=%r)",
                event,
                kwargs,
            )

    def bind(
        self,
        branch: Branch,
        roster: dict[str, UUID],
        sender_name: str | None = None,
        channel: str = "team",
    ) -> Tool:
        """Return a Tool scoped to branch as sender with roster as valid recipients."""
        from lionagi.protocols.messages import Message

        exchange = self.exchange
        fire = self._fire
        sender_id = branch.id
        _sender_name = sender_name or str(sender_id)[:8]

        def _track(msg: Message):
            """Add message to branch progression for persistence."""
            if msg not in branch.msgs.messages:
                branch.msgs.messages.include(msg)

        def messenger(
            action: str,
            to: str | list[str] = None,
            content: str = None,
            urgency: str = None,
        ) -> str:
            """Send messages to teammates, receive pending ones, signal
            done/finished, wake a teammate, or send a help signal. action in
            {'send', 'receive', 'done', 'finished', 'wakeup', 'help'}; to
            (name or list of names) and content are required for
            send/wakeup, neither is required for receive; content (the
            reason) is required for help, urgency is optional (defaults to
            'fyi')."""
            if action == "receive":
                pending = exchange.receive(sender_id)
                if not pending:
                    return "No new messages."
                name_by_id = {v: k for k, v in roster.items()}
                senders = {m.sender for m in pending}
                drained: list[Message] = []
                for s in senders:
                    while (m := exchange.pop_message(owner_id=sender_id, sender=s)) is not None:
                        drained.append(m)
                drained.sort(key=lambda m: m.created_datetime)
                lines = []
                for m in drained:
                    from_name = name_by_id.get(m.sender, str(m.sender)[:8])
                    lines.append(f"[{from_name}] {m.content}")
                return "\n".join(lines)

            if action == "send":
                if not to or not content:
                    return "Error: 'send' requires both 'to' and 'content'."
                targets = [to] if isinstance(to, str) else to
                results = []
                for name in targets:
                    if name not in roster:
                        results.append(f"Unknown recipient: {name}")
                        continue
                    msg = exchange.send(
                        sender=sender_id,
                        recipient=roster[name],
                        content=content,
                        channel=channel,
                    )
                    _track(msg)
                    results.append(f"Sent to {name}")
                return "; ".join(results)

            elif action == "done":
                fire("done", name=_sender_name, sender_id=sender_id, reason=content or "")
                return f"{_sender_name} is now done: {content or 'no reason'}"

            elif action == "finished":
                fire(
                    "finished",
                    name=_sender_name,
                    sender_id=sender_id,
                    reason=content or "",
                )
                return f"{_sender_name} is permanently finished: {content or 'no reason'}"

            elif action == "wakeup":
                if not to or not content:
                    return "Error: 'wakeup' requires both 'to' and 'content'."
                target_name = to if isinstance(to, str) else to[0]
                if target_name not in roster:
                    return f"Unknown teammate: {target_name}"
                msg = exchange.send(
                    sender=sender_id,
                    recipient=roster[target_name],
                    content=f"[WAKEUP] {content}",
                    channel=channel,
                )
                _track(msg)
                fire(
                    "wakeup",
                    name=_sender_name,
                    sender_id=sender_id,
                    target=target_name,
                    message=content,
                )
                return f"Woke up {target_name}"

            elif action == "help":
                if not content:
                    return "Error: 'help' requires 'content' (the reason you need help)."
                _urgency = urgency or "fyi"
                fire(
                    "help",
                    name=_sender_name,
                    sender_id=sender_id,
                    reason=content,
                    urgency=_urgency,
                )
                return f"{_sender_name} sent a help signal ({_urgency}): {content}"

            return f"Unknown action: {action}"

        return Tool(func_callable=messenger, request_options=MessengerRequest)

    def to_tool(self) -> Tool:
        raise NotImplementedError(
            "LionMessenger requires branch context. Use messenger.bind(branch_id, roster) instead."
        )

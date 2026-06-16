# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""LionMessenger: exchange-bound communication tool for branch-to-branch messaging."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, Field

from lionagi.protocols.action.tool import Tool

from ..base import LionTool

if TYPE_CHECKING:
    from lionagi.session.branch import Branch
    from lionagi.session.exchange import Exchange

__all__ = ("LionMessenger",)


class MessengerAction(str, Enum):
    send = "send"
    done = "done"
    finished = "finished"
    wakeup = "wakeup"


class MessengerRequest(BaseModel):
    action: MessengerAction = Field(
        ...,
        description=(
            "Action to perform. One of:\n"
            "- 'send': Send a message to one or more recipients.\n"
            "- 'done': Signal you've finished your part (can be woken later).\n"
            "- 'finished': Permanently done, cannot be woken.\n"
            "- 'wakeup': Wake a teammate who is in done state."
        ),
    )
    to: str | list[str] | None = Field(
        None,
        description="Recipient name(s). Required for 'send' and 'wakeup'.",
    )
    content: str | None = Field(
        None,
        description="Message content (send/wakeup) or reason (done/finished).",
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
            cb(**kwargs)

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

        def messenger(action: str, to: str | list[str] = None, content: str = None) -> str:
            """Send messages to teammates, signal done/finished, or wake a teammate.

            Args:
                action: One of 'send', 'done', 'finished', 'wakeup'.
                to: Recipient name or list of names. Required for send/wakeup.
                content: Message body (send/wakeup) or reason (done/finished).

            Returns:
                Confirmation string.
            """
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

            return f"Unknown action: {action}"

        return Tool(func_callable=messenger, request_options=MessengerRequest)

    def to_tool(self) -> Tool:
        raise NotImplementedError(
            "LionMessenger requires branch context. Use messenger.bind(branch_id, roster) instead."
        )

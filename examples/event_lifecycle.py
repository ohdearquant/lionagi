"""Event lifecycle — total invoke semantics, status transitions, and timing.

lionagi's Event is the fundamental execution primitive. Its ``invoke()``
method is *total*: business failures are captured as FAILED status (never
propagated), while cancellation (BaseException) propagates. This makes
``asyncio.gather(*events)`` safe without ``return_exceptions``.

No LLM calls required — runs instantly.

    uv run python examples/event_lifecycle.py
"""

from __future__ import annotations

import asyncio

from lionagi.protocols.generic.event import Event, EventStatus


class HttpFetch(Event):
    """Simulate an HTTP fetch that succeeds."""

    async def _invoke(self) -> dict:
        await asyncio.sleep(0.01)
        return {"status": 200, "body": "ok"}


class HttpError(Event):
    """Simulate an HTTP fetch that fails (4xx/5xx)."""

    async def _invoke(self) -> dict:
        await asyncio.sleep(0.01)
        raise ConnectionError("503 Service Unavailable")


class SlowTask(Event):
    """A task that takes too long — meant to be cancelled."""

    async def _invoke(self) -> str:
        await asyncio.sleep(60)
        return "unreachable"


async def main():
    # ── Success ──────────────────────────────────────────────────────────
    fetch = HttpFetch()
    assert fetch.status == EventStatus.PENDING
    await fetch.invoke()
    assert fetch.status == EventStatus.COMPLETED
    assert fetch.execution.response == {"status": 200, "body": "ok"}
    print(f"[1] Success: {fetch.status.value}, response={fetch.execution.response}")

    # ── Failure captured, not raised ─────────────────────────────────────
    err = HttpError()
    await err.invoke()  # does NOT raise
    assert err.status == EventStatus.FAILED
    assert err.execution.error is not None
    print(f"[2] Failure: {err.status.value}, error captured (ConnectionError)")

    # ── Idempotency — re-invoke is a no-op ───────────────────────────────
    await fetch.invoke()  # already COMPLETED
    assert fetch.status == EventStatus.COMPLETED
    print("[3] Idempotency: re-invoke on COMPLETED is no-op")

    # ── Timing ───────────────────────────────────────────────────────────
    timed = HttpFetch()
    await timed.invoke()
    assert timed.execution.duration > 0
    print(f"[4] Timing: duration={timed.execution.duration:.4f}s")

    # ── Cancellation propagates (BaseException) ──────────────────────────
    slow = SlowTask()
    task = asyncio.create_task(slow.invoke())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
        raise AssertionError("CancelledError should propagate")
    except asyncio.CancelledError:
        pass
    assert slow.status == EventStatus.CANCELLED
    print(f"[5] Cancellation: {slow.status.value} — BaseException propagated")

    # ── Mixed gather — no exception leaks ────────────────────────────────
    batch = [HttpFetch(), HttpError(), HttpFetch(), HttpError(), HttpFetch()]
    await asyncio.gather(*[e.invoke() for e in batch])
    statuses = [e.status.value for e in batch]
    assert statuses == ["completed", "failed", "completed", "failed", "completed"]
    print(f"[6] Mixed gather: {statuses}")

    # ── Completion event signalling ──────────────────────────────────────
    ev = HttpFetch()
    ce = ev.completion_event
    assert not ce.is_set()
    await ev.invoke()
    assert ce.is_set()
    print("[7] Completion event: signalled on terminal status")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

"""Hook bus — lifecycle instrumentation via the session observer.

The HookBus provides ordered, point-specific handlers (guards, logging,
metrics) that record onto the session's observer as typed HookSignals.
This lets you instrument any part of the session lifecycle — message adds,
API calls, session boundaries — with a single, composable mechanism.

No LLM calls required — runs instantly.

    uv run python examples/hook_bus.py
"""

from __future__ import annotations

import asyncio

from lionagi.hooks import HookBus, HookPoint, HookSignal
from lionagi.session.branch import Branch
from lionagi.session.session import Session


async def main():
    session = Session()
    branch = Branch()
    session.include_branches(branch)

    # ── Ordered handlers (fire in registration order) ────────────────────
    api_log: list[str] = []
    bus = HookBus(observer=session.observer)
    bus.on(HookPoint.MESSAGE_ADD, lambda **kw: api_log.append(f"msg:{kw.get('role', '?')}"))
    bus.on(HookPoint.API_PRE_CALL, lambda **kw: api_log.append(f"pre:{kw.get('model', '?')}"))
    bus.on(HookPoint.API_POST_CALL, lambda **kw: api_log.append(f"post:{kw.get('model', '?')}"))

    # ── Reactive observer (sees ALL HookSignals) ─────────────────────────
    reactive_log: list[str] = []
    session.observe(HookSignal, handler=lambda s, _c: reactive_log.append(str(s.point)))

    # ── Emit lifecycle signals ───────────────────────────────────────────
    await bus.emit(HookPoint.SESSION_START, model="gpt-5.3-codex-spark")
    await bus.emit(HookPoint.MESSAGE_ADD, role="system", content="You are a helpful assistant.")
    await bus.emit(HookPoint.MESSAGE_ADD, role="user", content="Hello")
    await bus.emit(HookPoint.API_PRE_CALL, model="gpt-5.3-codex-spark")
    await bus.emit(HookPoint.API_POST_CALL, model="gpt-5.3-codex-spark", tokens=142)
    await bus.emit(HookPoint.SESSION_END, status="completed")

    print(f"Ordered handlers: {api_log}")
    assert api_log == [
        "msg:system",
        "msg:user",
        "pre:gpt-5.3-codex-spark",
        "post:gpt-5.3-codex-spark",
    ]

    print(f"Reactive observer: {reactive_log}")
    assert len(reactive_log) == 6

    # ── Query the observer by type ───────────────────────────────────────
    recorded = session.observer.by_type(HookSignal)
    points = [r.point for r in recorded]
    assert points == [
        "session.start",
        "message.add",
        "message.add",
        "api.pre_call",
        "api.post_call",
        "session.end",
    ]
    print(f"Observer stored: {points}")

    # ── Bind/unbind ──────────────────────────────────────────────────────
    bus.bind(None)  # detach from observer
    await bus.emit(HookPoint.MESSAGE_ADD, role="test")
    assert len(session.observer.by_type(HookSignal)) == 6  # no new signal
    bus.bind(session.observer)  # reattach
    await bus.emit(HookPoint.MESSAGE_ADD, role="test")
    assert len(session.observer.by_type(HookSignal)) == 7  # recorded again
    print("Bind/unbind: works")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

"""Engine lifecycle — stateless engines with per-run contexts.

An Engine is a reusable, stateless configuration; each ``run()`` creates
a fresh EngineRun with its own Session, dedup state, event store, and
concurrency control. Multiple runs of the same engine never collide.

No LLM calls required — runs instantly.

    uv run python examples/engine_lifecycle.py
"""

from __future__ import annotations

import asyncio

from lionagi.engines.engine import Engine, EngineEvent, EngineRun
from lionagi.session.session import Session


class BugReport(EngineEvent):
    file: str = ""
    line: int = 0
    summary: str = ""
    severity: str = "low"


class AuditEngine(Engine):
    name: str = "audit-engine"

    async def run(self, prompt: str, *, session: Session | None = None, **kw):
        return EngineRun(self, session=session)


async def main():
    engine = AuditEngine(model="codex")
    print(f"Engine: {engine.name}")

    # ── Fresh session per run ────────────────────────────────────────────
    run1 = await engine.run("first")
    run2 = await engine.run("second")
    assert run1.session.id != run2.session.id
    print(f"Two runs, two sessions: {str(run1.session.id)[:8]} vs {str(run2.session.id)[:8]}")

    # ── Shared session (for continuing work) ─────────────────────────────
    shared = Session()
    run3 = await engine.run("shared", session=shared)
    assert run3.session is shared

    # ── Domain events (emit + query) ─────────────────────────────────────
    notifications: list[str] = []
    run = await engine.run("event test")
    run.on_event = lambda d: notifications.append(d.get("type", "?"))

    await run.emit(BugReport(file="auth.py", line=42, summary="SQL injection", severity="high"))
    await run.emit(
        BugReport(file="api.py", line=88, summary="Missing rate limit", severity="medium")
    )

    print(f"Emitted 2 domain events, callback saw: {notifications}")
    assert notifications == ["BugReport", "BugReport"]

    # ── Dedup (case-insensitive) ─────────────────────────────────────────
    assert not run.seen("sql injection in auth")
    assert run.seen("sql injection in auth")  # already seen
    assert run.seen("SQL INJECTION IN AUTH")  # case-insensitive
    assert not run.seen("missing rate limit")
    print("Dedup: case-insensitive, normalized")

    # ── Agent construction from casts ────────────────────────────────────
    agent = await run.make_agent("auditor", name="sec-auditor", modes=["evidential"])
    print(f"make_agent: {agent.name} (branch {str(agent.id)[:8]})")

    # ── Bounded recursion (spawn + quiescence) ───────────────────────────
    results: list[int] = []

    async def worker(n: int):
        await asyncio.sleep(0.01)
        results.append(n)

    for i in range(5):
        run.spawn(worker(i))
    await run.wait_quiescence()
    assert sorted(results) == [0, 1, 2, 3, 4]
    print(f"spawn+quiescence: {sorted(results)}")

    # ── Concurrent runs stay isolated ────────────────────────────────────
    async def isolated(label: str):
        r = await engine.run(label)
        return not r.seen(label)  # should be first-seen

    flags = await asyncio.gather(*[isolated(f"run-{i}") for i in range(5)])
    assert all(flags)
    print("5 concurrent runs: isolated")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

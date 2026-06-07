"""Pile — type-safe collections with O(1) UUID access.

Pile is lionagi's core collection: a UUID-keyed store that supports
type-based queries (``pile[Task]``), inclusion/exclusion, and safe
concurrent access. Progression provides ordered views over the same Pile.

No LLM calls required — runs instantly.

    uv run python examples/pile_and_types.py
"""

from __future__ import annotations

import asyncio

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression


class Task(Element):
    title: str = ""
    priority: int = 0
    done: bool = False


class Note(Element):
    content: str = ""


async def main():
    pile = Pile()

    # ── Populate ─────────────────────────────────────────────────────────
    tasks = [
        Task(title="Fix auth bug", priority=1, done=False),
        Task(title="Write API docs", priority=2, done=True),
        Task(title="Deploy v2", priority=1, done=False),
    ]
    notes = [
        Note(content="Edge case in token refresh"),
        Note(content="Meeting: Q3 roadmap"),
    ]
    for item in [*tasks, *notes]:
        pile.include(item)

    print(f"Pile: {len(pile)} items")

    # ── Type-based query ─────────────────────────────────────────────────
    found_tasks = pile[Task]
    found_notes = pile[Note]
    print(f"pile[Task]: {len(found_tasks)} — {[t.title for t in found_tasks]}")
    print(f"pile[Note]: {len(found_notes)} — {[n.content[:30] for n in found_notes]}")
    assert len(found_tasks) == 3
    assert len(found_notes) == 2

    # ── UUID access ──────────────────────────────────────────────────────
    fetched = pile[tasks[0].id]
    assert fetched is tasks[0]

    # ── Filtering ────────────────────────────────────────────────────────
    open_p1 = [t for t in pile[Task] if t.priority == 1 and not t.done]
    print(f"Open P1 tasks: {[t.title for t in open_p1]}")
    assert len(open_p1) == 2

    # ── Membership + exclusion ───────────────────────────────────────────
    assert tasks[1] in pile
    pile.exclude(tasks[1])
    assert tasks[1] not in pile
    assert len(pile) == 4

    # ── Progression (ordered view) ───────────────────────────────────────
    prog = Progression()
    for t in pile[Task]:
        prog.append(t.id)
    print(f"Progression: {len(prog)} items (ordered task IDs)")
    assert len(prog) == 2  # tasks[1] was excluded

    # ── Bulk operations ──────────────────────────────────────────────────
    bulk = [Task(title=f"batch-{i}", priority=3) for i in range(50)]
    for item in bulk:
        pile.include(item)
    assert len(pile) == 54
    print(f"After bulk add: {len(pile)} items")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())

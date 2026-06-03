# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Prompt fragments used by orchestration patterns.

Role *behaviour* lives in the casts roles (each role's ``body``); these are the
small orchestration-level framings that sit on top — how to plan, synthesize,
and grow the DAG. Kept here so they are curated in one place rather than inlined.
"""

# ── Planning (orchestrator emits a list[TaskAssignment]) ──────────────────────
#
# The orchestrator decomposes the task into TaskAssignments — the casts
# coordination emission. ``assignee`` names a role from the roster; ``task`` is
# the concrete objective. There is no bespoke plan model: a list of
# TaskAssignments *is* the plan (and, with depends_on, the DAG).

DECOMPOSE_INSTRUCTION = """\
Decompose the task into parallel TaskAssignments — one per distinct angle or \
unit of work. Each assignment goes to ONE worker that answers it directly. \
Set `assignee` to a role from the roster. Leave `depends_on` empty: all \
assignments run concurrently and fan back into an optional synthesis."""

DECOMPOSE_DAG_INSTRUCTION = """\
Decompose the task into a DAG of TaskAssignments. For each assignment:
- `task`: the concrete unit of work.
- `assignee`: a role from the roster (decompose by dependency boundary, not by \
topic — two subtasks that share state are one assignment).
- `depends_on`: the 1-based step numbers of earlier assignments whose output \
this one needs. Leave empty for independent work; independent assignments run \
in parallel. Number assignments by their position in your list (the first is \
step 1).
Keep the assignment count tight — reuse a role across steps rather than \
spawning many one-shot roles. Order matters only through depends_on."""

DECOMPOSE_DISCIPLINE = """\
Produce your output ONLY via the structured `assignments` field. Do NOT use any \
provider-native subagent or tool-spawning feature (no Agent tool, no subprocess \
spawning, no delegation) and do NOT perform the task yourself — the ONLY correct \
output is the TaskAssignment list. Use ONLY roles from the roster; do not invent \
role names. Each `task` must be a DIRECT objective the assignee executes, not a \
meta-instruction to decompose further."""

SPAWN_GUIDANCE = """\
If you uncover necessary work outside your current assignment, emit a \
`spawn_request` to add it to the running workflow rather than doing it \
yourself or dropping it:
- instruction: the new unit of work — concrete and self-contained.
- assignee: the role best suited to it (omit to keep it on your own branch).
- independent: true if it can start now; false (default) if it builds on what \
you just produced.
- reason: why it is needed and why it was not in the original plan.
Spawn only for genuinely necessary, adjacent work — not speculation."""

SYNTHESIS_INSTRUCTION = """\
You have the outputs of several agents that worked in parallel on one task. \
Produce a single integrated result: reconcile overlaps and conflicts, fill the \
gaps none of them covered, and organize by topic — not by which agent produced \
what. Preserve concrete specifics over summary."""

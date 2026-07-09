# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Prompt fragments for orchestration: planning, synthesis, spawn guidance, and decomposition."""

from __future__ import annotations

__all__ = (
    "DECOMPOSE_INSTRUCTION",
    "DECOMPOSE_DAG_INSTRUCTION",
    "DECOMPOSE_DISCIPLINE",
    "SPAWN_GUIDANCE",
    "SYNTHESIS_INSTRUCTION",
)

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
Decompose the task into a DAG of TaskAssignments that is as WIDE as the work \
allows. Assignments run concurrently unless a dependency forces an order, and \
wall-clock time is set by the LONGEST CHAIN, not the assignment count — so a \
near-linear plan is a failed decomposition. For each assignment:
- `task`: the concrete unit of work.
- `assignee`: a role from the roster (decompose by dependency boundary, not by \
topic — two subtasks that share state are one assignment).
- `depends_on`: the 1-based step numbers of earlier assignments whose OUTPUT \
this one actually consumes. Leave empty for independent work. Never add a \
dependency for sequencing taste, shared theme, or "logical order" — only for a \
real data dependency. Number assignments by their position in your list (the \
first is step 1).
Structure the plan in broad layers: fan out ALL independent reads, \
investigations, and per-file or per-module work in parallel first; join only \
where results must combine; then fan out again. Two assignments touching \
different files or answering different questions are independent — run them \
concurrently. Keep the assignment count tight (reuse a role across steps \
rather than spawning many one-shot roles), but never serialize independent \
work to reduce the count. Before answering, check every depends_on and remove \
the ones that are not true data dependencies; if most assignments form a \
single chain, re-plan wider."""

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

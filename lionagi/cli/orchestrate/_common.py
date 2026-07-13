# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared formatting, team helpers, and worker-prompt fragments."""

from __future__ import annotations

from collections.abc import Callable

from lionagi import json_dumps
from lionagi.ln._utils import now_utc

from .. import team as _team_module
from ..team import _locked_team

# ── Output formatting ─────────────────────────────────────────────────────


def _format_result_text(
    worker_results: list[dict],
    synthesis_result: dict | None = None,
    *,
    header_fn: Callable[[dict, int, int], list[str]] | None = None,
) -> str:
    lines = []
    n = len(worker_results)
    for i, w in enumerate(worker_results, 1):
        lines.append(f"{'═' * 60}")
        if header_fn is not None:
            lines.extend(header_fn(w, i, n))
        else:
            lines.append(f"  Worker {i}/{n}  [{w['model']}]")
        lines.append(f"  {w['time_ms']:.0f}ms")
        lines.append(f"{'═' * 60}")
        lines.append(w.get("response", "(no response)"))
        lines.append("")

    if synthesis_result is not None:
        lines.append(f"{'═' * 60}")
        lines.append(f"  Synthesis  [{synthesis_result['model']}]")
        lines.append(f"  {synthesis_result['time_ms']:.0f}ms")
        lines.append(f"{'═' * 60}")
        lines.append(synthesis_result.get("response", "(no response)"))
        lines.append("")

    return "\n".join(lines)


def _format_result_json(
    worker_results: list[dict],
    synthesis_result: dict | None = None,
) -> str:
    out = {"workers": worker_results}
    if synthesis_result is not None:
        out["synthesis"] = synthesis_result
    return json_dumps(out)


# ── Default worker system prompt (shared by flow + fanout) ────────────────


def _bare_worker_system() -> str:
    from lionagi.session.prompts import LION_SYSTEM_MESSAGE

    return LION_SYSTEM_MESSAGE.strip() + "\n\n" + _BARE_WORKER_BODY


_BARE_WORKER_BODY = """\
You are a specialist worker agent in a DAG pipeline. \
Complete your assigned task directly and precisely. \
You may read files, use tools, and run commands as needed. \
Do NOT spawn sub-agents or delegate further — you are a leaf executor.

Follow artifact and path conventions specified in your instruction. \
Your instruction tells you where to write output and how to reference \
upstream artifacts from dependent ops.

SESSION PERSISTENCE: Your session persists. If given follow-up work \
later, your conversation history is retained.

BASH QUOTING: Use variable assignment for multi-word CLI args: \
Q="your query" && command "$Q" (NOT command "your query").\
"""

BARE_WORKER_SYSTEM = _bare_worker_system()


# ── Team-mode coordination section ────────────────────────────────────────
#
# Appended onto the base worker system prompt (BARE_WORKER_SYSTEM or a
# profile's system_prompt) when the worker runs in team mode. This is a
# SECTION, not a replacement — workers still need the artifact protocol
# and tool guidance from the base prompt.
#
# Two variants exist because a team-mode worker reaches the team through
# exactly one of two disjoint channels, never both: CLI-provider workers
# (codex/gemini subprocesses, no tool-calling surface) get only the bash
# `li team` channel; API-model workers additionally get the in-process
# `messenger` tool bound to their branch and should coordinate through that
# instead. Which variant applies is decided by `messenger_bound` in
# `build_worker_branch` before the system prompt is assembled — see
# `team_worker_system()` in `_orchestration.py`.

TEAM_COORD_SECTION = """\
## Team Coordination

You are **{worker_name}** on team "{team_name}" (id: {team_id}).

### Your team
{roster_text}

### Protocol

**Before starting work**: Check your inbox.
```bash
li team receive -t {team_id} --as {worker_name}
```

**During work**: Send coordination signals to teammates when you discover \
something affecting them. Keep them short and actionable — NOT full deliverables.
```bash
li team send "Found 3 undocumented endpoints — hold off on gap analysis \
until I update inventory" -t {team_id} --to analyst --from {worker_name} \
--from-op <your_op_id>
```
The `--from-op` tag ties the message to your specific invocation so \
downstream ops can trace which turn emitted it.

**After work**: Your artifact files are the deliverable. Team messages \
are supplementary — full results are auto-posted to the team at flow end.

### What goes where
- **Team messages**: coordination signals, warnings, discoveries affecting others
- **Artifact files**: structured deliverables (still your primary output)
- **stdout**: progress updates only

### Resuming
After this round, teammates or the orchestrator can follow up:
- `li team receive -t {team_id} --as {worker_name}` to read messages
- `li team send "..." -t {team_id} --to {worker_name}` to reply
- `li agent -r {{branch_id}} "follow-up"` to continue your session\
"""

TEAM_COORD_SECTION_MESSENGER = """\
## Team Coordination

You are **{worker_name}** on team "{team_name}" (id: {team_id}).

### Your team
{roster_text}

### Protocol

You have the **messenger** tool bound to this session — use it for team \
coordination. You do NOT have a `li team` shell channel; the messenger tool \
is your only coordination path.

**Before starting work**: Call the messenger tool with `action="receive"` \
to check your inbox for anything relevant from teammates.

**During work**: Call the messenger tool with `action="send"`, \
`to="<teammate>"`, and `content="..."` to send coordination signals when \
you discover something affecting them. Keep them short and actionable — \
NOT full deliverables.

**If you get stuck**: Call the messenger tool with `action="help"`, \
`content="<reason>"`, and `urgency="fyi"` (soft, you're continuing) or \
`urgency="blocked"` (hard, you cannot proceed) to signal you need input or \
authority you don't have.

**After work**: Your artifact files are the deliverable. Team messages \
are supplementary — full results are auto-posted to the team at flow end.

### What goes where
- **Team messages** (via the messenger tool): coordination signals, \
warnings, discoveries affecting others
- **Artifact files**: structured deliverables (still your primary output)
- **stdout**: progress updates only

### Resuming
After this round, teammates or the orchestrator can follow up:
- Call the messenger tool with `action="receive"` to read messages
- Call the messenger tool with `action="send"` to reply
- `li agent -r {{branch_id}} "follow-up"` to continue your session\
"""

# Deprecated: TEAM_WORKER_SYSTEM is a backward-compatible composed alias for
# the old standalone template ("You are a specialist..." + TEAM_COORD_SECTION).
# It has no production caller in this repository. A module-level constant
# cannot emit a call-time DeprecationWarning without added attribute-access
# machinery, so this comment (plus the changelog and docs) is the deprecation
# signal for this cycle. Use TEAM_COORD_SECTION directly, appended onto the
# worker's own system prompt, instead of importing TEAM_WORKER_SYSTEM.
TEAM_WORKER_SYSTEM = BARE_WORKER_SYSTEM + "\n\n" + TEAM_COORD_SECTION


def _build_worker_operate_node(
    builder,
    *,
    branch,
    instruction,
    context: list,
    messenger_bound: bool,
    depends_on: list[str] | None = None,
) -> str:
    """Add the static `operate` node for a worker branch (shared by fanout.py
    and flow.py). Passes `actions=True` only when this worker actually got
    the in-process messenger tool bound (team messaging active AND a
    non-CLI worker), so Branch.operate() serializes branch.acts for it."""
    return builder.add_operation(
        "operate",
        branch=branch,
        depends_on=depends_on,
        instruction=instruction,
        context=context,
        **({"actions": True} if messenger_bound else {}),
    )


def _create_fanout_team(
    team_name: str,
    worker_names: list[str],
) -> dict:
    from uuid import uuid4

    team_id = uuid4().hex[:12]
    members = ["orchestrator"] + worker_names
    teams_dir = _team_module.TEAMS_DIR
    teams_dir.mkdir(parents=True, exist_ok=True)
    path = teams_dir / f"{team_id}.json"
    team_dict = {
        "id": team_id,
        "name": team_name,
        "members": members,
        "messages": [],
        "created_at": now_utc().isoformat(),
    }
    with _locked_team(team_id, create_path=path) as data:
        data.update(team_dict)
    return team_dict


def _post_results_to_team(
    team_data: dict,
    worker_results: list[dict],
    worker_names: list[str],
    synthesis_result: dict | None = None,
) -> None:
    """Post worker results + optional synthesis to the team inbox under a lock."""
    from uuid import uuid4

    with _locked_team(team_data["id"]) as data:
        messages = data.setdefault("messages", [])
        for wr, name in zip(worker_results, worker_names, strict=False):
            messages.append(
                {
                    "id": uuid4().hex[:12],
                    "from": name,
                    "from_op": wr.get("id"),
                    "to": ["*"],
                    "content": wr.get("response", "(no response)"),
                    "timestamp": now_utc().isoformat(),
                    "read_by": {},
                }
            )

        if synthesis_result:
            messages.append(
                {
                    "id": uuid4().hex[:12],
                    "from": "orchestrator",
                    "from_op": "synthesis",
                    "to": ["*"],
                    "content": f"[SYNTHESIS]\n{synthesis_result.get('response', '')}",
                    "timestamp": now_utc().isoformat(),
                    "read_by": {},
                }
            )

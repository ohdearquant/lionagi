# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared formatting, team helpers, and worker-prompt fragments.

The orchestrator's plan vocabulary is now ``casts.emission.TaskAssignment``
(see ``lionagi.orchestration.plan``) — there is no bespoke ``AgentRequest``
model. This module keeps only the cross-pattern output/team helpers and the
bare worker prompt.
"""

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

# Backward-compat alias: the old standalone template was
# "You are a specialist..." + TEAM_COORD_SECTION. External callers that
# imported TEAM_WORKER_SYSTEM still get the composed version.
TEAM_WORKER_SYSTEM = BARE_WORKER_SYSTEM + "\n\n" + TEAM_COORD_SECTION


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
    """Append one message per worker result + optional synthesis, under a
    lock so post-flow commits don't race concurrent worker sends.

    Each message carries ``from_op`` so downstream consumers can tell
    which op produced the payload (important when one agent ran several
    ops)."""
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

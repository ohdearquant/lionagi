# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The verb namespace behind the single dispatch tool.

A verb is reachable only because it appears in this file — adding a command
to the CLI does not widen this surface (the projector can read any parser,
but reading is not permission to run). Depending on the verb: spawn/job verbs
run through :mod:`lionagi.mcp.jobs`; long-tail verbs run
``li <path> --machine`` and return its versioned envelope; a command with no
machine envelope is listed as absent, with a reason, rather than reached by
scraping its console output — the absent entries are part of the catalog.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = (
    "Verb",
    "AbsentVerb",
    "VERBS",
    "ABSENT",
    "SYNONYMS",
    "SYNONYM_REMOVAL_DATE",
    "FENCED_PATHS",
    "MAX_OPS",
    "resolve",
    "catalog_names",
)

# Old spellings are removed outright after this date.
SYNONYM_REMOVAL_DATE = "2026-09-30"

# Previous tool-per-operation names, resolved before dispatch and deliberately
# absent from the catalog — for callers already scripted against them, not new ones.
SYNONYMS: Mapping[str, str] = {
    "submit_agent": "agent.submit",
    "submit_flow": "flow.submit",
    "submit_fanout": "fanout.submit",
    "submit_play": "play.submit",
    "job_status": "job.status",
    "job_output": "job.output",
    "job_kill": "job.kill",
    "job_wait": "job.wait",
    "jobs_list": "job.list",
    "server_info": "server.info",
}

# Operations that grant privilege to the caller (every caller is an agent, so
# trusting a plugin/hook bundle would let the grantee also be the granter). No
# verb resolves to these paths and none accepts opaque argv, so unreachable.
FENCED_PATHS = ("state migrate", "plugin trust", "hooks trust")

# Exceeding this is an error naming the count, never a silent truncation.
MAX_OPS = 8

# Shared reason for long-tail commands with no `--machine` seam.
_NO_MACHINE_SEAM = (
    "the CLI path emits no versioned machine result (`li <path> --machine`), so "
    "there is nothing to return that is not scraped console text"
)


@dataclass(frozen=True)
class Verb:
    """One reachable operation.

    ``cli_path`` names the parser the schema is projected from; a verb with
    none carries ``own_schema`` instead. ``admits`` lists passthrough
    parameters (``None`` = all except ``refuses``). ``server_params`` win a
    name collision since the server, not the CLI, implements them.
    """

    name: str
    summary: str
    executor: str
    cli_path: str | None = None
    job_kind: str | None = None
    admits: tuple[str, ...] | None = None
    requires: tuple[str, ...] = ()
    refuses: Mapping[str, str] = field(default_factory=dict)
    server_params: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    own_schema: dict[str, Any] | None = None
    playbook_aware: bool = False


@dataclass(frozen=True)
class AbsentVerb:
    """A verb the catalog names and cannot run, with why.

    ``cli_path`` is stated rather than derived from ``name`` — a verb name
    isn't a dotted spelling of its path (``orchestrate fanout`` registers as
    ``fanout.submit``).
    """

    name: str
    summary: str
    reason: str
    cli_path: str


# ── parameters this server implements rather than passes through ─────────────

_PROMPT = {
    "type": "string",
    "description": (
        "The instruction text. It is written to a file inside the job record and "
        "the run is spawned with an argv list and no shell, so quotes, newlines "
        "and code in it are safe. Give it here or as prompt_file, never both."
    ),
    "x-server-owned": True,
}

_PROMPT_FILE = {
    "type": "string",
    "description": (
        "Absolute path to a file holding the instruction. The server reads it now "
        "and snapshots the text, so editing the file afterwards cannot change what "
        "an already-submitted run executes. '-' is refused: a detached run has no "
        "stdin to read."
    ),
    "x-server-owned": True,
}

_LABEL = {
    "type": "string",
    "description": "Short label recorded on the job and returned by job.list.",
    "x-server-owned": True,
}

_NOTIFY_COMMAND = {
    "type": "string",
    "description": (
        "Delivery command as a JSON argv list, run once this run reaches a "
        "terminal status, overriding the configured default. Placeholders: "
        "{payload}, {status}, {invocation_id}, {target}. The CLI's own --notify "
        "flag is not available: the server wires it to its own terminal hook so "
        "the job record gets a reliable finished status."
    ),
    "x-server-owned": True,
}

_NOTIFY_SEAT = {
    "type": "string",
    "description": "Fills the {target} placeholder in the delivery command.",
    "x-server-owned": True,
}

_NOTIFY_SENDER = {
    "type": "string",
    "description": (
        "Who the terminal notice is from. Fills the {sender} placeholder in the "
        "delivery command and is published to it as LIONAGI_NOTIFY_SENDER. "
        "Without it the notifier reports whatever identity it resolves from the "
        "run's working directory, which is the directory's owner and not the "
        "submitter."
    ),
    "x-server-owned": True,
}

_PLAYBOOK_FINGERPRINT = {
    "type": "string",
    "description": (
        "The playbook fingerprint this call was written against, as returned by "
        "help for this verb with the playbook named. The server resolves the "
        "playbook again at execution and reports in the result whether it changed "
        "since then, so a run against an edited playbook is visible to the caller "
        "rather than silent."
    ),
    "x-server-owned": True,
}

_SPAWN_SERVER_PARAMS: Mapping[str, dict[str, Any]] = {
    "prompt": _PROMPT,
    "prompt_file": _PROMPT_FILE,
    "label": _LABEL,
    "notify_command": _NOTIFY_COMMAND,
    "notify_seat": _NOTIFY_SEAT,
    "notify_sender": _NOTIFY_SENDER,
}

_FLOW_SERVER_PARAMS: Mapping[str, dict[str, Any]] = {
    **_SPAWN_SERVER_PARAMS,
    "playbook_fingerprint": _PLAYBOOK_FINGERPRINT,
}

# Flags a detached run cannot honour; refused by name rather than silently dropped.
_SPAWN_REFUSALS: Mapping[str, str] = {
    "verbose": "streams to a terminal nobody is attached to; read the run with job.output",
    "theme": "colours terminal output; a detached run writes to a plain log file",
    "prompt_flag": "use the prompt parameter, which is snapshotted at submit time",
    "notify": "the server wires the terminal hook; use notify_command and notify_seat",
}

_PURGE_SWEEP_REFUSALS: Mapping[str, str] = {
    "status": "sweeps every row in that status, including rows the caller never read; "
    "purge one id, or run the sweep from a terminal",
    "before": "sweeps by age, so it deletes rows the caller never named and reports a "
    "count for rows that can no longer be inspected; purge one id instead",
}

_AGENT_REFUSALS: Mapping[str, str] = {
    **_SPAWN_REFUSALS,
    "list_profiles": "prints the agent-profile catalog and exits without running anything",
}

_FLOW_REFUSALS: Mapping[str, str] = {
    **_SPAWN_REFUSALS,
    "background": (
        "the run is already detached; re-detaching orphans it and job.status would "
        "lose the run it was given"
    ),
}


# ── schemas for the operations this server implements itself ─────────────────

_RUN_ID = {
    "type": "string",
    "description": (
        "Id of a background run as returned by a submit verb (format "
        "YYYYMMDDTHHMMSS-<6hex>). An id with no job record answers with "
        "known=false rather than failing."
    ),
}


def _own(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


_JOB_STATUS_SCHEMA = _own(
    {
        "run_id": _RUN_ID,
        "detail": {
            "type": "boolean",
            "default": False,
            "description": (
                "When true, adds a 'detail' object carrying the planned "
                "execution graph (nodes, each with role/status/started_at/"
                "duration_s/spawned_by), artifact_contract (declared "
                "artifacts vs satisfied), and stalls (seconds_idle/"
                "last_activity/idle_source per still-running node — "
                "idle_source names where the timestamp came from, and is "
                "'none' with seconds_idle/last_activity absent when no "
                "activity timestamp has been recorded yet, rather than "
                "guessing from the node's start time) — the same state the "
                "Studio Fleet view renders, so a caller can tell which node a "
                "run is stuck on without tailing its console. Costs a StateDB "
                "read; the base payload (default) is unchanged and stays "
                "cheap. A run whose session/graph rows are missing (or the "
                "'studio' extra is not installed) answers with "
                "detail={'detail_unavailable': <reason>} rather than failing."
            ),
        },
    },
    ["run_id"],
)

_JOB_OUTPUT_SCHEMA = _own(
    {
        "run_id": _RUN_ID,
        "tail_chars": {
            "type": "integer",
            "default": 20000,
            "description": (
                "How much of the console log to return, counted from the END. "
                "Raise it when a run's final answer is longer than the tail; the "
                "artifact list comes back in full either way."
            ),
        },
    },
    ["run_id"],
)

_JOB_KILL_SCHEMA = _own({"run_id": _RUN_ID}, ["run_id"])

_JOB_LIST_SCHEMA = _own(
    {
        "limit": {"type": "integer", "default": 50, "description": "How many jobs, newest first."},
        "status": {
            "type": "string",
            "description": (
                "Return only jobs whose recorded status matches this string "
                "exactly. The vocabulary is open — a status the CLI recorded is "
                "passed through verbatim — so filter on a value already seen in a "
                "job record."
            ),
        },
    }
)

_JOB_WAIT_SCHEMA = _own(
    {
        "run_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Run ids to observe. Results come back in this order, one entry "
                "each, and an id with no job record fails only its own entry."
            ),
        },
        "max_wait": {
            "type": "number",
            "default": 60.0,
            "description": (
                "Seconds to keep observing before returning what is known, clamped "
                "to 0-600; 0 takes a single snapshot. Expiry is not an error: the "
                "result carries every observation, so calling again is safe."
            ),
        },
        "poll_interval": {
            "type": "number",
            "default": 1.0,
            "description": (
                "Seconds between status reads, clamped to 0.05-60. The effective "
                "value is echoed back beside the requested one."
            ),
        },
    },
    ["run_ids"],
)

_SERVER_INFO_SCHEMA = _own({})

_ROSTER_CWD = {
    "type": "string",
    "description": (
        "Resolve as a run submitted with this cwd would: the search starts at its "
        "git root, walks up from it, then reaches ~/.lionagi/. Omit it to answer "
        "for the server's own directory, which is what a submit without cwd gets."
    ),
}

_PROFILE_LIST_SCHEMA = _own(
    {
        "cwd": _ROSTER_CWD,
        "names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Answer for these profile names only, matched exactly as the roster "
                "spells them. A name nothing declares is simply absent from the reply, "
                "so asking for three and reading back two says which one is missing. "
                "Omit it for the whole roster."
            ),
        },
        "fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Return only these keys per profile: 'source', 'shadowed', 'ambiguous', "
                "'resolved' for the whole configuration block, or 'resolved.<key>' for "
                "one of its fields ('resolved.model', 'resolved.effort', ...). 'name' is "
                "always returned. An unrecognised field is an error rather than an empty "
                "one. Omit it and every profile comes back as 'name', 'source', "
                "'shadowed' and the whole 'resolved' block, with each placed file given "
                "as a path and a scope and nothing else. Naming a placement field returns "
                "more of it than that, not less: 'source' and 'shadowed' entries then also "
                "carry 'match', which says whether the resolver found the spelling asked "
                "for or substituted the other separator, and 'ambiguous' — the files a "
                "name is refused over rather than ranked between — is reachable only by "
                "asking for it here."
            ),
        },
    }
)

_PROFILE_SHOW_SCHEMA = _own(
    {
        "name": {
            "type": "string",
            "description": (
                "The profile name, as it would be passed to agent.submit. A name "
                "nothing declares is an error listing every name that is available "
                "here, rather than an empty result."
            ),
        },
        "cwd": _ROSTER_CWD,
    },
    ["name"],
)


# ── the registry ─────────────────────────────────────────────────────────────

_REGISTERED: tuple[Verb, ...] = (
    Verb(
        name="agent.submit",
        summary="Run one agent on one task as a detached background run.",
        executor="spawn",
        cli_path="agent",
        job_kind="agent",
        refuses=_AGENT_REFUSALS,
        server_params=_SPAWN_SERVER_PARAMS,
    ),
    Verb(
        name="flow.submit",
        summary="Plan and run a DAG of agents with dependencies, in the background.",
        executor="spawn",
        cli_path="orchestrate flow",
        job_kind="flow",
        refuses=_FLOW_REFUSALS,
        server_params=_FLOW_SERVER_PARAMS,
        playbook_aware=True,
    ),
    Verb(
        name="fanout.submit",
        summary="Run N agents on one task in parallel, optionally synthesized.",
        executor="spawn",
        cli_path="orchestrate fanout",
        job_kind="fanout",
        refuses=_SPAWN_REFUSALS,
        server_params=_SPAWN_SERVER_PARAMS,
    ),
    Verb(
        name="play.submit",
        summary="Run a saved playbook: a flow whose plan and prompt are already written down.",
        executor="spawn",
        cli_path="orchestrate flow",
        job_kind="play",
        # flow.submit is the same command with the playbook optional.
        requires=("playbook",),
        refuses=_FLOW_REFUSALS,
        server_params=_FLOW_SERVER_PARAMS,
        playbook_aware=True,
    ),
    Verb(
        name="job.status",
        summary="Current state of a background run: liveness, job record, CLI manifest.",
        executor="job",
        own_schema=_JOB_STATUS_SCHEMA,
    ),
    Verb(
        name="job.output",
        summary="Console tail and artifact list of a background run.",
        executor="job",
        own_schema=_JOB_OUTPUT_SCHEMA,
    ),
    Verb(
        name="job.list",
        summary=(
            "Recent background jobs, newest first, optionally filtered by status. "
            "Each row carries notify_delivery_state (none/delivered/"
            "delivered_unverified/failed/unknown) — sweeping for terminal notices that "
            "never arrived means reading this column and acting on 'failed' or 'unknown'; no "
            "prior suspicion about any particular run is needed."
        ),
        executor="job",
        own_schema=_JOB_LIST_SCHEMA,
    ),
    Verb(
        name="job.wait",
        summary="Observe runs until terminal or the window closes; partial results, never a bool.",
        executor="job",
        own_schema=_JOB_WAIT_SCHEMA,
    ),
    Verb(
        name="job.kill",
        summary="Stop a background job by signalling the process group this server created.",
        executor="job",
        own_schema=_JOB_KILL_SCHEMA,
    ),
    Verb(
        name="profile.list",
        summary=(
            "Agent profiles agent.submit would accept here, each with the file it "
            "comes from and the configuration it resolves to."
        ),
        executor="roster",
        own_schema=_PROFILE_LIST_SCHEMA,
    ),
    Verb(
        name="profile.show",
        summary=(
            "What one agent profile name resolves to: its winning file, the files "
            "it shadows, and its effective configuration."
        ),
        executor="roster",
        own_schema=_PROFILE_SHOW_SCHEMA,
    ),
    Verb(
        name="server.info",
        summary="Which build is serving: version, contract version, uptime, verb counts.",
        executor="job",
        own_schema=_SERVER_INFO_SCHEMA,
    ),
    Verb(
        name="handshake",
        summary="The machine-result contract version this build speaks.",
        executor="machine",
        cli_path="handshake",
        admits=(),
    ),
    Verb(
        name="doctor",
        summary="Environment checks and which of them failed.",
        executor="machine",
        cli_path="doctor",
        admits=(),
    ),
    Verb(
        name="runs",
        summary="Recorded runs on disk and what each one wrote.",
        executor="machine",
        cli_path="runs",
        admits=("limit",),
    ),
    Verb(
        name="lifecycle",
        summary="What the lifecycle store records about one run: whether every session it "
        "opened has ended, and with what outcome.",
        executor="machine",
        cli_path="lifecycle",
        admits=("run_id",),
    ),
    Verb(
        name="schedule.list",
        summary="Every schedule this Studio holds, with its trigger and enabled state.",
        executor="machine",
        cli_path="schedule list",
        admits=(),
    ),
    Verb(
        name="schedule.get",
        summary="One schedule in full, including its ten most recent runs.",
        executor="machine",
        cli_path="schedule get",
        admits=("id",),
    ),
    Verb(
        name="schedule.status",
        summary="Did it work: the schedule header, its latest run, and that run's verdict.",
        executor="machine",
        cli_path="schedule status",
        admits=("id",),
    ),
    Verb(
        name="schedule.runs",
        summary="Runs of one schedule, newest first, optionally filtered by status.",
        executor="machine",
        cli_path="schedule runs",
        admits=("id", "limit", "status"),
    ),
    Verb(
        name="schedule.limits",
        summary="The global concurrent-fire cap and how many fires are in flight now.",
        executor="machine",
        cli_path="schedule limits",
        admits=(),
    ),
    Verb(
        name="schedule.validate",
        summary="Whether a ScheduleSet file resolves, and what each schedule resolves to.",
        executor="machine",
        cli_path="schedule validate",
        admits=("file",),
    ),
    Verb(
        name="schedule.create",
        summary=(
            "Write a schedule row, and report when its trigger next resolves in the "
            "scheduler's own timezone."
        ),
        executor="machine",
        cli_path="schedule create",
        admits=(
            "name",
            "trigger_type",
            "cron",
            "interval",
            "github_repo",
            "github_filter",
            "threshold_config",
            "poll_interval",
            "action_kind",
            "prompt",
            "model",
            "agent",
            "playbook",
            "flow_yaml",
            "action_command",
            "action_command_args",
            "project",
            "cwd",
            "description",
            "max_runs",
            "once",
            "max_cost_usd",
            "max_tokens",
            "on_success",
            "on_fail",
        ),
    ),
    Verb(
        name="schedule.trigger",
        summary=("Fire a schedule now: reports the run id allocated, never that the run ran."),
        executor="machine",
        cli_path="schedule trigger",
        admits=("id",),
    ),
    Verb(
        name="schedule.enable",
        summary="Let a schedule fire again. Reports the state that was committed.",
        executor="machine",
        cli_path="schedule enable",
        admits=("id",),
    ),
    Verb(
        name="schedule.disable",
        summary="Stop a schedule firing. Reports the state that was committed.",
        executor="machine",
        cli_path="schedule disable",
        admits=("id",),
    ),
    Verb(
        name="schedule.delete",
        summary="Remove a schedule row. Reports the deletion the store confirmed.",
        executor="machine",
        cli_path="schedule delete",
        admits=("id",),
    ),
    Verb(
        name="schedule.export",
        summary="Convert schedule rows into ScheduleSet documents, returned inline.",
        executor="machine",
        cli_path="schedule export",
        admits=("legacy",),
    ),
    # ── observability reads ──────────────────────────────────────────────────
    Verb(
        name="monitor",
        summary="Entities in flight right now: sessions, invocations, shows, plays.",
        executor="machine",
        cli_path="monitor",
        admits=("since", "entity_type", "project"),
        refuses={
            "id": (
                "opens the detail view, whose result is a different shape entirely; "
                "this verb answers with the table"
            ),
            "watch": "redraws a terminal until interrupted",
            "refresh": "paces a redraw this verb does not do",
            "run_ids": "waits for schedule runs to finish; use job.wait for a bounded wait",
            "interval": "paces the wait this verb does not do",
            "follow": "keeps a wait open indefinitely",
            "chain": "shapes the wait this verb does not do",
            "max_wait": "bounds the wait this verb does not do",
        },
    ),
    Verb(
        name="stats.runs",
        summary="Run counts and first/last timestamps, grouped by project/kind/agent/model/status.",
        executor="machine",
        cli_path="stats runs",
        admits=("since", "group_by"),
        refuses={
            "json": (
                "shapes the human printout only; the machine result is already the "
                "envelope and carries the same rows"
            )
        },
    ),
    Verb(
        name="invoke.list",
        summary="Recent skill-level invocations, newest first.",
        executor="machine",
        cli_path="invoke list",
        admits=("skill", "status", "limit"),
    ),
    Verb(
        name="dispatch.ls",
        summary="Rows in the durable dispatch outbox, newest first, without their payloads.",
        executor="machine",
        cli_path="dispatch ls",
        admits=("status", "limit"),
    ),
    Verb(
        name="dispatch.show",
        summary="One dispatch row in full, including its payload and ack token.",
        executor="machine",
        cli_path="dispatch show",
        admits=("id",),
    ),
    Verb(
        name="dispatch.ack",
        summary="Acknowledge a delivered dispatch with its ack token, so the queue "
        "stops redelivering it. A wrong token is refused without echoing the real one.",
        executor="machine",
        cli_path="dispatch ack",
        admits=("id", "token"),
    ),
    Verb(
        name="dispatch.retry",
        summary="Return a failed or dead-lettered dispatch to pending so delivery is "
        "attempted again.",
        executor="machine",
        cli_path="dispatch retry",
        admits=("id",),
    ),
    Verb(
        name="dispatch.purge",
        summary="Delete one dispatch row by id, whatever its status, recording an audit "
        "row. Deleting by --status/--before is refused here: a sweep deletes rows the "
        "caller never named and reports a count for rows that can no longer be inspected.",
        executor="machine",
        cli_path="dispatch purge",
        admits=("id", "dry_run"),
        # Omitting `id` is how a terminal asks for a sweep; sweeps are refused
        # here, so `id` is required rather than merely optional.
        requires=("id",),
        # Refused (not left out of `admits`): these exist and are spelled
        # correctly, so "unknown parameter" would send a caller looking for a typo.
        refuses=_PURGE_SWEEP_REFUSALS,
    ),
    Verb(
        name="state.ls",
        summary="Sessions in the lifecycle store with their branch and message counts.",
        executor="machine",
        cli_path="state ls",
        admits=("limit", "status"),
    ),
    Verb(
        name="state.stats",
        summary="Store and write-ahead-log size, per-table row counts, session status spread.",
        executor="machine",
        cli_path="state stats",
        admits=(),
    ),
    Verb(
        name="team.list",
        summary="Teams on disk with their members and message counts.",
        executor="machine",
        cli_path="team list",
        admits=(),
    ),
    Verb(
        name="team.create",
        summary="Create a new team with named members.",
        executor="machine",
        cli_path="team create",
        admits=("name", "members"),
    ),
    Verb(
        name="team.show",
        summary="Show team details and messages.",
        executor="machine",
        cli_path="team show",
        admits=("team",),
    ),
    Verb(
        name="team.send",
        summary="Send a message to team members.",
        executor="machine",
        cli_path="team send",
        admits=("content", "team", "to", "sender", "from_op", "kind", "artifacts"),
    ),
    Verb(
        name="team.receive",
        summary="Read inbox messages.",
        executor="machine",
        cli_path="team receive",
        admits=("team", "member"),
    ),
    Verb(
        name="plugin.info",
        summary="One plugin's version, trust state, and everything its manifest declares.",
        executor="machine",
        cli_path="plugin info",
        admits=("name",),
    ),
)


def _absent(
    prefix: str, names: tuple[str, ...], summary: str, reason: str = _NO_MACHINE_SEAM
) -> tuple[AbsentVerb, ...]:
    """Absent entries sharing a prefix, a summary and a reason.

    An entry whose ``cli_path`` doesn't follow from ``prefix``+name is written
    out in full below instead of forced through here.
    """
    return tuple(
        AbsentVerb(
            name=f"{prefix}.{n}",
            summary=summary,
            reason=reason,
            cli_path=f"{prefix.replace('.', ' ')} {n}",
        )
        for n in names
    )


# Kept separate from _NO_MACHINE_SEAM: a machine seam would make these
# callable, not safe to call — the reason doesn't expire when the CLI grows one.
_PRIVILEGE = (
    "it widens what this caller can reach — trusting a bundle, enabling a plugin "
    "or importing a hook command grants a right to the agent asking for it, so no "
    "machine seam would make it available here"
)

_STORE_MUTATION = (
    "it rewrites or removes rows in the lifecycle store that every read verb "
    "reports on, and nothing in a machine result tells a caller which of its own "
    "earlier answers the write invalidated"
)

_LONG_RUNNING = (
    "it runs for as long as the process lives rather than returning a result, so "
    "it is a process to start, not a call to make"
)

# Separate from _STORE_MUTATION, whose objection is that a write invalidates
# answers the caller already holds. That one is about consistency and would be
# answerable by a better result shape. This one is not: the content is gone, and
# no reply a machine seam could return would undo the call that asked for it.
_IRREVERSIBLE_LOSS = (
    "it destroys message content permanently — the rows and their references "
    "survive but the bodies do not, and nothing a machine result could say would "
    "give a caller back what its own call removed"
)


ABSENT: tuple[AbsentVerb, ...] = (
    AbsentVerb(
        name="schedule.apply",
        summary="Reconcile a whole ScheduleSet file into the store, atomically.",
        reason=(
            "it writes a whole ScheduleSet atomically and reports a per-row plan; the "
            "plan's shape has not been decided as a machine result yet"
        ),
        cli_path="schedule apply",
    ),
    AbsentVerb(
        name="schedule.run",
        summary="One schedule run.",
        reason=(
            "it reports one schedule run, which schedule.runs already returns in a machine result"
        ),
        cli_path="schedule run",
    ),
    *_absent(
        "state",
        ("doctor",),
        "Read-only inspection of the lifecycle store.",
    ),
    # `plugin trust`/`hooks trust` are deliberately absent from this list too:
    # they're accounted for by FENCED_PATHS, and naming them here would
    # advertise the capability to the caller it's fenced from.
    *_absent(
        "plugin",
        ("enable", "disable"),
        "Plugin bundle enablement.",
        _PRIVILEGE,
    ),
    *_absent(
        "hooks",
        ("import",),
        "Importing hook commands from another tool's config.",
        _PRIVILEGE,
    ),
    *_absent(
        "state",
        ("checkpoint", "prune", "vacuum", "import", "import-teams"),
        "Writes against the lifecycle store.",
        _STORE_MUTATION,
    ),
    *_absent(
        "state",
        ("null-content",),
        "Reclaiming the space held by old message bodies.",
        _IRREVERSIBLE_LOSS,
    ),
    *_absent(
        "studio",
        ("start",),
        "The Studio server.",
        _LONG_RUNNING,
    ),
    AbsentVerb(
        name="mirror",
        summary="Mirror Claude Code sessions into Studio, live.",
        reason=_LONG_RUNNING,
        cli_path="mirror",
    ),
    AbsentVerb(
        name="mcp",
        summary="Serve this surface over stdio.",
        reason=(
            "it is this server: a call to it from here would serve a second copy of "
            "the surface the call arrived on"
        ),
        cli_path="mcp",
    ),
    AbsentVerb(
        name="engine.run",
        summary="Run a domain-specific multi-agent pipeline.",
        reason=(
            "the spawn verbs return a job id because they go through this server's "
            "job records; this path spawns without one, so a caller would get a "
            "result it could not later ask about"
        ),
        cli_path="engine run",
    ),
    *_absent(
        "invoke",
        ("start", "end"),
        "Opening and closing a skill-level orchestration record.",
        (
            "the pair brackets a caller's own work, and this surface has no way to "
            "tell that the caller who opened a record is the one closing it; "
            "invoke.list reads what they wrote"
        ),
    ),
    AbsentVerb(
        name="kill",
        summary="Terminate a run, session, play or show by id.",
        reason=(
            "job.kill covers the jobs this server spawned, where the record carries "
            "the pid to correlate against; this path reaches entities this server "
            "never spawned and holds no identity for"
        ),
        cli_path="kill",
    ),
    *_absent(
        "orchestrate.ctl",
        ("pause", "resume", "msg"),
        "The running-flow control plane.",
        (
            "it steers a flow that is already running, and the effect lands on the "
            "flow rather than in a result; whether the flow honoured it is read from "
            "the flow's own state, not returned here"
        ),
    ),
    AbsentVerb(
        name="orchestrate.ctl.status",
        summary="What a running flow's control plane reports about it.",
        reason=_NO_MACHINE_SEAM,
        cli_path="orchestrate ctl status",
    ),
    AbsentVerb(
        name="orchestrate.ctl.resolve",
        summary="Close a control whose consumer claimed it and never reported back.",
        # Not a missing seam. This command exists because whether a claimed
        # message reached the model is not recoverable from anything the system
        # kept, so the row waits for a person who went and found out. A machine
        # caller has exactly the knowledge the row is missing, which is none, so
        # exposing it here would turn a human's finding into an automated guess
        # -- the same guess the design already refuses to make on a timer.
        reason=(
            "it records what a human established about a delivery the system "
            "cannot determine; a machine caller would be asserting the fact the "
            "row is waiting for rather than reporting one"
        ),
        cli_path="orchestrate ctl resolve",
    ),
    AbsentVerb(
        name="casts",
        summary="The built-in roles and modes an agent can be composed from.",
        reason=_NO_MACHINE_SEAM,
        cli_path="casts",
    ),
    AbsentVerb(
        name="plugin.list",
        summary="Installed plugin bundles and their trust state.",
        # Not a missing seam: the listing garbage-collects trust records as part
        # of listing, deleting the record of any plugin whose bundle directory
        # has gone. That is a write, and what it writes to is the trust surface
        # this surface is fenced away from, so a read-only variant of it would
        # be a second command wearing the name of this one.
        reason=(
            "listing prunes trust records for plugins whose bundle directory is "
            "gone, so it writes to user settings on the trust surface; a listing "
            "that skipped the prune would not be this command"
        ),
        cli_path="plugin list",
    ),
)


VERBS: Mapping[str, Verb] = {verb.name: verb for verb in _REGISTERED}


def resolve(name: Any) -> str:
    """The namespaced verb *name* refers to, following a previous-surface synonym.

    Resolves silently rather than warning — a machine-result reader has no use
    for a deprecation warning.
    """
    if not isinstance(name, str):
        raise TypeError("op must be a string")
    return SYNONYMS.get(name, name)


def catalog_names() -> tuple[str, ...]:
    """Every name the catalog lists, available and absent, in catalog order."""
    return (*(v.name for v in _REGISTERED), *(a.name for a in ABSENT))

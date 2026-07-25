# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The verb namespace behind the single dispatch tool.

The server advertises one tool. Everything it can do is a namespaced verb
registered here, and a verb is reachable only because it appears in this file —
adding a command to the CLI does not widen this surface. Reachability and
authorization are separate on purpose: the projector can read a parser for any
command in the CLI, and reading a parser is not permission to run it.

A verb is registered only when its underlying path can answer a machine caller
honestly. That is three different things depending on the verb:

* the spawn and job verbs run through :mod:`lionagi.mcp.jobs`, which spawns the
  ``li`` CLI as a detached subprocess and keeps its own record of the job;
* a long-tail verb runs ``li <path> --machine`` as a subprocess and returns the
  versioned envelope that command emits;
* a command that only prints prose for a human reader has no such envelope, so
  it is listed here as absent with the reason, rather than reached by scraping
  its console output.

The absent entries are part of the catalog. A caller that asks what exists gets
the name and the reason it cannot be called, which is a different answer from
the verb never having been considered.
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

# The one place the synonym sunset lives, so retiring them is one edit rather
# than a search. After this date the old spellings are removed outright.
SYNONYM_REMOVAL_DATE = "2026-09-30"

# The names the previous tool-per-operation surface used. They are accepted
# inside `ops` and resolved before dispatch, and they are deliberately absent
# from the catalog: they exist for callers already scripted against them, not as
# something new callers should learn.
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

# Operations that grant privilege to the caller. Every caller here is an agent,
# so trusting a plugin or a hook bundle would let the thing being granted a right
# be the thing that grants it, and migrating the store rewrites what every other
# verb reports on. No verb resolves to these paths, and no verb accepts opaque
# argv, so there is no route to them through this surface at all.
FENCED_PATHS = ("state migrate", "plugin trust", "hooks trust")

# How many ops one call may carry. Exceeding it is an error naming the count,
# never a truncation that would run some of a caller's batch and report success.
MAX_OPS = 8

# The reason every long-tail command shares today: `--machine` answers for three
# commands, and the rest print for a human reader. The fix belongs in the CLI —
# the command gains a machine-result seam — not in a parser of its console text,
# which would make its wording an API contract.
_NO_MACHINE_SEAM = (
    "the CLI path emits no versioned machine result (`li <path> --machine`), so "
    "there is nothing to return that is not scraped console text"
)


@dataclass(frozen=True)
class Verb:
    """One reachable operation.

    ``cli_path`` names the parser the schema is projected from; a verb with none
    is owned by this server (it reads the job sidecar) and carries ``own_schema``
    instead. ``admits`` lists the projected parameters the verb passes through —
    ``None`` means all of them except ``refuses``. ``server_params`` are this
    server's own parameters, merged over the projection, and they win a name
    collision because the server, not the CLI, implements them.
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
    """A verb the catalog names and cannot run, with why."""

    name: str
    summary: str
    reason: str


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
}

_FLOW_SERVER_PARAMS: Mapping[str, dict[str, Any]] = {
    **_SPAWN_SERVER_PARAMS,
    "playbook_fingerprint": _PLAYBOOK_FINGERPRINT,
}

# Flags a detached run cannot honour. Each is refused by name with its reason
# rather than accepted and dropped, because a caller who passes one believes it
# took effect.
_SPAWN_REFUSALS: Mapping[str, str] = {
    "verbose": "streams to a terminal nobody is attached to; read the run with job.output",
    "theme": "colours terminal output; a detached run writes to a plain log file",
    "prompt_flag": "use the prompt parameter, which is snapshotted at submit time",
    "notify": "the server wires the terminal hook; use notify_command and notify_seat",
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


_JOB_STATUS_SCHEMA = _own({"run_id": _RUN_ID}, ["run_id"])

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
        # The playbook is the subject of this verb; flow.submit is the same
        # command with the playbook optional.
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
        summary="Recent background jobs, newest first, optionally filtered by status.",
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
        # The machine path for this command takes no arguments; --json shapes the
        # human printout only, so passing it through would be accepted by the
        # parser and then refused by the machine dispatcher.
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
        # `--wait` blocks for as long as the run takes, past any caller's call;
        # `--json` shapes the human printout only. Both are refused by the
        # machine path rather than accepted and ignored.
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
        # `--wait` blocks until the fired run is terminal, which outlives the
        # call; the outcome is read with schedule.status or schedule.runs.
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
        # `--output` and `--report` write files relative to the dispatching
        # process's directory, which is not the caller's; the documents and the
        # conversion report come back in the result instead.
        admits=("legacy",),
    ),
)


def _absent(prefix: str, names: tuple[str, ...], summary: str) -> tuple[AbsentVerb, ...]:
    return tuple(
        AbsentVerb(name=f"{prefix}.{n}", summary=summary, reason=_NO_MACHINE_SEAM) for n in names
    )


ABSENT: tuple[AbsentVerb, ...] = (
    AbsentVerb(
        name="schedule.apply",
        summary="Reconcile a whole ScheduleSet file into the store, atomically.",
        reason=(
            "it writes a whole ScheduleSet atomically and reports a per-row plan; the "
            "plan's shape has not been decided as a machine result yet"
        ),
    ),
    AbsentVerb(
        name="schedule.run",
        summary="One schedule run.",
        reason=(
            "it reports one schedule run, which schedule.runs already returns in a machine result"
        ),
    ),
    *_absent(
        "team",
        ("create", "list", "show", "send", "receive"),
        "Messaging between agents working as a team.",
    ),
    *_absent(
        "state",
        ("ls", "stats", "doctor"),
        "Read-only inspection of the lifecycle store.",
    ),
    *_absent(
        "dispatch",
        ("ls", "show", "ack", "retry", "purge"),
        "The outbound dispatch queue.",
    ),
    AbsentVerb(
        name="monitor",
        summary="What is running right now, across sessions and invocations.",
        reason=_NO_MACHINE_SEAM,
    ),
    AbsentVerb(
        name="stats",
        summary="Aggregate run counts and durations.",
        reason=_NO_MACHINE_SEAM,
    ),
)


VERBS: Mapping[str, Verb] = {verb.name: verb for verb in _REGISTERED}


def resolve(name: Any) -> str:
    """The namespaced verb *name* refers to, following a previous-surface synonym.

    A synonym resolves silently rather than warning: it is accepted precisely so
    a caller already scripted against the old spelling keeps working, and a
    warning in a machine result is noise to the only kind of reader there is.
    """
    if not isinstance(name, str):
        raise TypeError("op must be a string")
    return SYNONYMS.get(name, name)


def catalog_names() -> tuple[str, ...]:
    """Every name the catalog lists, available and absent, in catalog order."""
    return (*(v.name for v in _REGISTERED), *(a.name for a in ABSENT))

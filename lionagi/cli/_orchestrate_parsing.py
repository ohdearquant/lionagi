# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse

from ._providers import add_common_cli_args


def _add_mcp_config_args(parser: argparse.ArgumentParser) -> None:
    """Where this run's workers get their MCP servers from.

    An orchestration builds every worker from the one set its own process
    resolved, so the answer is given once here rather than left to each
    provider CLI to find for itself from a directory the caller never chose.
    """
    parser.add_argument(
        "--mcp-config",
        dest="mcp_config",
        metavar="PATH",
        default=None,
        help=(
            "Read this MCP config and hand its servers to every worker this run "
            "builds. By default the nearest .mcp.json at or above the directory "
            "this command was run in is used, so the workers' tools come from "
            "the submission rather than from --cwd. The file is read once, at "
            "startup."
        ),
    )
    parser.add_argument(
        "--no-mcp-config",
        dest="no_mcp_config",
        action="store_true",
        help=(
            "Hand the workers no MCP servers, and say so deliberately instead "
            "of arriving there by an empty search."
        ),
    )


def add_orchestrate_subparser(
    subparsers: argparse._SubParsersAction,
) -> dict[str, argparse.ArgumentParser]:
    orch = subparsers.add_parser(
        "orchestrate",
        aliases=["o"],
        help="Multi-agent orchestration patterns.",
        description="Orchestrate multiple agents in structured patterns.",
    )
    orch_sub = orch.add_subparsers(dest="orch_command", required=True)

    fo = orch_sub.add_parser(
        "fanout",
        help="Fan-out N workers in parallel, optionally synthesize.",
        description=(
            "Orchestrator decomposes task into N agent requests, "
            "fans out to workers, optionally synthesizes. "
            "Effort can be embedded in model spec: claude/opus-4-7-high. "
            "Flags may appear anywhere relative to the positionals."
        ),
    )
    fo.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Orchestrator model spec (provider/model-effort) followed by the "
            "task prompt. Model is also used as the default worker model "
            "unless --workers is set; omit it when -a/--agent provides one. "
            "A single positional is treated as the prompt."
        ),
    )
    fo.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load orchestrator profile by name. Resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md. "
            "Profile provides system prompt, default model, effort, yolo. "
            "CLI flags and positional model override profile settings."
        ),
    )

    fo.add_argument(
        "-n",
        "--num-workers",
        type=int,
        default=3,
        help=(
            "Maximum assignments the orchestrator generates (default 3). It may generate fewer "
            "if the task does not divide that far, and --workers specs beyond this cap go unused."
        ),
    )
    fo.add_argument(
        "--workers",
        metavar="M1,M2,...",
        default=None,
        help=(
            "Comma-separated worker model specs, assigned round-robin (each may carry an effort "
            "suffix). This is how one fanout mixes cheap and expensive models across workers."
        ),
    )
    fo.add_argument(
        "--pack",
        metavar="PATH",
        default=None,
        help=(
            "Path to a YAML routing pack. Provides per-role model/effort when "
            "--workers is absent. --workers overrides pack routing."
        ),
    )
    fo.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help=(
            "Cap on workers running at the same time. 0, the default, runs them all at once — "
            "lower it to stay under a provider's rate limit."
        ),
    )
    fo.add_argument(
        "--with-synthesis",
        nargs="?",
        const=True,
        default=False,
        metavar="MODEL",
        help=(
            "Run a final pass merging the workers' results into one answer. Bare flag uses the "
            "orchestrator model; with an argument, that model instead."
        ),
    )
    fo.add_argument(
        "--synthesis-prompt",
        default=None,
        help=(
            "What the merged answer should be — a ranked list, a decision, a single patch. "
            "Implies synthesis."
        ),
    )
    fo.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Result format: 'text' (default) for reading, 'json' for a machine-readable run.",
    )
    fo.add_argument(
        "--save",
        metavar="DIR",
        default=None,
        help=(
            "Directory to write each worker's output and the run's artifacts into. Without it "
            "they land in the run's own artifacts directory."
        ),
    )

    fo.add_argument(
        "--team-mode",
        nargs="?",
        const="fanout",
        default=None,
        metavar="NAME",
        help=(
            "Create a persistent team for this fanout. Workers get team context "
            "and results are posted as team messages. Bare flag uses 'fanout' as "
            "team name; with arg uses that name."
        ),
    )

    _add_mcp_config_args(fo)
    add_common_cli_args(fo, resume_on_timeout_supported=False)

    fl = orch_sub.add_parser(
        "flow",
        help="Auto-DAG pipeline: orchestrator plans DAG, engine executes.",
        description=(
            "Orchestrator analyzes the task, composes a DAG of agents "
            "with dependency edges, and executes with automatic "
            "parallelism where dependencies allow. Flags may appear "
            "anywhere relative to the positionals."
        ),
    )
    fl.add_argument(
        "query",
        nargs="*",
        metavar="[MODEL] PROMPT",
        help=(
            "Orchestrator model spec followed by the task prompt. Model is "
            "optional when -a/--agent provides one; a single positional is "
            "treated as the prompt. The prompt itself may instead come from "
            "-f/--file or -p/--playbook."
        ),
    )
    fl.add_argument(
        "-f",
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "Load flow spec from YAML or JSON file. File values serve as "
            "defaults; CLI flags override them. Prompt can come from the "
            "file (prompt: key) or as a positional argument."
        ),
    )
    fl.add_argument(
        "-p",
        "--playbook",
        metavar="NAME",
        default=None,
        help=(
            "Load playbook from ~/.lionagi/playbooks/<NAME>.playbook.yaml. "
            "Playbooks may declare args: schema, artifacts: contracts, "
            "or argument-hint: placeholders for prompt template values."
        ),
    )
    fl.add_argument(
        "-a",
        "--agent",
        metavar="NAME",
        default=None,
        help=(
            "Load orchestrator profile by name — resolves "
            ".lionagi/agents/<NAME>/<NAME>.md first, then .lionagi/agents/<NAME>.md."
        ),
    )
    fl.add_argument(
        "--with-synthesis",
        nargs="?",
        const=True,
        default=False,
        metavar="MODEL",
        help=(
            "Run a final pass merging the DAG's results into one answer. Bare flag uses the "
            "orchestrator model; with an argument, that model instead."
        ),
    )
    fl.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help=(
            "Cap on agents running at the same time within one phase. 0, the default, runs the "
            "whole phase at once — lower it to stay under a provider's rate limit."
        ),
    )
    fl.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Result format: 'text' (default) for reading, 'json' for a machine-readable run.",
    )
    fl.add_argument(
        "--save",
        metavar="DIR",
        default=None,
        help=(
            "Directory to write each agent's output and the run's artifacts into. Required by "
            "--background, which has nowhere else to report."
        ),
    )
    fl.add_argument(
        "--team-mode",
        nargs="?",
        const="flow",
        default=None,
        metavar="NAME",
        help=(
            "Create a FRESH team for this flow (new UUID every invocation). "
            "Bare flag uses 'flow' as the name."
        ),
    )
    fl.add_argument(
        "--team-attach",
        metavar="NAME",
        default=None,
        help=(
            "Attach to a team by NAME — upsert semantics: load existing team "
            "if found (preserving message history), else create fresh. "
            "Mutually exclusive with --team-mode."
        ),
    )
    fl.add_argument(
        "--team-max-rounds",
        type=int,
        default=2,
        metavar="N",
        help=(
            "In team mode (reactive), how many extra wakeup rounds the "
            "coordinator may run after all currently-running workers signal "
            "done, to deliver unread teammate messages before the run "
            "wraps up (default: 2)."
        ),
    )
    fl.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the DAG but don't execute. Shows agents, deps, and model resolution.",
    )
    fl.add_argument(
        "--show-graph",
        action="store_true",
        help="Render DAG as matplotlib visualization. With --save, saves PNG to save dir.",
    )
    fl.add_argument(
        "--background",
        action="store_true",
        help="Run flow in background. Requires --save. Check output in save dir.",
    )
    fl.add_argument(
        "--bare",
        action="store_true",
        help=(
            "Ignore agent profiles — all workers use the CLI model spec. "
            "Roles define behavioral focus only, no profile system prompts."
        ),
    )
    fl.add_argument(
        "--workers",
        metavar="M1,M2,...",
        default=None,
        help=(
            "Comma-separated worker model specs (assignment i uses pool[i %% len]). "
            "Overrides the per-role model while KEEPING each role's profile/system "
            "prompt — unlike --bare, which also drops profiles. Enables mixed-model "
            "flows (cheap roles + expensive roles)."
        ),
    )
    fl.add_argument(
        "--pack",
        metavar="PATH",
        default=None,
        help=(
            "Path to a YAML routing pack. Provides per-role model/effort when "
            "--workers is absent. --workers overrides pack routing."
        ),
    )
    fl.add_argument(
        "--max-ops",
        "--max-agents",
        dest="max_ops",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Cap total ops (nodes in the planned DAG). 0 = no shared ceiling; "
            "reactive spawns are capped at 20. "
            "`--max-agents` is a deprecated alias — prefer `--max-ops`."
        ),
    )
    fl.add_argument(
        "--reactive",
        metavar="MODE",
        default=None,
        help=(
            "Who may grow the live DAG by emitting a SpawnRequest: "
            "'all' (default — every worker), 'off' (flat batch DAG, no spawning), "
            "or a comma-separated list of roles (e.g. 'critic,evaluator') that "
            "alone may spawn. Caps still apply via --max-ops."
        ),
    )
    fl.add_argument(
        "--resume",
        metavar="RUN_OR_SESSION_ID",
        default=None,
        help=(
            "Resume a checkpointed flow from a prior process (by run id, or "
            "any session/invocation/play id backed by one). Replays the "
            "persisted plan verbatim — no planner call, no other flow flags "
            "read (model/prompt/playbook/etc. all come from the checkpoint). "
            "Distinct from `li o ctl resume`, which un-pauses a still-running "
            "session."
        ),
    )
    fl.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "With --resume: run the ops the checkpoint recorded as failed "
            "again, instead of refusing. Their reactive children from the "
            "superseded attempt are dropped so the re-run decides its own. "
            "Without this flag a checkpoint holding any failed op refuses "
            "loudly, naming them, because replaying one as terminal skips it "
            "and everything downstream. Re-running re-executes whatever side "
            "effects the first attempt already had."
        ),
    )
    fl.add_argument(
        "--allow-degraded-context",
        action="store_true",
        help=(
            "With --resume: proceed even when a pending op declared "
            "inherit_context — it runs against an empty branch instead of "
            "its predecessor's conversation history, which resume does not "
            "restore. Without this flag such ops refuse loudly, naming them."
        ),
    )
    _add_mcp_config_args(fl)
    add_common_cli_args(fl, resume_on_timeout_supported=False)

    # `li o ctl status <id>` aliases the same status renderer as `li agent
    # status`; pause/resume/msg queue session_controls rows for the poller.
    ctl = orch_sub.add_parser(
        "ctl",
        help="Control-plane surfaces for a run (status, pause, resume, msg).",
        description="Read-only and control operations addressed by run id.",
    )
    ctl_sub = ctl.add_subparsers(dest="ctl_command", required=True)
    ctl_status = ctl_sub.add_parser(
        "status",
        help="Show lifecycle status for a session, invocation, or play by id.",
        description=(
            "Generic id-addressed status lookup — no agent/play kind scoping, so "
            "<id> is required (no 'latest run' default). Prefer `li agent status` "
            "/ `li play status` when the kind is known."
        ),
    )
    ctl_status.add_argument(
        "id",
        help=(
            "Session, invocation, play, or run id to report on — full, or an unambiguous "
            "prefix. Required here: this command has no 'latest run' default."
        ),
    )
    ctl_status.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print one JSON object with stable keys instead of the human table, for scripting.",
    )

    ctl_pause = ctl_sub.add_parser(
        "pause",
        help="Queue a pause for a running flow.",
        description=(
            "Queues a pause control row; the target flow's control poller applies "
            "it at the next op boundary (idempotent — safe to queue more than once)."
        ),
    )
    ctl_pause.add_argument(
        "id",
        help=(
            "Running flow to pause, by session, invocation, play, or run id (full, an "
            "unambiguous 8-character hex prefix, or an unambiguous run-id prefix "
            "through the T after its date). "
            "In-flight operations are not interrupted."
        ),
    )

    ctl_resume = ctl_sub.add_parser(
        "resume",
        help="Queue a resume for a paused flow.",
        description="Queues a resume control row, releasing a pending pause gate.",
    )
    ctl_resume.add_argument(
        "id",
        help=(
            "Paused flow to release, by session, invocation, play, or run id (full, an "
            "unambiguous 8-character hex prefix, or an unambiguous run-id prefix "
            "through the T after its date)."
        ),
    )

    ctl_msg = ctl_sub.add_parser(
        "msg",
        help="Queue an operator message for a running flow, playbook run, or agent leg.",
        description=(
            "Queues a message control row. A flow or playbook run's control poller "
            "deep-merges it into the workspace context, visible to any op not yet "
            "started (context mode only; --as-op is not supported by this command "
            "yet). A running `li agent` leg instead drains it at its next turn "
            "boundary, landing as a warm continuation turn rather than a context "
            "merge — check `li o ctl status` for which happened."
        ),
    )
    ctl_msg.add_argument(
        "id",
        help=(
            "Running flow, playbook run, or agent leg to message, by session, "
            "invocation, play, or run id (exact, an unambiguous 8-character hex prefix, "
            "or an unambiguous run-id prefix through the T after its date)."
        ),
    )
    ctl_msg.add_argument(
        "text",
        help=(
            "Message merged into the flow's workspace context. Ops already started will not see "
            "it; every op that has not started yet will."
        ),
    )

    ctl_resolve = ctl_sub.add_parser(
        "resolve",
        help="Close a control whose consumer claimed it and never reported back.",
        description=(
            "A `msg` control is delivered at most once, so a consumer claims it "
            "before attempting the delivery. If that consumer dies in between, "
            "nothing left behind can say whether the message reached the model, "
            "and the row is deliberately left standing rather than guessed at. "
            "`li o ctl status` shows who claimed it and when. Use this once you "
            "have found out which it was; the claim is preserved in the record."
        ),
    )
    ctl_resolve.add_argument(
        "control_id",
        help="The control id shown by `li o ctl status` (full, no prefix matching).",
    )
    ctl_resolve.add_argument(
        "--as",
        dest="outcome",
        required=True,
        choices=("applied", "abandoned"),
        help=(
            "What you established actually happened: 'applied' if the message "
            "reached the run, 'abandoned' if it did not and will not."
        ),
    )
    ctl_resolve.add_argument(
        "--by",
        dest="actor",
        default=None,
        help=(
            "Who resolved it, recorded beside the claim. Defaults to the OS "
            "account running the command; pass this when that is not the person "
            "who found out."
        ),
    )

    return {"fanout": fo, "flow": fl, "ctl": ctl}

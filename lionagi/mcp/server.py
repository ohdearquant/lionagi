# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi MCP server.

Every ``submit_*`` tool mirrors a ``li`` command; the only difference from the
CLI is that it returns a run_id immediately instead of blocking until the run
finishes. ``job_status`` / ``job_output`` / ``job_kill`` / ``jobs_list`` operate
on that id by reading the state the CLI already persists, and ``job_wait`` joins
several of them in one bounded call so a caller does not have to poll.

Every response that carries a run's ``status`` carries ``terminal`` and
``outcome`` beside it. The status string is an open vocabulary and is passed
through verbatim; the two derived fields are what a caller branches on, so no
caller has to keep its own copy of lionagi's status names.

Every flag the underlying command accepts is a typed parameter with a
description. There is no free-form pass-through: a capability a caller cannot
see in the tool schema is a capability nobody uses, so the schema carries all of
them. The few flags that cannot work in a background run are refused by name
with the reason, rather than accepted and quietly ignored.
"""

from __future__ import annotations

import functools
import inspect
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field
from pydantic.fields import FieldInfo

from lionagi.cli.machine import CONTRACT_VERSION
from lionagi.version import __version__

from . import jobs
from . import surface as _surface
from .observability import register_observability_tools

# The advertised server name. It moved from "lionagi" to "lion" when this surface
# became the machine contract a peer system drives; the previous name is kept as a
# readable constant because it is what older registrations and logs show. fastmcp
# carries exactly one name in the initialize response and offers no alias for it,
# so nothing here can make a client that asks for "lionagi" by name resolve — but
# nothing needs to: a client addresses this server by its own local config entry
# (the command it launches), not by this string, and the tool names are unchanged.
SERVER_NAME = "lion"
PREVIOUS_SERVER_NAME = "lionagi"

# Stamped at import so server_info can report which build is actually serving.
_STARTED_AT = datetime.now(timezone.utc).isoformat()
_STARTED_MONOTONIC = time.time()

mcp = FastMCP(SERVER_NAME)

# Observing and controlling what already runs — monitor/kill/stats/state/
# dispatch/doctor — as typed tools alongside the submit surface.
register_observability_tools(mcp)


def _resolve_prompt(prompt: str | None, prompt_file: str | None) -> str | None:
    """Return the instruction text, given either inline text or a file holding it.

    The file is read here rather than passed to the CLI as a path. That snapshots
    the text at submit time: ``submit`` writes it into the job directory, so
    editing the file afterwards cannot change what an already-submitted run goes
    on to execute.

    Bad input is rejected before anything is submitted, because otherwise it
    reaches the CLI only after the job record is written and a process is
    spawned, and the caller learns about a typo by going to read the console log
    of a failed run.
    """
    if prompt_file is None:
        return prompt
    if prompt is not None:
        raise ValueError("pass prompt or prompt_file, not both")
    if prompt_file == "-":
        # The CLI reads stdin for "-", but a background job is spawned with stdin
        # at DEVNULL, so it would read an empty prompt and fail.
        raise ValueError("prompt_file cannot be '-': a background run has no stdin")
    path = Path(prompt_file).expanduser()
    if not path.is_absolute():
        # The file is opened here, in the server process, so a relative path would
        # resolve against the server's working directory — NOT the ``cwd`` the
        # caller passes for the run, which is the directory they would reasonably
        # expect. Rather than resolve it against the wrong root, require an
        # absolute path and say so.
        raise ValueError(
            f"prompt_file must be an absolute path, got {prompt_file!r}: it is read by "
            "the server, so a relative path would not resolve against the run's cwd"
        )
    try:
        text = path.read_text()
    except OSError as exc:
        raise ValueError(f"could not read prompt_file {path}: {exc}") from exc
    if not text.strip():
        raise ValueError(f"prompt_file is empty: {path}")
    return text


def _in_process_defaults(fn):
    """Let a Python caller omit parameters whose default carries a description.

    ``Field(...)`` in a default is what puts a parameter's description in the tool
    schema, but the object itself is that description, not the value. Calls that
    arrive through MCP are validated first and never see it; a call made directly
    in this process would, so the declared defaults are filled in here.
    """
    sig = inspect.signature(fn)
    declared = {
        name: param.default.default
        for name, param in sig.parameters.items()
        if isinstance(param.default, FieldInfo)
    }

    @functools.wraps(fn)
    def call(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        for name, default in declared.items():
            if name not in bound.arguments:
                bound.arguments[name] = default
        return fn(*bound.args, **bound.kwargs)

    return call


# --- flags a background run cannot honour --------------------------------------

# A flag in here is refused by name with its reason. None of them are reachable
# through a typed parameter; the guard exists because a caller can still spell a
# flag out — a playbook's declared arguments are passed by name, and a value like
# a model spec is one argv token away from being read as one. Accepting such a
# flag and doing nothing with it is worse than refusing it: the caller believes it
# took effect.
_REJECTED_FLAGS: dict[str, str] = {
    "-h": "prints CLI help; this tool's schema already lists every parameter",
    "--help": "prints CLI help; this tool's schema already lists every parameter",
    "-v": "streams output to a terminal nobody is attached to; read the run with job_output",
    "--verbose": "streams output to a terminal nobody is attached to; read the run with job_output",
    "--theme": "colours terminal output; a background run writes to a plain log file",
    "--list-profiles": "prints the agent-profile catalog and exits without running anything",
    "--background": (
        "the run is already detached; re-detaching orphans it and job_status/job_output "
        "would lose the run they were given"
    ),
    "--prompt": "use the prompt parameter",
    "--prompt-file": "use the prompt_file parameter, which is read and snapshotted at submit time",
    "--notify": "wired by the server so the run records a terminal status; use notify/notify_seat",
}


def _reject_unusable(flags: list[str]) -> None:
    """Refuse a flag that cannot do anything useful in a background run."""
    for token in flags:
        name = token.split("=", 1)[0]
        reason = _REJECTED_FLAGS.get(name)
        if reason is not None:
            raise ValueError(f"{name} cannot be used in a background run: {reason}")


def _playbook_flags(playbook_args: dict[str, Any] | None) -> list[str]:
    """Render a playbook's declared arguments as the flags the CLI accepts.

    A playbook's ``args:`` block names its own arguments; the CLI turns each into
    ``--name-with-dashes``, taking a value unless the declared type is bool, in
    which case its presence is the value. Names are checked here so a typo lands
    as an error instead of an unrecognized flag in a failed run's log.
    """
    out: list[str] = []
    for name, value in (playbook_args or {}).items():
        bare = name.lstrip("-").replace("-", "_")
        if not bare or not bare.replace("_", "").isalnum():
            raise ValueError(
                f"playbook_args key {name!r} is not an argument name: a playbook declares "
                "alphanumeric names like 'target' or 'max_depth'"
            )
        flag = "--" + bare.replace("_", "-")
        if value is None or value is False:
            continue  # a declared bool that is off is simply absent
        if value is True:
            out.append(flag)
        else:
            out += [flag, str(value)]
    return out


def _check_output(output: str | None) -> None:
    if output is not None and output not in ("text", "json"):
        raise ValueError(f"output must be 'text' or 'json', got {output!r}")


# --- parameter descriptions (shared by the submit tools) -----------------------

_DESCRIPTIONS: dict[str, str] = {
    "prompt": (
        "The instruction text. Give it here or as prompt_file, never both. It is written to a "
        "file and the run is spawned with an argv list and no shell, so quotes, newlines and code "
        "in it are safe."
    ),
    "prompt_file": (
        "Absolute path to a file holding the instruction. The file is read now and snapshotted, "
        "so editing it afterwards cannot change what the run executes."
    ),
    "model": (
        "Model spec such as 'claude', 'codex', 'gemini-code', or a full spec like 'claude/opus' "
        "or 'claude/opus-high'. Omit it when a profile, a resume or a playbook already supplies "
        "one."
    ),
    "agent": (
        "Name of an agent profile in .lionagi/agents/ (or a trusted plugin's). The profile "
        "supplies the system prompt and default model, effort, yolo and timeout; parameters set "
        "here win over it."
    ),
    "preset": (
        "Built-in agent configuration to apply. 'coding' is the supported value: it wires the "
        "coding toolkit with path-guard hooks and a coding system prompt, and defaults the "
        "working directory to the invocation directory."
    ),
    "form": (
        "Path to a YAML or JSON work-form spec declaring typed fields and their values. "
        "Validation runs BEFORE any model call, so a malformed input fails the run immediately "
        "instead of after paying for tokens; validated values are injected into the prompt as "
        "structured context."
    ),
    "image": (
        "Image files to attach to the instruction for a multimodal model (.png, .jpg, .jpeg, "
        ".gif, .webp). Each is base64-encoded onto the user message. Use this to ask about a "
        "screenshot, a diagram or a chart."
    ),
    "context_from": (
        "Prior session ids, branch ids, run ids or file paths whose distilled content is injected "
        "above the prompt — how a fresh agent inherits what an earlier one learned. Cannot be "
        "combined with resume or continue_last."
    ),
    "context_budget": "Token budget shared across all context_from refs (default 8000).",
    "resume_branch": "Branch id of a previous agent run to continue, keeping its history.",
    "continue_last": "Continue the most recently used branch instead of starting fresh.",
    "effort": (
        "Reasoning effort, overriding any suffix in the model spec. claude: "
        "low|medium|high|xhigh|max. codex: none|minimal|low|medium|high|xhigh|max|ultra. gemini: "
        "Low|Medium|High."
    ),
    "cwd": "Working directory the run executes in — the repo or worktree it acts on.",
    "timeout": (
        "Hard wall-clock limit in seconds. Also injects a deadline preamble into the prompt so "
        "the agent paces itself rather than being cut off mid-thought."
    ),
    "resume_on_timeout": (
        "If the run hits its timeout, fire exactly one automatic resume asking it to continue and "
        "conclude, and report the combined result."
    ),
    "bypass": (
        "Bypass codex approvals and its sandbox. For environments that are already isolated (a "
        "container, a codespace), where the sandbox only gets in the way."
    ),
    "fast": (
        "Route codex requests through OpenAI's priority service tier for lower latency (requires "
        "account eligibility). Does not change model or reasoning effort, and has no effect on "
        "other providers."
    ),
    "invocation": (
        "Parent invocation id from `li invoke start`. Groups this run with the others in one "
        "orchestration so they report as a single piece of work."
    ),
    "project": "Project name for this run, overriding detection from config or git.",
    "notify": (
        "Delivery command (a JSON argv list) run once this run reaches a terminal status, "
        "overriding the configured default. Placeholders: {payload}, {status}, {invocation_id}, "
        "{target}."
    ),
    "notify_seat": "Fills the {target} placeholder in the delivery command — who to tell.",
    "label": "Short human label for this job, shown by jobs_list.",
    "save": "Directory to write each agent's output and the run's artifacts into.",
    "output": "Result format: 'text' (default) or 'json' for a machine-readable run.",
    "max_concurrent": "Cap on agents running at the same time (default: no cap).",
    "with_synthesis": (
        "Run a final pass that merges the workers' results into one answer. True uses the "
        "orchestrator's model; a model spec uses that model instead."
    ),
    "workers": (
        "Comma-separated worker model specs, assigned round-robin (M1,M2,...). Overrides each "
        "role's model while KEEPING its profile and system prompt — this is how a run mixes cheap "
        "and expensive models per role."
    ),
    "pack": (
        "Path to a YAML routing pack giving per-role model and effort. Used when workers is "
        "absent; workers overrides it."
    ),
    "bare": (
        "Ignore agent profiles entirely: every worker uses the given model spec and roles carry "
        "behavioural focus only, no profile system prompts."
    ),
    "team_mode": (
        "Create a FRESH team for this run so workers can message each other while they work. True "
        "uses the default name; a string names the team."
    ),
    "team_attach": (
        "Join an existing team by name, keeping its message history (created if absent). Mutually "
        "exclusive with team_mode."
    ),
    "team_max_rounds": (
        "In team mode, extra coordinator wakeup rounds after all workers signal done, so unread "
        "teammate messages still get delivered (default 2)."
    ),
    "max_ops": "Cap on total ops (nodes) in the planned DAG. 0 means unlimited.",
    "reactive": (
        "Who may grow the DAG mid-run by requesting a new agent: 'all' (default), 'off' for a "
        "flat batch, or a comma-separated role list (e.g. 'critic,evaluator'). The max_ops cap "
        "still applies."
    ),
    "dry_run": (
        "Plan the DAG and report agents, dependencies and model resolution without executing it — "
        "the cheap way to check what a run would do."
    ),
    "show_graph": (
        "Render the executed DAG to a PNG in the run directory (or the save directory). Requires "
        "matplotlib."
    ),
    "playbook_args": (
        "Values for the arguments a playbook declares in its own args: block, by name (e.g. "
        "{'target': 'docs/adr'}). A declared bool is passed as True to enable it. `li play check "
        "<name>` lists what a playbook accepts."
    ),
    "flow_resume": (
        "Resume a checkpointed flow from a prior run (its run id, or a session, invocation or "
        "play id backed by one). The persisted plan is replayed verbatim: no planner call, and "
        "model/prompt/playbook parameters are ignored."
    ),
    "allow_degraded_context": (
        "With resume: let a pending op that wanted its predecessor's conversation history run "
        "against an empty branch instead. Without it, such ops refuse and name themselves rather "
        "than silently running with less context."
    ),
    "yolo": "Auto-approve the agent's tool calls.",
    "flow_file": (
        "Path to a YAML or JSON flow spec. Its values are defaults; parameters set here override "
        "them, and the prompt may come from the spec's prompt: key."
    ),
    "flow_playbook": (
        "Name of a saved playbook in ~/.lionagi/playbooks/ to plan from. Prefer submit_play, "
        "which is this tool with the playbook as its subject."
    ),
    "num_workers": "How many worker assignments the orchestrator generates (default 3).",
    "synthesis_prompt": (
        "Instruction for the synthesis pass — what the merged answer should be (a ranked list, a "
        "decision, a single patch). Implies synthesis."
    ),
    "play_name": (
        "Playbook to run, from ~/.lionagi/playbooks/<name>.playbook.yaml. `li play list` names "
        "the available ones."
    ),
}


def _desc(key: str) -> Any:
    """A typed parameter that carries its description into the tool schema.

    The description is the whole point: a caller reads the schema to decide what
    this tool can do, so a parameter without one is a capability they will not
    find.
    """
    return Field(description=_DESCRIPTIONS[key])


# --- submit tools (mirror the CLI) --------------------------------------------


@mcp.tool
@_in_process_defaults
def submit_agent(
    prompt: Annotated[str | None, _desc("prompt")] = None,
    prompt_file: Annotated[str | None, _desc("prompt_file")] = None,
    model: Annotated[str | None, _desc("model")] = None,
    agent: Annotated[str | None, _desc("agent")] = None,
    preset: Annotated[str | None, _desc("preset")] = None,
    form: Annotated[str | None, _desc("form")] = None,
    image: Annotated[list[str] | None, _desc("image")] = None,
    effort: Annotated[str | None, _desc("effort")] = None,
    cwd: Annotated[str | None, _desc("cwd")] = None,
    timeout: Annotated[int | None, _desc("timeout")] = None,
    resume: Annotated[str | None, _desc("resume_branch")] = None,
    continue_last: Annotated[bool, _desc("continue_last")] = False,
    context_from: Annotated[list[str] | None, _desc("context_from")] = None,
    context_budget: Annotated[int | None, _desc("context_budget")] = None,
    yolo: Annotated[bool, _desc("yolo")] = False,
    bypass: Annotated[bool, _desc("bypass")] = False,
    fast: Annotated[bool, _desc("fast")] = False,
    invocation: Annotated[str | None, _desc("invocation")] = None,
    project: Annotated[str | None, _desc("project")] = None,
    resume_on_timeout: Annotated[bool, _desc("resume_on_timeout")] = False,
    notify: Annotated[str | None, _desc("notify")] = None,
    notify_seat: Annotated[str | None, _desc("notify_seat")] = None,
    label: Annotated[str | None, _desc("label")] = None,
) -> dict[str, Any]:
    """Run ONE agent on one task, in the background (mirrors ``li agent``).

    Reach for this when a single capable agent can do the whole job: write a
    patch, review a diff, answer a question about a repo, drive a CLI. It returns
    ``{run_id, pid, status}`` immediately — poll ``job_status``, block on
    ``job_wait``, read the final response with ``job_output``, stop it with
    ``job_kill``.

    Siblings, when one agent is the wrong shape:

    * ``submit_fanout`` — N workers on the same task in parallel, optionally
      synthesized into one answer. Use it for breadth: many files, many angles.
    * ``submit_flow`` — an orchestrator plans a DAG of agents with dependencies
      and runs it with automatic parallelism. Use it when later work needs
      earlier results.
    * ``submit_play`` — a saved playbook: a flow whose plan and prompt are already
      written down and reused.

    Beyond a plain prompt this tool can: attach images for a multimodal model
    (``image``), validate structured inputs before spending a token (``form``),
    carry context forward from an earlier run (``context_from``), continue a
    previous conversation (``resume`` / ``continue_last``), and apply a built-in
    configuration such as the coding toolkit (``preset``).

    Purely interactive flags are deliberately absent and are refused if spelled
    out: ``--verbose`` and ``--theme`` (nobody is watching the terminal — use
    ``job_output``), ``--list-profiles`` (it prints a catalog instead of running),
    and ``--help``.
    """
    prompt = _resolve_prompt(prompt, prompt_file)
    flags: list[str] = []
    if model:
        flags.append(model)  # leading positional model spec
    if agent:
        flags += ["-a", agent]
    if preset:
        flags += ["--preset", preset]
    if form:
        flags += ["--form", form]
    for path in image or []:
        flags += ["--image", path]
    if resume:
        flags += ["-r", resume]
    if continue_last:
        flags.append("-c")
    if effort:
        flags += ["--effort", effort]
    if cwd:
        flags += ["--cwd", cwd]
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if invocation:
        flags += ["--invocation", invocation]
    if project:
        flags += ["--project", project]
    if resume_on_timeout:
        flags.append("--resume-on-timeout")
    for ref in context_from or []:
        flags += ["--context-from", ref]
    if context_budget is not None:
        flags += ["--context-budget", str(context_budget)]
    if yolo:
        flags.append("--yolo")
    if bypass:
        flags.append("--bypass")
    if fast:
        flags.append("--fast")
    _reject_unusable(flags)

    return jobs.submit(
        "agent",
        flags,
        prompt=prompt,
        cwd=cwd,
        label=label,
        notify_command=notify,
        notify_target=notify_seat,
    )


@mcp.tool
@_in_process_defaults
def submit_flow(
    prompt: Annotated[str | None, _desc("prompt")] = None,
    prompt_file: Annotated[str | None, _desc("prompt_file")] = None,
    model: Annotated[str | None, _desc("model")] = None,
    agent: Annotated[str | None, _desc("agent")] = None,
    file: Annotated[str | None, _desc("flow_file")] = None,
    playbook: Annotated[str | None, _desc("flow_playbook")] = None,
    playbook_args: Annotated[dict[str, Any] | None, _desc("playbook_args")] = None,
    effort: Annotated[str | None, _desc("effort")] = None,
    cwd: Annotated[str | None, _desc("cwd")] = None,
    timeout: Annotated[int | None, _desc("timeout")] = None,
    max_concurrent: Annotated[int | None, _desc("max_concurrent")] = None,
    max_ops: Annotated[int | None, _desc("max_ops")] = None,
    reactive: Annotated[str | None, _desc("reactive")] = None,
    with_synthesis: Annotated[str | bool | None, _desc("with_synthesis")] = None,
    workers: Annotated[str | None, _desc("workers")] = None,
    pack: Annotated[str | None, _desc("pack")] = None,
    bare: Annotated[bool, _desc("bare")] = False,
    team_mode: Annotated[str | bool | None, _desc("team_mode")] = None,
    team_attach: Annotated[str | None, _desc("team_attach")] = None,
    team_max_rounds: Annotated[int | None, _desc("team_max_rounds")] = None,
    save: Annotated[str | None, _desc("save")] = None,
    output: Annotated[str | None, _desc("output")] = None,
    dry_run: Annotated[bool, _desc("dry_run")] = False,
    show_graph: Annotated[bool, _desc("show_graph")] = False,
    resume: Annotated[str | None, _desc("flow_resume")] = None,
    allow_degraded_context: Annotated[bool, _desc("allow_degraded_context")] = False,
    yolo: Annotated[bool, _desc("yolo")] = False,
    bypass: Annotated[bool, _desc("bypass")] = False,
    fast: Annotated[bool, _desc("fast")] = False,
    invocation: Annotated[str | None, _desc("invocation")] = None,
    project: Annotated[str | None, _desc("project")] = None,
    resume_on_timeout: Annotated[bool, _desc("resume_on_timeout")] = False,
    notify: Annotated[str | None, _desc("notify")] = None,
    notify_seat: Annotated[str | None, _desc("notify_seat")] = None,
    label: Annotated[str | None, _desc("label")] = None,
) -> dict[str, Any]:
    """Plan and run a DAG of agents in the background (mirrors ``li o flow``).

    An orchestrator reads the task, decides which agents are needed and how they
    depend on each other, then executes that graph — everything independent runs
    in parallel, everything dependent waits for what it needs. Reach for this when
    the work has stages: research then design then implement, or gather then
    compare then decide. For a single agent use ``submit_agent``; for N copies of
    the same task with no dependencies between them use ``submit_fanout``.

    The graph can grow while it runs: a worker may request another agent
    (``reactive``), bounded by ``max_ops``. Workers can also talk to each other
    for the duration (``team_mode`` / ``team_attach``). ``dry_run`` shows the plan
    without executing it, and ``resume`` replays a checkpointed run.

    The task may come from ``prompt``, ``prompt_file``, a flow spec (``file``), or
    a playbook (``playbook``, whose declared arguments go in ``playbook_args``).

    ``--background`` is refused: the run is already detached, and re-detaching it
    would leave ``job_status`` and ``job_output`` holding a run id that no longer
    tracks anything. Interactive flags (``--verbose``, ``--theme``, ``--help``)
    are refused for the same class of reason.
    """
    prompt = _resolve_prompt(prompt, prompt_file)
    _check_output(output)
    flags: list[str] = []
    if model:
        flags.append(model)
    if agent:
        flags += ["-a", agent]
    if file:
        flags += ["-f", file]
    if playbook:
        flags += ["-p", playbook]
    if resume:
        flags += ["--resume", resume]
    if allow_degraded_context:
        flags.append("--allow-degraded-context")
    if effort:
        flags += ["--effort", effort]
    if cwd:
        flags += ["--cwd", cwd]
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if max_concurrent is not None:
        flags += ["--max-concurrent", str(max_concurrent)]
    if max_ops is not None:
        flags += ["--max-ops", str(max_ops)]
    if reactive:
        flags += ["--reactive", reactive]
    if workers:
        flags += ["--workers", workers]
    if pack:
        flags += ["--pack", pack]
    if bare:
        flags.append("--bare")
    if team_mode is not None:
        if isinstance(team_mode, str):
            flags += ["--team-mode", team_mode]
        elif team_mode:
            flags.append("--team-mode")
    if team_attach:
        flags += ["--team-attach", team_attach]
    if team_max_rounds is not None:
        flags += ["--team-max-rounds", str(team_max_rounds)]
    if with_synthesis is not None:
        if isinstance(with_synthesis, str):
            flags += ["--with-synthesis", with_synthesis]
        elif with_synthesis:
            flags.append("--with-synthesis")
    if save:
        flags += ["--save", save]
    if output:
        flags += ["--output", output]
    if dry_run:
        flags.append("--dry-run")
    if show_graph:
        flags.append("--show-graph")
    if invocation:
        flags += ["--invocation", invocation]
    if project:
        flags += ["--project", project]
    if resume_on_timeout:
        flags.append("--resume-on-timeout")
    if yolo:
        flags.append("--yolo")
    if bypass:
        flags.append("--bypass")
    if fast:
        flags.append("--fast")
    flags += _playbook_flags(playbook_args)
    _reject_unusable(flags)

    return jobs.submit(
        "flow",
        flags,
        prompt=prompt,
        cwd=cwd,
        label=label,
        notify_command=notify,
        notify_target=notify_seat,
    )


@mcp.tool
@_in_process_defaults
def submit_fanout(
    prompt: Annotated[str | None, _desc("prompt")] = None,
    prompt_file: Annotated[str | None, _desc("prompt_file")] = None,
    model: Annotated[str | None, _desc("model")] = None,
    agent: Annotated[str | None, _desc("agent")] = None,
    num_workers: Annotated[int | None, _desc("num_workers")] = None,
    workers: Annotated[str | None, _desc("workers")] = None,
    pack: Annotated[str | None, _desc("pack")] = None,
    max_concurrent: Annotated[int | None, _desc("max_concurrent")] = None,
    with_synthesis: Annotated[str | bool | None, _desc("with_synthesis")] = None,
    synthesis_prompt: Annotated[str | None, _desc("synthesis_prompt")] = None,
    team_mode: Annotated[str | bool | None, _desc("team_mode")] = None,
    save: Annotated[str | None, _desc("save")] = None,
    output: Annotated[str | None, _desc("output")] = None,
    effort: Annotated[str | None, _desc("effort")] = None,
    cwd: Annotated[str | None, _desc("cwd")] = None,
    timeout: Annotated[int | None, _desc("timeout")] = None,
    yolo: Annotated[bool, _desc("yolo")] = False,
    bypass: Annotated[bool, _desc("bypass")] = False,
    fast: Annotated[bool, _desc("fast")] = False,
    invocation: Annotated[str | None, _desc("invocation")] = None,
    project: Annotated[str | None, _desc("project")] = None,
    resume_on_timeout: Annotated[bool, _desc("resume_on_timeout")] = False,
    notify: Annotated[str | None, _desc("notify")] = None,
    notify_seat: Annotated[str | None, _desc("notify_seat")] = None,
    label: Annotated[str | None, _desc("label")] = None,
) -> dict[str, Any]:
    """Run N agents on one task in parallel (mirrors ``li o fanout``).

    The orchestrator splits the task into ``num_workers`` assignments, runs them
    all at once, and — with ``with_synthesis`` or ``synthesis_prompt`` — merges
    the results into one answer. Reach for this when breadth wins: audit every
    module, review a change from several angles, draft several candidate
    approaches and pick one. There are no dependencies between workers; if later
    work needs earlier results, use ``submit_flow`` instead, and for a single
    agent use ``submit_agent``.

    ``workers`` assigns a different model per worker round-robin, so a wide sweep
    can be cheap models with one expensive synthesizer rather than N expensive
    runs. ``max_concurrent`` throttles how many run at once.

    Interactive flags (``--verbose``, ``--theme``, ``--help``) are refused: no one
    is attached to this run's terminal, so read it back with ``job_output``.
    """
    prompt = _resolve_prompt(prompt, prompt_file)
    _check_output(output)
    flags: list[str] = []
    if model:
        flags.append(model)
    if agent:
        flags += ["-a", agent]
    if num_workers is not None:
        flags += ["--num-workers", str(num_workers)]
    if workers:
        flags += ["--workers", workers]
    if pack:
        flags += ["--pack", pack]
    if max_concurrent is not None:
        flags += ["--max-concurrent", str(max_concurrent)]
    if with_synthesis is not None:
        if isinstance(with_synthesis, str):
            flags += ["--with-synthesis", with_synthesis]
        elif with_synthesis:
            flags.append("--with-synthesis")
    if synthesis_prompt:
        flags += ["--synthesis-prompt", synthesis_prompt]
    if team_mode is not None:
        if isinstance(team_mode, str):
            flags += ["--team-mode", team_mode]
        elif team_mode:
            flags.append("--team-mode")
    if save:
        flags += ["--save", save]
    if output:
        flags += ["--output", output]
    if effort:
        flags += ["--effort", effort]
    if cwd:
        flags += ["--cwd", cwd]
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if invocation:
        flags += ["--invocation", invocation]
    if project:
        flags += ["--project", project]
    if resume_on_timeout:
        flags.append("--resume-on-timeout")
    if yolo:
        flags.append("--yolo")
    if bypass:
        flags.append("--bypass")
    if fast:
        flags.append("--fast")
    _reject_unusable(flags)

    return jobs.submit(
        "fanout",
        flags,
        prompt=prompt,
        cwd=cwd,
        label=label,
        notify_command=notify,
        notify_target=notify_seat,
    )


@mcp.tool
@_in_process_defaults
def submit_play(
    name: Annotated[str | None, _desc("play_name")] = None,
    prompt: Annotated[str | None, _desc("prompt")] = None,
    prompt_file: Annotated[str | None, _desc("prompt_file")] = None,
    playbook_args: Annotated[dict[str, Any] | None, _desc("playbook_args")] = None,
    model: Annotated[str | None, _desc("model")] = None,
    agent: Annotated[str | None, _desc("agent")] = None,
    resume: Annotated[str | None, _desc("flow_resume")] = None,
    allow_degraded_context: Annotated[bool, _desc("allow_degraded_context")] = False,
    team_mode: Annotated[str | bool | None, _desc("team_mode")] = None,
    team_attach: Annotated[str | None, _desc("team_attach")] = None,
    team_max_rounds: Annotated[int | None, _desc("team_max_rounds")] = None,
    max_concurrent: Annotated[int | None, _desc("max_concurrent")] = None,
    max_ops: Annotated[int | None, _desc("max_ops")] = None,
    reactive: Annotated[str | None, _desc("reactive")] = None,
    workers: Annotated[str | None, _desc("workers")] = None,
    pack: Annotated[str | None, _desc("pack")] = None,
    bare: Annotated[bool, _desc("bare")] = False,
    with_synthesis: Annotated[str | bool | None, _desc("with_synthesis")] = None,
    save: Annotated[str | None, _desc("save")] = None,
    output: Annotated[str | None, _desc("output")] = None,
    dry_run: Annotated[bool, _desc("dry_run")] = False,
    show_graph: Annotated[bool, _desc("show_graph")] = False,
    cwd: Annotated[str | None, _desc("cwd")] = None,
    timeout: Annotated[int | None, _desc("timeout")] = None,
    effort: Annotated[str | None, _desc("effort")] = None,
    yolo: Annotated[bool, _desc("yolo")] = False,
    bypass: Annotated[bool, _desc("bypass")] = False,
    fast: Annotated[bool, _desc("fast")] = False,
    invocation: Annotated[str | None, _desc("invocation")] = None,
    project: Annotated[str | None, _desc("project")] = None,
    resume_on_timeout: Annotated[bool, _desc("resume_on_timeout")] = False,
    notify: Annotated[str | None, _desc("notify")] = None,
    notify_seat: Annotated[str | None, _desc("notify_seat")] = None,
    label: Annotated[str | None, _desc("label")] = None,
) -> dict[str, Any]:
    """Run a saved playbook in the background (mirrors ``li play``).

    A playbook is a flow someone already designed and wrote down: the DAG, the
    roles, usually the prompt template. Reach for this when the job is one the
    team does repeatedly — an ADR pass, a review sweep, a release check — instead
    of re-describing it to ``submit_flow`` every time.

    ``prompt`` / ``prompt_file`` fill the playbook's template rather than
    replacing it. A playbook may also declare its own named arguments; pass those
    in ``playbook_args`` (for example ``{"target": "docs/adr"}``), and run
    ``li play check <name>`` to see what a given playbook accepts.

    ``resume`` replays a checkpointed run instead of starting a playbook — pass it
    alone, without ``name``, since the plan, model and prompt all come from the
    checkpoint.

    Every flow control is available here too: ``reactive`` growth bounded by
    ``max_ops``, per-role models via ``workers`` or ``pack``, teams, synthesis,
    and ``dry_run`` to see the plan without running it. ``--background`` and the
    interactive flags are refused, as in ``submit_flow``.
    """
    prompt = _resolve_prompt(prompt, prompt_file)
    _check_output(output)
    if resume:
        if name:
            raise ValueError(
                "pass name or resume, not both: a resumed flow replays its "
                "persisted plan and never reads a playbook"
            )
    elif not name:
        raise ValueError("submit_play needs a playbook name (or resume, to replay a prior run)")

    flags: list[str] = []
    if model:
        flags.append(model)  # leading positional model spec
    if resume:
        flags += ["--resume", resume]
        if allow_degraded_context:
            flags.append("--allow-degraded-context")
    else:
        flags += ["-p", name]
    if agent:
        flags += ["-a", agent]
    if team_mode is not None:
        if isinstance(team_mode, str):
            flags += ["--team-mode", team_mode]
        elif team_mode:
            flags.append("--team-mode")
    if team_attach:
        flags += ["--team-attach", team_attach]
    if team_max_rounds is not None:
        flags += ["--team-max-rounds", str(team_max_rounds)]
    if max_concurrent is not None:
        flags += ["--max-concurrent", str(max_concurrent)]
    if max_ops is not None:
        flags += ["--max-ops", str(max_ops)]
    if reactive:
        flags += ["--reactive", reactive]
    if workers:
        flags += ["--workers", workers]
    if pack:
        flags += ["--pack", pack]
    if bare:
        flags.append("--bare")
    if with_synthesis is not None:
        if isinstance(with_synthesis, str):
            flags += ["--with-synthesis", with_synthesis]
        elif with_synthesis:
            flags.append("--with-synthesis")
    if save:
        flags += ["--save", save]
    if output:
        flags += ["--output", output]
    if dry_run:
        flags.append("--dry-run")
    if show_graph:
        flags.append("--show-graph")
    if effort:
        flags += ["--effort", effort]
    if cwd:
        flags += ["--cwd", cwd]
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if invocation:
        flags += ["--invocation", invocation]
    if project:
        flags += ["--project", project]
    if resume_on_timeout:
        flags.append("--resume-on-timeout")
    if yolo:
        flags.append("--yolo")
    if bypass:
        flags.append("--bypass")
    if fast:
        flags.append("--fast")
    flags += _playbook_flags(playbook_args)
    _reject_unusable(flags)

    return jobs.submit(
        "play",
        flags,
        prompt=prompt,
        cwd=cwd,
        label=label or name,
        notify_command=notify,
        notify_target=notify_seat,
    )


# --- query tools --------------------------------------------------------------


@mcp.tool
def job_status(run_id: str) -> dict[str, Any]:
    """Current state of a background run: liveness, MCP record, CLI manifest."""
    return jobs.status(run_id)


@mcp.tool
def job_output(run_id: str, tail_chars: int = 20000) -> dict[str, Any]:
    """Terminal output of a run: console (an agent's final response) + artifacts."""
    return jobs.output(run_id, tail_chars=tail_chars)


@mcp.tool
def job_kill(run_id: str) -> dict[str, Any]:
    """Stop a running background job (signals its whole process group)."""
    return jobs.kill(run_id)


@mcp.tool
async def job_wait(
    run_ids: list[str],
    max_wait: float = 60.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    """Wait for background runs to finish, bounded — one call instead of a poll loop.

    Returns one entry per requested id, in the order given, each with its
    ``status`` (verbatim, never to be matched against a list), ``terminal``
    ("stop waiting") and ``outcome`` (``succeeded``/``failed``, null while not
    terminal) — plus ``all_terminal``, ``timed_out`` and the ids still
    ``pending``.

    Both numbers are clamped to documented bounds and the effective values come
    back in the result. ``max_wait=0`` takes a single snapshot. A window that
    closes early is not an error: the result still carries everything learned, so
    calling again is safe and costs nothing already known. An unknown id is an
    error on that entry alone and never costs the other ids their observation.

    Waiting only reads. Giving up on the wait — expiry, cancellation, a dropped
    connection — leaves every run running exactly as it was.
    """
    return await jobs.wait(run_ids, max_wait=max_wait, poll_interval=poll_interval)


@mcp.tool
def jobs_list(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """List recent background jobs, newest first; optionally filter by status."""
    return jobs.list_jobs(limit=limit, status_filter=status)


_surface.register(mcp)


@mcp.tool
async def server_info() -> dict[str, Any]:
    """What this server is, and whether it is the build you expect.

    A server process loads its code once, at startup, and serves that code for
    as long as it lives. Updating the installation on disk therefore changes
    nothing about a server already running: it goes on advertising exactly the
    tools it started with. No tool result reveals this, so a caller looking for a
    capability added after its server started sees only that the tool is absent,
    which is indistinguishable from the capability never having been built.

    That is the question this answers, in one call. Check ``tools`` for the
    capability you came for, or ``lionagi_version`` against the version you
    expect. If either is behind, the fix is restarting the server process;
    reinstalling on its own will not do it, and neither will a retry.

    Returns ``lionagi_version``, ``contract_version`` (the machine-result
    envelope version this build speaks), ``started_at``, ``uptime_seconds``,
    ``tools`` (every registered tool name, sorted), ``tool_count`` and ``pid``.
    """
    names = sorted(t.name for t in await mcp.list_tools())
    return {
        "server_name": SERVER_NAME,
        "lionagi_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "started_at": _STARTED_AT,
        "uptime_seconds": round(time.time() - _STARTED_MONOTONIC, 3),
        "tools": names,
        "tool_count": len(names),
        "pid": os.getpid(),
    }


def main() -> None:
    """Console entrypoint: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

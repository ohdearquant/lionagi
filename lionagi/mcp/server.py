# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""lionagi MCP server.

Every ``submit_*`` tool mirrors a ``li`` command; the only difference from the
CLI is that it returns a run_id immediately instead of blocking until the run
finishes. ``job_status`` / ``job_output`` / ``job_kill`` / ``jobs_list`` operate
on that id by reading the state the CLI already persists.

The submit tools deliberately expose the common flags as typed parameters (so
callers do not have to remember CLI syntax) plus an ``extra_args`` escape hatch
for the long tail of flags, so the surface never drifts out of reach of the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from . import jobs

mcp = FastMCP("lionagi")


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


# --- submit tools (mirror the CLI) --------------------------------------------


@mcp.tool
def submit_agent(
    prompt: str | None = None,
    prompt_file: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    effort: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    resume: str | None = None,
    continue_last: bool = False,
    yolo: bool = False,
    bypass: bool = False,
    project: str | None = None,
    resume_on_timeout: bool = False,
    context_from: list[str] | None = None,
    context_budget: int | None = None,
    notify: str | None = None,
    notify_seat: str | None = None,
    label: str | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a one-shot agent run in the background (mirrors ``li agent``).

    Returns ``{run_id, pid, status}`` right away. Poll with ``job_status``, read
    the final response with ``job_output``, stop it with ``job_kill``.

    ``model`` is a spec like ``claude/opus`` or ``codex``; omit it when ``agent``
    (a profile) or ``resume``/``continue_last`` already supplies one. ``agent``
    loads a profile from ``.lionagi/agents/``.

    Give the instruction as either ``prompt`` (text) or ``prompt_file`` (an
    absolute path to a file holding it), never both. Either way the text is
    written to a file before the run and the process is spawned with an argv list
    and no shell, so long text containing quotes or code is safe.

    On terminal the run records its status. If a delivery command is configured
    (``notify`` here, or lionagi's ``notify.on_terminal`` setting) it also sends
    a terminal notice; ``notify_seat`` fills that command's ``{target}``
    placeholder. With nothing configured the run delivers nothing.
    """
    prompt = _resolve_prompt(prompt, prompt_file)
    flags: list[str] = []
    if model:
        flags.append(model)  # leading positional model spec
    if agent:
        flags += ["-a", agent]
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
    if extra_args:
        flags += list(extra_args)

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
def submit_flow(
    prompt: str | None = None,
    prompt_file: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    file: str | None = None,
    playbook: str | None = None,
    effort: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    max_concurrent: int | None = None,
    reactive: str | None = None,
    with_synthesis: str | bool | None = None,
    save: str | None = None,
    yolo: bool = False,
    bypass: bool = False,
    project: str | None = None,
    notify: str | None = None,
    notify_seat: str | None = None,
    label: str | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Submit an orchestrated flow in the background (mirrors ``li o flow``).

    The orchestrator composes a DAG of agents and runs it with automatic
    parallelism. Prompt may come from ``prompt``, ``prompt_file`` (an absolute
    path to a file holding the text), ``file`` (-f), or ``playbook`` (-p).
    ``with_synthesis`` may be a model spec or ``True`` for the default.
    """
    prompt = _resolve_prompt(prompt, prompt_file)
    flags: list[str] = []
    if model:
        flags.append(model)
    if agent:
        flags += ["-a", agent]
    if file:
        flags += ["-f", file]
    if playbook:
        flags += ["-p", playbook]
    if effort:
        flags += ["--effort", effort]
    if cwd:
        flags += ["--cwd", cwd]
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if max_concurrent is not None:
        flags += ["--max-concurrent", str(max_concurrent)]
    if reactive:
        flags += ["--reactive", reactive]
    if with_synthesis is not None:
        if isinstance(with_synthesis, str):
            flags += ["--with-synthesis", with_synthesis]
        elif with_synthesis:
            flags.append("--with-synthesis")
    if save:
        flags += ["--save", save]
    if project:
        flags += ["--project", project]
    if yolo:
        flags.append("--yolo")
    if bypass:
        flags.append("--bypass")
    if extra_args:
        flags += list(extra_args)

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
def submit_fanout(
    prompt: str | None = None,
    prompt_file: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    num_workers: int | None = None,
    workers: str | None = None,
    pack: str | None = None,
    max_concurrent: int | None = None,
    with_synthesis: str | bool | None = None,
    synthesis_prompt: str | None = None,
    save: str | None = None,
    cwd: str | None = None,
    timeout: int | None = None,
    yolo: bool = False,
    bypass: bool = False,
    project: str | None = None,
    notify: str | None = None,
    notify_seat: str | None = None,
    label: str | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Submit a flat parallel fan-out in the background (mirrors ``li o fanout``).

    ``num_workers`` copies of one worker, or ``workers`` as a comma-separated
    list of model specs (M1,M2,...). ``synthesis_prompt`` drives an optional
    synthesis pass over the workers' results. The worker instruction may be given
    inline as ``prompt`` or as ``prompt_file`` (an absolute path).
    """
    prompt = _resolve_prompt(prompt, prompt_file)
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
    if save:
        flags += ["--save", save]
    if cwd:
        flags += ["--cwd", cwd]
    if timeout is not None:
        flags += ["--timeout", str(timeout)]
    if project:
        flags += ["--project", project]
    if yolo:
        flags.append("--yolo")
    if bypass:
        flags.append("--bypass")
    if extra_args:
        flags += list(extra_args)

    return jobs.submit(
        "fanout",
        flags,
        prompt=prompt,
        cwd=cwd,
        label=label,
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
def jobs_list(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """List recent background jobs, newest first; optionally filter by status."""
    return jobs.list_jobs(limit=limit, status_filter=status)


def main() -> None:
    """Console entrypoint: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()

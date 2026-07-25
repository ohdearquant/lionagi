# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""MCP tools for the parts of ``li`` an agent cannot otherwise see.

Scheduling, team messaging, the roles/modes catalog, plugin bundles and
invocation tracking are all reachable from the command line and nowhere else,
so an agent driving lionagi never learns they exist. Each tool here is a typed
schema over one of those capabilities: every flag is a named parameter, and the
return value is data to branch on rather than the text the terminal prints.

Every tool calls the function that sits underneath the CLI's formatting — the
Studio HTTP API for schedules, the locked team-file primitives, the invocation
state-DB helpers — so nothing here parses console output.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

# Schedule action kinds, mirroring the closed set the scheduler accepts.
ActionKind = Literal[
    "agent", "flow", "fanout", "play", "playbook", "flow_yaml", "engine", "command"
]
TriggerType = Literal["cron", "interval", "github", "github_poll"]
RunStatus = Literal["running", "completed", "failed", "timed_out", "aborted", "cancelled"]
MessageKind = Literal["message", "done", "finished", "wakeup"]


# --- Studio API transport ------------------------------------------------------


def _request(path: str, method: str = "GET", body: dict | None = None) -> dict[str, Any]:
    """Call the Studio schedules API and return a structured result.

    The CLI's own helper prints the failure and returns ``None``; a tool caller
    needs the reason instead, so this returns ``{"ok": bool, ...}`` and never
    writes to a stream.
    """
    import urllib.error
    import urllib.request

    from lionagi.studio.cli import _base_url

    base = _base_url()
    url = f"{base}/api/schedules{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return {"ok": True, "status": resp.status, "data": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "error": exc.read().decode(errors="replace"),
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": None,
            "error": (
                f"cannot reach Studio at {base}: {exc}. The schedule API is served by "
                "the Studio daemon — start it with `li studio start`, or set "
                "LIONAGI_STUDIO_URL to a running instance."
            ),
        }


def _err(message: str) -> dict[str, Any]:
    return {"ok": False, "status": None, "error": message}


# --- schedule ------------------------------------------------------------------


def schedule_list() -> dict[str, Any]:
    """List every schedule known to the Studio daemon, with its trigger and state.

    A schedule fires an agent, flow, fan-out or playbook run on its own — on a
    cron expression, a fixed interval, or a GitHub pull-request poll — without
    anyone driving it. Start here to find out what is already automated before
    creating anything.

    Returns ``{"ok": true, "schedules": [...]}`` where each entry carries at
    least ``id``, ``name``, ``enabled``, ``trigger_type`` and, when a run cap is
    set, ``max_runs``/``remaining_runs``. Use the ``id`` with every other
    schedule tool.
    """
    result = _request("/")
    if not result["ok"]:
        return result
    data = result["data"] or {}
    return {"ok": True, "schedules": data.get("schedules", [])}


def schedule_get(schedule_id: str) -> dict[str, Any]:
    """Full stored record of one schedule: trigger, action, budgets and chains.

    This is the whole row — cron expression, resolved timezone, action kind and
    its prompt/model/agent, execution root, budget caps, and the on_success /
    on_fail chain actions. Use it to read back exactly what a schedule will do
    before enabling or triggering it.

    Returns ``{"ok": true, "schedule": {...}}``.

    Args:
        schedule_id: Schedule id, as returned by ``schedule_list``.
    """
    result = _request(f"/{schedule_id}")
    if not result["ok"]:
        return result
    return {"ok": True, "schedule": result["data"]}


def schedule_limits() -> dict[str, Any]:
    """How many scheduled runs may fire at once, and how many are in flight now.

    The cap is global across every schedule, so a fleet of hourly schedules can
    starve each other. Check this when a schedule's runs are being skipped
    rather than failing.

    Returns ``{"ok": true, "max_concurrent": int | None, "in_flight": int}``;
    ``max_concurrent`` is ``None`` when uncapped.
    """
    result = _request("/limits")
    if not result["ok"]:
        return result
    data = result["data"] or {}
    return {
        "ok": True,
        "max_concurrent": data.get("max_scheduled_concurrent") or None,
        "in_flight": data.get("current_inflight", 0),
    }


def _validate_chain(action: dict | None, field: str) -> tuple[str | None, list[str]]:
    """Return (error, warnings) for one chain action, matching the CLI's rules."""
    if action is None:
        return None, []
    from lionagi.studio.cli import _validate_chain_action_node
    from lionagi.studio.scheduler.engine import _MAX_CHAIN_DEPTH

    err = _validate_chain_action_node(action, field, field, 1, _MAX_CHAIN_DEPTH)
    warnings: list[str] = []
    if err is None and field not in action:
        warnings.append(
            f"{field} does not set its own {field!r} key: the chained run is built as "
            f"a shallow merge over this schedule, so it inherits this {field} too and "
            f'fires again at the next chain depth. Set "{field}": null inside it.'
        )
    return err, warnings


def schedule_create(
    name: str,
    trigger_type: TriggerType = "cron",
    cron: str | None = None,
    interval_seconds: int | None = None,
    github_repo: str | None = None,
    github_filter: dict[str, Any] | None = None,
    poll_interval_seconds: int | None = None,
    threshold_config: dict[str, Any] | None = None,
    action_kind: ActionKind = "agent",
    prompt: str | None = None,
    model: str | None = None,
    agent: str | None = None,
    playbook: str | None = None,
    flow_yaml_file: str | None = None,
    action_command: str | None = None,
    action_command_args: list[str] | None = None,
    project: str | None = None,
    cwd: str | None = None,
    description: str | None = None,
    max_runs: int | None = None,
    once: bool = False,
    max_cost_usd: float | None = None,
    max_tokens: int | None = None,
    on_success: dict[str, Any] | None = None,
    on_fail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an agent, flow, fan-out or playbook on a cron schedule, on a fixed
    interval, or when a GitHub pull request matches a filter — and optionally
    chain a follow-up run when it succeeds or fails.

    This is how work happens with nobody watching: a nightly digest, a poll that
    reviews every new PR on a repo, a fifteen-minute health check that pages a
    second agent when it fails. The schedule is stored by the Studio daemon,
    which spawns each run as its own process.

    Requires a running Studio daemon (``li studio start``); use
    ``schedule_create_typed`` instead to write straight to the state database
    with no daemon, or when the cron expression must be pinned to a named
    timezone.

    Returns ``{"ok": true, "id": ..., "name": ..., "warnings": [...]}``.

    Args:
        name: Schedule name, unique per project.
        trigger_type: ``cron`` (an expression), ``interval`` (every N seconds),
            or ``github``/``github_poll`` (poll a repository's pull requests).
        cron: Cron expression, e.g. ``"0 9 * * *"``. It is evaluated in the
            **scheduler daemon's own timezone** — ``LIONAGI_SCHEDULER_TZ`` if
            set, otherwise the daemon host's system local zone, and never
            implicitly UTC. A schedule created here carries no timezone of its
            own, so moving the daemon or changing that variable moves every
            fire time with it.
        interval_seconds: Seconds between fires, for ``trigger_type=interval``.
        github_repo: ``owner/name`` to poll, for the GitHub trigger.
        github_filter: Object narrowing which pull requests fire the trigger,
            e.g. ``{"state": "open", "base": "main"}``.
        poll_interval_seconds: How often to poll GitHub. GitHub trigger only.
        threshold_config: Turn this schedule into a metric alert: its cron or
            interval only *evaluates* the metric on each tick and fires the
            action when the threshold is breached. Shape:
            ``{"metric": "failed_sessions" | "total_cost_usd" | "p95_latency_ms"
            | "github_poll_healthy_age_minutes" | "github_poll_consecutive_401",
            "op": "gt" | "gte", "value": N, "window_minutes": N}``.
            ``window_minutes`` doubles as the re-alert cooldown.
        action_kind: What to run. ``agent`` is one agent turn; ``flow`` and
            ``fanout`` are the orchestrators; ``play``/``playbook`` runs a named
            playbook; ``flow_yaml`` runs a YAML flow spec; ``command`` spawns an
            allow-listed executable directly instead of going through ``li``.
        prompt: Instruction for the run. May contain ``{{var}}`` placeholders
            rendered from the trigger context (e.g. ``{{pr_number}}`` under a
            GitHub trigger).
        model: Model spec for the run, e.g. ``claude/opus`` or ``codex``. For
            ``action_kind="agent"`` set this explicitly whenever the schedule
            also carries extra CLI args — the model is passed as a positional,
            and the scheduler refuses that combination rather than let an
            argument be mistaken for the model. With no model and no extra args
            the agent profile's own model is used.
        agent: Agent profile name to load from ``.lionagi/agents/``.
        playbook: Playbook name, for ``action_kind="play"``/``"playbook"``.
        flow_yaml_file: Path to a YAML flow spec; its **contents** are read and
            stored now, so later edits to the file do not change the schedule.
        action_command: Bare executable name for ``action_kind="command"``. It
            must be in ``LIONAGI_SCHEDULER_COMMAND_ALLOWLIST``; rejected both
            here and again when the schedule fires.
        action_command_args: argv for ``action_command``, each element a
            ``{{var}}`` template rendered from the trigger context.
        project: Project name to run under. When omitted it is auto-detected
            from the current directory.
        cwd: Execution root for the spawned process, snapshotted now as an
            absolute path. When neither ``cwd`` nor ``project`` resolves to a
            directory that still exists at fire time, the run is refused rather
            than silently executed in the daemon's own working directory.
        description: Human-readable note stored with the schedule.
        max_runs: Auto-disable after N fires. Chained ``on_success``/``on_fail``
            runs do not count toward N. Mutually exclusive with ``once``.
        once: Fire exactly once, then auto-disable. Same as ``max_runs=1``.
        max_cost_usd: Auto-disable once cumulative spend reaches this. Checked
            before a fire, so an in-flight run is never interrupted and the
            total may overshoot by one run.
        max_tokens: Auto-disable once cumulative input+output tokens reach N.
            Same pre-fire semantics as ``max_cost_usd``.
        on_success: Follow-up action fired when this run exits 0. Allowed keys:
            ``kind``/``action_kind``, ``model``, ``prompt``, ``agent``,
            ``playbook``, ``on_success``, ``on_fail``. It is **shallow-merged**
            over this schedule, so every key you omit is inherited — including
            ``on_success`` itself, which makes the chain re-fire at each depth.
            Set ``"on_success": null`` inside it to stop the chain.
        on_fail: Follow-up action fired when this run exits non-zero. Same keys
            and the same shallow-merge caveat as ``on_success``.
    """
    if once and max_runs is not None:
        return _err("once and max_runs are mutually exclusive")
    resolved_max_runs = 1 if once else max_runs
    if resolved_max_runs is not None and resolved_max_runs < 1:
        return _err(f"max_runs must be a positive integer, got {resolved_max_runs}")
    if max_cost_usd is not None and (not math.isfinite(max_cost_usd) or max_cost_usd <= 0):
        return _err(f"max_cost_usd must be a finite positive number, got {max_cost_usd}")
    if max_tokens is not None and max_tokens <= 0:
        return _err(f"max_tokens must be a positive integer, got {max_tokens}")
    if poll_interval_seconds is not None and poll_interval_seconds < 1:
        return _err("poll_interval_seconds must be a positive integer")

    from lionagi.studio.scheduler.subprocess import _ALIAS_ACTION_KINDS

    warnings: list[str] = []
    body: dict[str, Any] = {
        "name": name,
        "trigger_type": "github_poll" if trigger_type == "github" else trigger_type,
        "action_kind": _ALIAS_ACTION_KINDS.get(action_kind, action_kind),
    }
    if cron:
        body["cron_expr"] = cron
    if interval_seconds:
        body["interval_sec"] = interval_seconds
    if github_repo:
        body["github_repo"] = github_repo
    if github_filter is not None:
        body["github_filter"] = github_filter
    if threshold_config is not None:
        body["threshold_config"] = threshold_config
    if poll_interval_seconds is not None:
        body["poll_interval_sec"] = poll_interval_seconds
    if resolved_max_runs is not None:
        body["max_runs"] = resolved_max_runs
    if max_cost_usd is not None:
        body["budget_usd"] = max_cost_usd
    if max_tokens is not None:
        body["budget_tokens"] = max_tokens
    if prompt:
        body["action_prompt"] = prompt
    if model:
        body["action_model"] = model
    if agent:
        body["action_agent"] = agent
    if playbook:
        body["action_playbook"] = playbook
    if flow_yaml_file:
        path = Path(flow_yaml_file).expanduser()
        if not path.is_file():
            return _err(f"flow_yaml_file not found: {path}")
        body["action_flow_yaml"] = path.read_text()
    if action_command:
        body["action_command"] = action_command
    if action_command_args is not None:
        body["action_command_args"] = list(action_command_args)
    if cwd:
        resolved_cwd = Path(cwd).expanduser().resolve()
        if not resolved_cwd.is_dir():
            return _err(f"cwd does not exist or is not a directory: {resolved_cwd}")
        body["action_cwd"] = str(resolved_cwd)
    if project:
        body["action_project"] = project
    else:
        detected = _detect_project(Path.cwd())
        if detected:
            body["action_project"] = detected
    if "action_cwd" not in body and "action_project" not in body:
        body["action_cwd"] = str(Path.cwd())
    if description:
        body["description"] = description

    for field, action in (("on_success", on_success), ("on_fail", on_fail)):
        err, chain_warnings = _validate_chain(action, field)
        if err:
            return _err(err)
        warnings += chain_warnings
        if action is not None:
            body[field] = action

    result = _request("/", method="POST", body=body)
    if not result["ok"]:
        return result
    data = result["data"] or {}
    return {
        "ok": True,
        "id": data.get("id"),
        "name": data.get("name"),
        "schedule": data,
        "warnings": warnings,
    }


def _detect_project(root: Path) -> str | None:
    """Best-effort project detection; a failure must never block a create."""
    try:
        from lionagi.cli._project import detect_project
        from lionagi.studio.scheduler.subprocess import _validate_identifier

        detected, _source = detect_project(root)
        if detected:
            _validate_identifier(detected, "action_project")
            return detected
    except Exception:  # noqa: BLE001 - detection is advisory only
        return None
    return None


def schedule_create_typed(
    name: str,
    kind: Literal["agent", "flow", "playbook", "command"],
    profile: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    flow_file: str | None = None,
    playbook: str | None = None,
    playbook_args: dict[str, Any] | None = None,
    executable: str | None = None,
    executable_args: list[str] | None = None,
    at: str | None = None,
    cron: str | None = None,
    timezone: str | None = None,
    every: str | None = None,
    github_repo: str | None = None,
    github_filter: dict[str, Any] | None = None,
    cwd: str | None = None,
    once: bool = False,
    max_runs: int | None = None,
    overlap: Literal["skip", "allow"] = "skip",
    missed_fire: Literal["skip", "run_once"] = "skip",
    budget_usd: float | None = None,
    budget_tokens: int | None = None,
    rate_limit: dict[str, Any] | None = None,
    description: str | None = None,
    disabled: bool = False,
) -> dict[str, Any]:
    """Create a schedule with a timezone-pinned trigger and overlap/rate-limit
    policies, writing straight to the state database.

    Prefer this over ``schedule_create`` when any of these matter: the cron
    expression must resolve in a *named* timezone rather than wherever the
    daemon happens to run; the job should fire once at an exact instant; two
    runs must never overlap; or there is no Studio daemon up to accept an HTTP
    call. The trade-off is a smaller action surface — agent, flow, playbook and
    command only, and no ``on_success``/``on_fail`` chains.

    Exactly one trigger must be given: ``at``, ``cron`` (with ``timezone``),
    ``every``, or ``github_repo``. Returns ``{"ok": true, "id": ...,
    "qualified_name": ...}``.

    Args:
        name: Schedule name. Refused if the resolved project-qualified name is
            already taken, rather than creating a second row with the same name.
        kind: Target type. Decides which of the target arguments below apply.
        profile: Agent profile name. Required for ``kind="agent"``.
        prompt: Instruction text. Required for ``kind="agent"``.
        model: Model override for ``kind="agent"``; defaults to the profile's.
        flow_file: Path to an ``li o flow`` YAML spec. Required for
            ``kind="flow"``. Stored as an absolute host path, so the schedule
            only re-resolves on this machine.
        playbook: Playbook name. Required for ``kind="playbook"``.
        playbook_args: Typed arguments passed to the playbook.
        executable: Program to spawn for ``kind="command"``, subject to the
            scheduler's command allow-list.
        executable_args: argv passed to ``executable``.
        at: Fire once at this absolute instant, RFC 3339 with a mandatory UTC
            offset, e.g. ``2026-07-15T09:00:00-04:00``. Implies one run only.
        cron: Cron expression. Requires ``timezone`` — that is the whole point
            of this tool: the expression is stored with its zone, so it keeps
            meaning the same wall-clock time no matter where the daemon runs.
        timezone: IANA zone name for ``cron``, e.g. ``America/New_York``.
            Daylight-saving transitions are honoured.
        every: Interval as a duration string: ``30s``, ``15m``, ``6h``, ``2d``.
        github_repo: ``owner/name`` to poll for pull-request events.
        github_filter: Object narrowing which pull requests fire the trigger.
        cwd: Execution root for the spawned run; defaults to the current
            directory, resolved to an absolute path and stored now.
        once: Fire once, then auto-disable. Mutually exclusive with ``max_runs``.
        max_runs: Auto-disable after N fires.
        overlap: What to do when the previous run is still going — ``skip`` the
            new fire (default) or ``allow`` them to run concurrently.
        missed_fire: What to do about fires missed while the daemon was down —
            ``skip`` them (default) or ``run_once`` to catch up with a single run.
        budget_usd: Auto-disable once cumulative spend reaches this.
        budget_tokens: Auto-disable once cumulative token usage reaches this.
        rate_limit: Rolling-window fire cap, e.g.
            ``{"max_fires": 3, "window_sec": 3600}``.
        description: Human-readable note stored with the schedule.
        disabled: Create it disabled, so it never fires until enabled.
    """
    from pydantic import ValidationError

    from lionagi.ln.concurrency import run_async
    from lionagi.state.db import StateDB
    from lionagi.studio.services import schedule_declaration as sd

    if once and max_runs is not None:
        return _err("once and max_runs are mutually exclusive")

    triggers = [t for t in (at, cron, every, github_repo) if t is not None]
    if len(triggers) != 1:
        return _err("give exactly one of at / cron / every / github_repo")
    if cron and not timezone:
        return _err("cron requires timezone, e.g. 'America/New_York'")

    try:
        if at:
            trigger = sd.Trigger(at=at)
        elif cron:
            trigger = sd.Trigger(cron=sd.CronTrigger(expression=cron, timezone=timezone))
        elif every:
            trigger = sd.Trigger(every=every)
        else:
            trigger = sd.Trigger(
                github=sd.GithubTriggerSpec(repo=github_repo, filter=github_filter)
            )

        if kind == "agent":
            if not profile:
                return _err("kind='agent' requires profile")
            if not prompt or not prompt.strip():
                return _err("kind='agent' requires a non-empty prompt")
            target = sd.AgentTarget(kind="agent", profile=profile, prompt=prompt, model=model)
        elif kind == "flow":
            if not flow_file:
                return _err("kind='flow' requires flow_file")
            target = sd.FlowTarget(kind="flow", file=flow_file)
        elif kind == "playbook":
            if not playbook:
                return _err("kind='playbook' requires playbook")
            target = sd.PlaybookTarget(kind="playbook", name=playbook, args=playbook_args or {})
        else:
            if not executable:
                return _err("kind='command' requires executable")
            target = sd.CommandTarget(
                kind="command", executable=executable, args=list(executable_args or [])
            )

        budget = None
        if budget_usd is not None or budget_tokens is not None:
            budget = sd.Budget(usd=budget_usd, tokens=budget_tokens)
        policies = sd.Policies(
            missedFire=missed_fire,
            overlap=overlap,
            maxRuns=1 if once else max_runs,
            budget=budget,
            rateLimit=rate_limit,
        )
        root = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
        member = sd.ScheduleMember(
            description=description,
            enabled=not disabled,
            trigger=trigger,
            target=target,
            execution=sd.Execution(cwd=str(root)),
            policies=policies,
        )
    except ValidationError as exc:
        return _err(str(exc))

    project = _detect_project(root)

    async def _run():
        async with StateDB() as db:
            return await sd.create_quick_schedule(db, name, member, cwd=root, project=project)

    try:
        schedule_id, resolved = run_async(_run())
    except sd.ScheduleSetError as exc:
        return _err("; ".join(message for _name, message in exc.errors))

    return {"ok": True, "id": schedule_id, "qualified_name": resolved.qualified_name}


def schedule_set_enabled(schedule_id: str, enabled: bool) -> dict[str, Any]:
    """Turn a schedule on or off without deleting it.

    A disabled schedule keeps its definition and run history but never fires;
    this is the safe way to stop a misbehaving automation. Schedules that hit
    their ``max_runs`` or budget cap disable themselves the same way, so
    re-enabling one that stopped on its own will let it run past that cap.

    Returns ``{"ok": true, "id": ..., "enabled": ...}``.

    Args:
        schedule_id: Schedule to switch.
        enabled: ``true`` to let it fire again, ``false`` to pause it.
    """
    verb = "enable" if enabled else "disable"
    result = _request(f"/{schedule_id}/{verb}", method="POST")
    if not result["ok"]:
        return result
    return {"ok": True, "id": schedule_id, "enabled": enabled}


def schedule_delete(schedule_id: str) -> dict[str, Any]:
    """Delete a schedule permanently. Use ``schedule_set_enabled`` to pause one.

    Returns ``{"ok": true, "id": ..., "deleted": true}``.

    Args:
        schedule_id: Schedule to delete. Its run history goes with it.
    """
    result = _request(f"/{schedule_id}", method="DELETE")
    if not result["ok"]:
        return result
    return {"ok": True, "id": schedule_id, "deleted": True}


def schedule_trigger(schedule_id: str, wait: bool = False) -> dict[str, Any]:
    """Fire a schedule right now, without waiting for its next scheduled time.

    Use it to prove a schedule actually works after creating it, or to force an
    off-cycle run. The fire is real: it spends budget and counts toward the
    schedule's run cap exactly as a scheduled fire would.

    Args:
        schedule_id: Schedule to fire.
        wait: Block until the run reaches a terminal status and report its
            outcome. Off by default because a scheduled run can be long; when
            off, poll with ``schedule_run`` on the returned ``run_id``.

    Returns ``{"ok": true, "id": ..., "run_id": ...}``, plus ``status`` and
    ``outcome`` when ``wait`` is set.
    """
    result = _request(f"/{schedule_id}/trigger", method="POST")
    if not result["ok"]:
        return result
    data = result["data"] or {}
    run_id = data.get("run_id")
    out: dict[str, Any] = {"ok": True, "id": schedule_id, "run_id": run_id}
    if not wait or not run_id:
        return out
    run = _await_run(run_id)
    if not run["ok"]:
        return run
    out["status"] = run["run"].get("status")
    out["outcome"] = run["run"].get("outcome")
    out["run"] = run["run"]
    return out


def _await_run(run_id: str) -> dict[str, Any]:
    """Poll one occurrence until it reaches a terminal status."""
    import time

    from lionagi.state.db import SCHEDULE_RUN_TERMINAL_STATUSES
    from lionagi.studio.cli import (
        _TRIGGER_WAIT_GRACE_POLL_SECONDS,
        _TRIGGER_WAIT_GRACE_SECONDS,
        _TRIGGER_WAIT_MAX_SECONDS,
        _TRIGGER_WAIT_POLL_SECONDS,
    )

    # The run id comes back before its row is durably written, so a lookup
    # straight after a trigger can miss it; retry within a bounded grace period.
    grace_deadline = time.monotonic() + _TRIGGER_WAIT_GRACE_SECONDS
    result = _request(f"/runs/{run_id}")
    while not result["ok"] and time.monotonic() < grace_deadline:
        time.sleep(_TRIGGER_WAIT_GRACE_POLL_SECONDS)
        result = _request(f"/runs/{run_id}")
    if not result["ok"]:
        return result

    deadline = time.monotonic() + _TRIGGER_WAIT_MAX_SECONDS
    run = result["data"] or {}
    while run.get("status") not in SCHEDULE_RUN_TERMINAL_STATUSES and time.monotonic() < deadline:
        time.sleep(_TRIGGER_WAIT_POLL_SECONDS)
        result = _request(f"/runs/{run_id}")
        if not result["ok"]:
            return result
        run = result["data"] or {}
    return {"ok": True, "run": run}


def schedule_runs(
    schedule_id: str,
    limit: int = 20,
    status: list[RunStatus] | None = None,
) -> dict[str, Any]:
    """Run history for one schedule: what fired, when, and how it ended.

    This is where a schedule that "does nothing" explains itself — runs that
    were skipped for overlap, failed, or timed out all appear here with an
    outcome code and the artifacts they produced.

    Args:
        schedule_id: Schedule whose runs to list.
        limit: Maximum runs to return, 1-200, newest first.
        status: Only return runs in these statuses.

    Returns ``{"ok": true, "runs": [...]}``; each run carries ``id``,
    ``status``, ``fired_at``, ``duration_ms``, ``outcome``, ``invocation_id``
    and ``artifacts``.
    """
    if not 1 <= limit <= 200:
        return _err(f"limit must be between 1 and 200, got {limit}")
    path = f"/{schedule_id}/runs?limit={limit}"
    for value in status or ():
        path += f"&status={value}"
    result = _request(path)
    if not result["ok"]:
        return result
    return {"ok": True, "runs": (result["data"] or {}).get("runs", [])}


def schedule_run(run_id: str) -> dict[str, Any]:
    """One scheduled run in full: status, timing, outcome, sessions, artifacts.

    Returns ``{"ok": true, "run": {...}}``.

    Args:
        run_id: Occurrence id, as returned by ``schedule_trigger`` or listed by
            ``schedule_runs``. This is not a schedule id.
    """
    result = _request(f"/runs/{run_id}")
    if not result["ok"]:
        return result
    return {"ok": True, "run": result["data"]}


def schedule_status(schedule_id: str, wait: bool = False) -> dict[str, Any]:
    """ "Did it work?" — one schedule's next fire time and its latest run's outcome.

    The quickest check after creating or triggering a schedule: it pairs the
    schedule's enabled state and next fire time with the most recent run's
    status, outcome and artifacts, so a broken automation shows up in one call.

    Args:
        schedule_id: Schedule to summarize.
        wait: Wait for the latest run to finish first, if one is in flight.

    Returns ``{"ok": true, "schedule": {...}, "latest_run": {...} | None,
    "exit_code": int}``. ``exit_code`` is 0 when the last run succeeded, 2 when
    there is nothing conclusive to report yet.
    """
    result = _request(f"/{schedule_id}/status")
    if wait:
        import time

        from lionagi.studio.cli import (
            _TRIGGER_WAIT_MAX_SECONDS,
            _TRIGGER_WAIT_POLL_SECONDS,
            _status_still_running,
        )

        deadline = time.monotonic() + _TRIGGER_WAIT_MAX_SECONDS
        while (
            result["ok"] and _status_still_running(result["data"]) and time.monotonic() < deadline
        ):
            time.sleep(_TRIGGER_WAIT_POLL_SECONDS)
            result = _request(f"/{schedule_id}/status")
    if not result["ok"]:
        return result
    data = result["data"] or {}
    return {
        "ok": True,
        "schedule": data.get("schedule") or {},
        "latest_run": data.get("latest_run"),
        "exit_code": data.get("exit_code", 2),
    }


# --- team ----------------------------------------------------------------------


def team_create(name: str, members: list[str]) -> dict[str, Any]:
    """Create a named team so separately-running agents can message each other.

    A team is a shared inbox file: any agent that knows the team id can send to
    a member or broadcast to all, and each member reads its own unread mail.
    That is the mechanism for handing work between agents that do not share a
    process — a reviewer telling an implementer what to fix, a worker reporting
    to a coordinator.

    Returns ``{"ok": true, "id": ..., "name": ..., "members": [...], "file": ...}``.

    Args:
        name: Team name. Other tools accept it in place of the generated id.
        members: Member names. Only these names can send or receive as
            themselves; a message to anyone else still sends, with a warning.
    """
    from uuid import uuid4

    from lionagi.cli import team as team_mod
    from lionagi.ln._utils import now_utc

    names = [m.strip() for m in members if m.strip()]
    if not names:
        return _err("members requires at least one name")
    team_id = uuid4().hex[:12]
    path = team_mod._teams_dir() / f"{team_id}.json"
    with team_mod._locked_team(team_id, create_path=path) as data:
        data.update(
            {
                "id": team_id,
                "name": name,
                "members": names,
                "messages": [],
                "created_at": now_utc().isoformat(),
            }
        )
    return {"ok": True, "id": team_id, "name": name, "members": names, "file": str(path)}


def team_list() -> dict[str, Any]:
    """List every team, newest first, with its members and message count.

    Use it to find the team id another agent was told to use. Returns
    ``{"ok": true, "teams": [{"id", "name", "members", "message_count"}]}``.
    """
    from lionagi.cli import team as team_mod

    files = sorted(
        team_mod._teams_dir().glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    teams = []
    for path in files:
        data = team_mod.read_team_json(path)
        if data is None:
            continue  # unreadable or corrupt: skip rather than fail the listing
        teams.append(
            {
                "id": data.get("id"),
                "name": data.get("name"),
                "members": data.get("members", []),
                "message_count": len(data.get("messages", [])),
            }
        )
    return {"ok": True, "teams": teams}


def team_show(team: str) -> dict[str, Any]:
    """Read a team's whole message history without consuming anything.

    Unlike ``team_receive`` this marks nothing as read, so it is the safe way to
    audit a conversation or catch up on what other members said.

    Returns ``{"ok": true, "team": {...}, "messages": [...]}``.

    Args:
        team: Team id, name, or an unambiguous id prefix.
    """
    from lionagi.cli import team as team_mod

    try:
        data = team_mod._load_team(team)
    except (FileNotFoundError, team_mod.AmbiguousIdError) as exc:
        return _err(str(exc))
    return {
        "ok": True,
        "team": {
            "id": data.get("id"),
            "name": data.get("name"),
            "members": data.get("members", []),
            "created_at": data.get("created_at"),
        },
        "messages": data.get("messages", []),
    }


def team_send(
    team: str,
    to: str,
    content: str,
    sender: str | None = None,
    kind: MessageKind = "message",
    from_op: str | None = None,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """Send a message to one team member, several, or the whole team.

    This is how a running agent tells another agent something — a result, a
    request, a hand-off. The write takes an exclusive file lock, so parallel
    senders serialize instead of overwriting each other.

    Args:
        team: Team id, name, or an unambiguous id prefix.
        to: ``"all"`` to broadcast, or comma-separated member names.
        content: Message body.
        sender: Name to send as. Defaults to ``_cli``. A name that is not a
            member still sends, and is reported back in ``warnings``.
        kind: ``message`` is ordinary content. The other three are lifecycle
            signals a worker posts about itself and a coordinator reads:
            ``done`` (finished this turn, may be woken again), ``finished``
            (permanently done, never revived), ``wakeup`` (make the addressed
            member active again).
        from_op: Operation id this message belongs to, when the sending agent
            runs several operations on one branch.
        artifacts: Paths to attach, typically alongside ``kind="done"``.

    Returns ``{"ok": true, "message_id": ..., "recipients": [...],
    "warnings": [...]}``.
    """
    from lionagi.cli import team as team_mod

    warnings: list[str] = []
    try:
        with team_mod._locked_team(team) as data:
            if not data:
                return _err(f"team {team!r} is empty or missing")
            members = data.get("members", [])
            who = sender or "_cli"
            if who != "_cli" and who not in members:
                warnings.append(f"{who!r} is not a member of this team")
            if to.lower() == "all":
                recipients = ["*"]
            else:
                recipients = [r.strip() for r in to.split(",") if r.strip()]
                warnings += [
                    f"{r!r} is not a member of this team" for r in recipients if r not in members
                ]
            msg = team_mod._build_message(
                who,
                recipients,
                content,
                kind=kind,
                from_op=from_op,
                artifacts=list(artifacts) if artifacts else None,
            )
            data.setdefault("messages", []).append(msg)
            team_name = data.get("name", team)
    except (FileNotFoundError, team_mod.AmbiguousIdError) as exc:
        return _err(str(exc))
    return {
        "ok": True,
        "message_id": msg["id"],
        "team": team_name,
        "recipients": recipients,
        "kind": kind,
        "warnings": warnings,
    }


def team_receive(team: str, member: str | None = None) -> dict[str, Any]:
    """Read and consume this member's unread team mail.

    Call it when you are acting as a named member and want the messages other
    agents left for you. Everything returned is marked read for that member, so
    a second call returns only what arrived since — use ``team_show`` to look
    without consuming.

    Args:
        team: Team id, name, or an unambiguous id prefix.
        member: Read as this member; unread state is tracked per member.
            Omitting it returns every message and marks nothing as read.

    Returns ``{"ok": true, "messages": [...], "count": N, "warnings": [...]}``.
    """
    from lionagi.cli import team as team_mod
    from lionagi.ln._utils import now_utc

    warnings: list[str] = []
    try:
        with team_mod._locked_team(team) as data:
            if not data:
                return _err(f"team {team!r} is empty or missing")
            if member and member not in data.get("members", []):
                warnings.append(f"{member!r} is not a member of this team")
            unread: list[dict] = []
            for msg in data.get("messages", []):
                read_by = team_mod._read_by_map(msg.get("read_by"))
                if member and member in read_by:
                    continue
                targets = msg["to"]
                if targets == ["*"] or (member and member in targets) or not member:
                    unread.append(msg)
            now = now_utc().isoformat()
            for msg in unread:
                read_by = team_mod._read_by_map(msg.get("read_by"))
                if member and member not in read_by:
                    read_by[member] = now
                    msg["read_by"] = read_by
    except (FileNotFoundError, team_mod.AmbiguousIdError) as exc:
        return _err(str(exc))
    return {"ok": True, "messages": unread, "count": len(unread), "warnings": warnings}


# --- casts ---------------------------------------------------------------------


def casts_list(kind: Literal["roles", "modes"] = "roles") -> dict[str, Any]:
    """List the built-in agent roles, or the behavior modes they can be run in.

    A role is a ready-made agent persona (its system prompt and the structured
    output it emits); a mode is a behavior overlay applied on top of one. These
    are what an agent profile composes from, so this is the catalog to read
    before naming a role or mode anywhere else.

    Returns ``{"ok": true, "kind": ..., "entries": [{"name", "description"}]}``.

    Args:
        kind: ``roles`` for the personas, ``modes`` for the behavior overlays.
    """
    from lionagi.casts.catalog import build_catalog

    catalog = build_catalog()
    entries = [{"name": e["name"], "description": e["description"]} for e in catalog[kind]]
    return {"ok": True, "kind": kind, "entries": entries}


def casts_get(name: str) -> dict[str, Any]:
    """Full definition of one role or mode, including its prompt body.

    Looks the name up in both catalogs and returns whichever matches, so you do
    not have to know in advance whether ``name`` is a role or a mode. Returns
    ``{"ok": true, "kind": "role" | "mode", "entry": {...}}``; a role entry
    carries ``description``/``emits``/``body``, a mode entry carries
    ``description``/``conflicts_with``/``behaviors``.

    Args:
        name: Role or mode name, as listed by ``casts_list``.
    """
    from lionagi.casts.catalog import build_catalog

    catalog = build_catalog()
    role = next((r for r in catalog["roles"] if r["name"] == name), None)
    if role is not None:
        return {"ok": True, "kind": "role", "entry": role}
    mode = next((m for m in catalog["modes"] if m["name"] == name), None)
    if mode is not None:
        return {"ok": True, "kind": "mode", "entry": mode}
    return _err(f"unknown role or mode: {name!r}")


# --- plugin --------------------------------------------------------------------


def plugin_list() -> dict[str, Any]:
    """List installed plugin bundles and whether each one is actually active.

    A plugin is a ``.lionagi/plugins/<name>/`` bundle that can add tools, hooks,
    agent profiles, playbooks and providers — but it stays completely inert
    until trusted. If a plugin's tools are missing, its ``state`` here says why:
    ``active``, ``disabled``, ``untrusted``, ``changed`` (edited since it was
    trusted), ``incompatible``, ``collision`` or ``invalid``.

    Also prunes trust records whose bundle directory is gone; those names come
    back in ``pruned``. Returns ``{"ok": true, "plugins": [...], "pruned": [...]}``.
    """
    from lionagi import plugins as plugins_mod
    from lionagi.plugins.discovery import discover_plugins
    from lionagi.plugins.trust import gc_trust_records

    pruned = list(gc_trust_records(discover_plugins()))
    plugins_mod.PluginRegistry.reset()
    records = plugins_mod.PluginRegistry.list_plugins()
    return {
        "ok": True,
        "pruned": pruned,
        "plugins": [
            {
                "name": r.name,
                "version": r.version,
                "state": r.state.value,
                "error": r.error,
            }
            for r in sorted(records, key=lambda r: r.name)
        ],
    }


def plugin_info(name: str) -> dict[str, Any]:
    """Everything one plugin declares: its tools, hooks, agents and providers.

    Read this before trusting a plugin — it is the full inventory of what the
    bundle would be allowed to load, including the argv of any external hook
    command it registers.

    Returns ``{"ok": true, "plugin": {...}, "disclosure": {...} | None}``; the
    disclosure is absent when the bundle has no valid manifest.

    Args:
        name: Plugin name, as listed by ``plugin_list``.
    """
    from lionagi import plugins as plugins_mod
    from lionagi.plugins.trust import build_trust_disclosure

    plugins_mod.PluginRegistry.reset()
    record = plugins_mod.PluginRegistry.get(name)
    if record is None:
        return _err(f"unknown plugin: {name!r}")
    info = {
        "name": record.name,
        "version": record.version,
        "state": record.state.value,
        "bundle_dir": str(record.bundle_dir),
        "error": record.error,
    }
    if record.manifest is None:
        return {"ok": True, "plugin": info, "disclosure": None}
    return {"ok": True, "plugin": info, "disclosure": build_trust_disclosure(record)}


def plugin_trust(name: str) -> dict[str, Any]:
    """Approve a plugin so its tools, hooks and profiles actually load.

    Trust is content-pinned: a sha256 of every declared file is recorded, and
    any later edit flips the plugin back to ``changed`` until it is trusted
    again. This grants a bundle the right to run code in your process, so read
    ``plugin_info`` first — the disclosure it returns is exactly what is being
    approved, and is echoed back here as ``disclosure``.

    Returns ``{"ok": true, "name": ..., "trusted": true, "disclosure": {...}}``.

    Args:
        name: Plugin to trust. Its bundle is re-scanned first, so what is
            pinned is what is on disk right now.
    """
    from lionagi import plugins as plugins_mod
    from lionagi.plugins.discovery import discover_plugins
    from lionagi.plugins.trust import build_trust_disclosure, trust_plugin

    plugins_mod.PluginRegistry.reset()
    record = plugins_mod.PluginRegistry.get(name)
    if record is None or record.manifest is None:
        return _err(f"unknown or invalid plugin: {name!r}")
    # Trust needs the freshly-discovered bundle (declared files + manifest),
    # not the registry's summary record.
    discovered = next(
        (d for d in discover_plugins() if d.manifest is not None and d.manifest.name == name),
        None,
    )
    if discovered is None:
        return _err(f"plugin {name!r} disappeared during trust; re-run plugin_list")
    disclosure = build_trust_disclosure(discovered)
    try:
        trust_plugin(discovered)
    except FileNotFoundError as exc:
        return _err(str(exc))
    plugins_mod.PluginRegistry.reset()
    return {"ok": True, "name": name, "trusted": True, "disclosure": disclosure}


def plugin_set_enabled(name: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a trusted plugin without touching its files.

    This flips a flag in the user settings file; the bundle itself stays
    pristine and its recorded trust survives, so re-enabling needs no
    re-approval. Use it to isolate which plugin is causing a problem.

    Returns ``{"ok": true, "name": ..., "enabled": ...}``.

    Args:
        name: Plugin to switch.
        enabled: ``true`` to load it again, ``false`` to leave it installed
            and trusted but inert.
    """
    from lionagi import plugins as plugins_mod
    from lionagi.plugins._user_settings import locked_user_settings

    plugins_mod.PluginRegistry.reset()
    if plugins_mod.PluginRegistry.get(name) is None:
        return _err(f"unknown plugin: {name!r}")
    with locked_user_settings() as settings:
        block = settings.setdefault("plugins", {})
        if not isinstance(block, dict):
            block = {}
            settings["plugins"] = block
        entry = block.setdefault(name, {})
        if not isinstance(entry, dict):
            entry = {}
            block[name] = entry
        entry["enabled"] = enabled
    plugins_mod.PluginRegistry.reset()
    return {"ok": True, "name": name, "enabled": enabled}


# --- invoke --------------------------------------------------------------------


def invoke_start(
    skill: str,
    plugin: str | None = None,
    prompt: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open an invocation so every run you spawn next is grouped under one record.

    Without this, a multi-step orchestration shows up as a scattering of
    unrelated sessions. Open an invocation first, pass its id to the runs you
    spawn, and the whole piece of work becomes one auditable unit with a
    session count, a status and a duration.

    Args:
        skill: Name of the orchestration, e.g. ``show`` or ``codex-pr-review``.
        plugin: Plugin packaging that skill, when it came from one.
        prompt: The request that triggered the work, stored as free text.
        metadata: Arbitrary JSON to attach — a plan, a target PR, review rounds.

    Returns ``{"ok": true, "invocation_id": ...}``. Close it with ``invoke_end``;
    an invocation left open stays ``running`` forever.
    """
    from lionagi.cli import invoke as invoke_mod
    from lionagi.ln.concurrency import run_async

    inv_id = run_async(
        invoke_mod._start_invocation(skill=skill, plugin=plugin, prompt=prompt, metadata=metadata)
    )
    return {"ok": True, "invocation_id": inv_id}


def invoke_end(
    invocation_id: str,
    status: Literal["completed", "failed", "timed_out", "aborted", "cancelled"] = "completed",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close an invocation with a terminal status and its final metadata.

    Args:
        invocation_id: The id from ``invoke_start``.
        status: How the work ended. Say ``failed`` when it did — the status is
            what later queries filter on.
        metadata: JSON merged key-by-key into the existing metadata, so anything
            written during the run survives unless this overwrites that key.

    Returns ``{"ok": true, "invocation": {...}}`` including ``session_count``.
    """
    from lionagi.cli import invoke as invoke_mod
    from lionagi.ln.concurrency import run_async

    result = run_async(invoke_mod._end_invocation(invocation_id, status=status, metadata=metadata))
    if result is None:
        return _err(f"invocation not found: {invocation_id}")
    return {"ok": True, "invocation": result}


def invoke_list(
    skill: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """List recent invocations — what orchestrations ran, and how they ended.

    Answers "has this already been done, and did it work?" across sessions.
    Filter by ``skill`` to see every run of one orchestration, or by ``status``
    (``running``, ``completed``, ``failed``, ``timed_out``, ``aborted``,
    ``cancelled``) to find the ones that need attention.

    Returns ``{"ok": true, "invocations": [...]}``, newest first, each with
    ``id``, ``skill``, ``status``, ``session_count`` and ``prompt``.

    Args:
        skill: Only return invocations of this skill.
        status: Only return invocations in this status.
        limit: Maximum rows to return.
    """
    from lionagi.cli import invoke as invoke_mod
    from lionagi.ln.concurrency import run_async

    rows = run_async(invoke_mod._list_invocations(skill=skill, status=status, limit=limit))
    return {"ok": True, "invocations": list(rows)}


TOOLS = (
    schedule_list,
    schedule_get,
    schedule_limits,
    schedule_create,
    schedule_create_typed,
    schedule_set_enabled,
    schedule_delete,
    schedule_trigger,
    schedule_runs,
    schedule_run,
    schedule_status,
    team_create,
    team_list,
    team_show,
    team_send,
    team_receive,
    casts_list,
    casts_get,
    plugin_list,
    plugin_info,
    plugin_trust,
    plugin_set_enabled,
    invoke_start,
    invoke_end,
    invoke_list,
)


def register(mcp) -> None:  # noqa: ANN001 - FastMCP, imported by the caller
    """Register every tool in this module on the given FastMCP server."""
    for tool in TOOLS:
        mcp.tool(tool)

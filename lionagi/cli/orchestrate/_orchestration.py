# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pattern-agnostic orchestration primitives.

Every CLI orchestration pattern (fanout, flow, and any future ones) walks
the same phases — only a few of them carry pattern-specific logic:

    A. Setup        (shared)   — load profile, build orchestrator, allocate run
    B. Plan         (pattern)  — emit structured plan (AgentRequest[] | FlowPlan | ...)
    C. Build worker (shared)   — resolve model/profile/system → Branch with cwd
    D. Execute DAG  (shared)   — session.flow(builder)
    E. Iterate      (pattern)  — optional critic/re-plan loop (flow only today)
    F. Synthesize   (shared)   — optional final synthesis agent
    G. Finalize     (shared)   — persist, write manifest, emit hints

This module provides A / C / G as standalone functions and an
``OrchestrationEnv`` dataclass that bundles the setup output. D and F are
thin enough to stay inline in the pattern files. B and E are genuinely
pattern-specific and stay with their owners.

A ``Pattern`` base class is deliberately NOT introduced yet — two patterns
does not justify the abstraction. Revisit when a third arrives.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi import Branch, Session
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.generic.log import DataLoggerConfig

from .._agents import AgentProfile, load_agent_profile
from .._logging import hint, progress
from .._providers import build_imodel_from_spec
from .._runs import RunDir, allocate_run, save_last_branch_pointer

__all__ = (
    "OrchestrationEnv",
    "resolve_worker_spec",
    "setup_orchestration",
    "build_worker_branch",
    "finalize_orchestration",
    "start_live_persist",
    "stop_live_persist",
    "EFFORT_GUIDANCE",
    "EFFORT_MAP",
    "team_guidance",
    "team_worker_system",
)


# ── Generic orchestrator-prompt building blocks ─────────────────────────
# Reused across patterns. Kept as module-level constants/functions so
# patterns compose them into their own planning prompts without each
# pattern restating the text.

EFFORT_GUIDANCE: str = (
    "EFFORT TIERS: Use per-op guidance for behavioral framing. "
    "low=skim structure quickly; medium=careful read; high=thorough "
    "analysis; xhigh=deep multi-step reasoning. Match effort to task weight. "
)

# Short instructions injected into each worker's context when its agent
# has an effort override. Keeps worker prompts tight.
EFFORT_MAP: dict[str, str] = {
    "low": "Skim quickly, structured output.",
    "medium": "Read carefully, balance depth/speed.",
    "high": "Thorough analysis, take your time.",
    "xhigh": "Deep reasoning, maximum effort.",
    "max": "Deep reasoning, maximum effort.",
}


def team_guidance(team_name: str | None) -> str:
    """Return team-mode orchestrator guidance, or empty string if no team."""
    if not team_name:
        return ""
    return (
        f"TEAM MODE active (team: {team_name}). In each op instruction, "
        "tell the executing agent to check its inbox before starting and "
        "send coordination signals to relevant teammates if it discovers "
        "something affecting them. "
    )


def team_worker_system(
    team_data: dict | None,
    worker_name: str,
) -> str | None:
    """Render the TEAM coordination section (to be APPENDED to the base
    system prompt), or None outside team mode.

    Roster comes from ``team_data["members"]`` — which was populated at
    team-creation time from the full pre-computed worker name list. This
    avoids an ordering bug: workers are built one-by-one, so reading
    from ``env.all_names`` would show a partial roster to early workers.

    build_worker_branch composes this section onto BARE_WORKER_SYSTEM or
    a profile's system_prompt so workers keep their artifact protocol
    and tool guidance.
    """
    if not team_data:
        return None
    from ._common import TEAM_COORD_SECTION  # avoid import cycle

    # team_data["members"] includes "orchestrator" + all worker names.
    # Render orchestrator explicitly at the top, then teammates, then self.
    all_members = team_data.get("members", [])
    worker_names = [m for m in all_members if m != "orchestrator"]
    teammates = [n for n in worker_names if n != worker_name]
    roster_lines = ["- orchestrator (coordinator)"]
    roster_lines += [f"- {t}" for t in teammates]
    roster_lines.append(f"- **{worker_name}** (you)")
    return TEAM_COORD_SECTION.format(
        worker_name=worker_name,
        team_name=team_data["name"],
        team_id=team_data["id"],
        roster_text="\n".join(roster_lines),
    )


# ── Worker spec resolution ──────────────────────────────────────────────


def resolve_worker_spec(
    token: str,
) -> tuple[str, AgentProfile | None]:
    """Resolve a worker token to (model_spec, profile_or_None).

    Tokens containing ``/`` are treated as explicit model specs
    (``codex/gpt-5.4``). Bare tokens try to load a profile first; if
    there is no profile with that name, they fall through as a model
    spec — useful for one-off names the user passes through.
    """
    if "/" in token:
        return token, None
    try:
        profile = load_agent_profile(token)
        # Profile model MUST be set when present; callers that need a
        # default apply it themselves (so we don't hardcode a fallback
        # that rots as the default model changes).
        return profile.model or token, profile
    except FileNotFoundError:
        return token, None


# ── OrchestrationEnv ────────────────────────────────────────────────────


@dataclass
class OrchestrationEnv:
    """Shared state and config for one orchestration run.

    Created by ``setup_orchestration``, consumed by ``build_worker_branch``
    and ``finalize_orchestration``, and passed around pattern-specific
    phases so they can read common config (run paths, session, orc branch,
    worker defaults) without threading 10+ kwargs.
    """

    # Persistence + Lion objects
    run: RunDir
    session: Session
    orc_branch: Branch
    builder: OperationGraphBuilder

    # Orchestrator config
    orc_profile: AgentProfile | None
    default_model_spec: str

    # Worker defaults
    bare: bool
    effort: str | None
    theme: str | None
    yolo: bool
    bypass: bool
    verbose: bool
    fast: bool
    cwd: str | None  # user --cwd (falls through to orchestrator); per-worker
    # artifact dirs override this when writing via build_worker_branch

    # Optional shared features
    team_data: dict | None = None

    # Live SQLite persist context (set by start_live_persist)
    _live_persist: dict | None = field(default=None, repr=False)

    # Worker name bookkeeping (mutable)
    _name_counts: dict[str, int] = field(default_factory=dict)
    _all_names: list[str] = field(default_factory=list)

    def assign_name(self, role: str) -> str:
        """Allocate a deduplicated name for a role (e.g. ``explorer-2``)."""
        self._name_counts[role] = self._name_counts.get(role, 0) + 1
        n = self._name_counts[role]
        name = f"{role}-{n}" if n > 1 else role
        self._all_names.append(name)
        return name

    def register_name(self, name: str) -> None:
        """Record a pre-assigned name (fanout pre-computes worker names)."""
        self._all_names.append(name)

    @property
    def all_names(self) -> list[str]:
        return list(self._all_names)


# ── Phase A: Setup ──────────────────────────────────────────────────────


def setup_orchestration(
    *,
    pattern_name: str,  # "Fanout" | "Flow" | ... — passed to builder name
    model_spec: str,
    agent_name: str | None,
    save_dir: str | None,
    cwd: str | None,
    yolo: bool,
    bypass: bool = False,
    verbose: bool,
    effort: str | None,
    theme: str | None,
    bare: bool = False,
    fast: bool = False,
) -> OrchestrationEnv:
    """Phase A — resolve orchestrator config, allocate run, build branch+session.

    Returns an ``OrchestrationEnv`` ready for the pattern's planning phase.
    The caller is responsible for setting up teams (which need worker
    names computed from the pattern's own plan) and for invoking the
    planning phase — setup does NOT add any operations to the builder.
    """
    orc_profile: AgentProfile | None = None
    if agent_name:
        orc_profile = load_agent_profile(agent_name)
        if orc_profile.model and not model_spec:
            model_spec = orc_profile.model
        if orc_profile.effort and not effort:
            effort = orc_profile.effort
        if orc_profile.yolo and not yolo:
            yolo = True
        if orc_profile.fast_mode and not fast:
            fast = True

    if not model_spec:
        raise ValueError(
            "Provide a model spec or use -a/--agent to load a profile with a model."
        )

    orc_imodel = build_imodel_from_spec(
        model_spec,
        yolo=yolo,
        verbose=verbose,
        effort_override=effort,
        theme=theme,
        fast=fast,
    )
    if cwd:
        orc_imodel.endpoint.config.kwargs.setdefault("repo", Path(cwd))

    run = allocate_run(save_dir=save_dir)
    run.ensure_artifact_root()

    orc_system = orc_profile.system_prompt if orc_profile else None
    orc_branch = Branch(
        chat_model=orc_imodel,
        system=orc_system,
        log_config=DataLoggerConfig(auto_save_on_exit=False),
        name="orchestrator",
    )
    session = Session(default_branch=orc_branch)
    builder = OperationGraphBuilder(pattern_name)

    return OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orc_branch,
        builder=builder,
        orc_profile=orc_profile,
        default_model_spec=model_spec,
        bare=bare,
        effort=effort,
        theme=theme,
        yolo=yolo,
        bypass=bypass,
        verbose=verbose,
        fast=fast,
        cwd=cwd,
    )


# ── Phase C: build_worker_branch ────────────────────────────────────────


def build_worker_branch(
    env: OrchestrationEnv,
    *,
    agent_id: str,
    role: str,
    model_override: str | None = None,
    explicit_name: str | None = None,
    system_prompt_override: str | None = None,
) -> tuple[Branch, str, AgentProfile | None]:
    """Phase C — resolve model/profile/effort/system and build a Branch.

    System prompt composition: the base prompt is chosen once
    (``system_prompt_override`` > profile > BARE), then if the run is in
    team mode the team coordination section is APPENDED to it. Team mode
    does not replace the base — workers still need the artifact protocol
    and tool guidance that BARE (or the profile) provides.

    Parameters
    ----------
    agent_id
        The stable id used for this worker's artifact directory
        (``run.agent_artifact_dir(agent_id)``). In fanout this is the
        pre-assigned worker name; in flow it is the plan-assigned
        ``FlowAgent.id``.
    role
        Role name used for profile lookup and name dedup (``explorer``,
        ``analyst``, ...). Ignored if ``env.bare``.
    model_override
        If set, overrides the profile/default model. The orchestrator
        may emit a specific model per worker (``FlowAgent.model``).
    explicit_name
        If the caller has its own naming scheme (fanout pre-computes),
        pass the name here; we still register it so ``env.all_names``
        stays in sync.
    system_prompt_override
        Hard override of the base system prompt (tests, special cases).
        The team coordination section is still appended on top when team
        mode is active — pass None to let the normal profile/BARE
        selection happen.

    Returns
    -------
    (branch, model_string, profile_or_None)
    """
    from ._common import BARE_WORKER_SYSTEM  # avoid import cycle

    w_profile: AgentProfile | None = None
    if env.bare:
        w_model = model_override or env.default_model_spec
    else:
        resolved_model, w_profile = resolve_worker_spec(role)
        if model_override:
            w_model = model_override
        elif w_profile:
            w_model = resolved_model
        else:
            w_model = env.default_model_spec

    w_effort = env.effort
    if not env.bare and w_profile and w_profile.effort and not env.effort:
        w_effort = w_profile.effort
    w_yolo = env.yolo
    if not env.bare and w_profile and w_profile.yolo:
        w_yolo = True
    w_fast = env.fast
    if not env.bare and w_profile and w_profile.fast_mode:
        w_fast = True

    w_imodel = build_imodel_from_spec(
        w_model,
        yolo=w_yolo,
        bypass=env.bypass,
        verbose=env.verbose,
        effort_override=w_effort,
        theme=env.theme,
        fast=w_fast,
    )
    # Per-agent artifact directory: workers write files here; downstream
    # agents read via relative paths. Overrides env.cwd.
    artifact_dir = env.run.agent_artifact_dir(agent_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    w_imodel.endpoint.config.kwargs["repo"] = artifact_dir
    # Grant write access to the actual project directory so workers can
    # edit source files, not just their artifact sandbox.
    project_root = (
        str(Path(env.cwd).resolve()) if env.cwd else str(Path.cwd().resolve())
    )
    w_imodel.endpoint.config.kwargs.setdefault("add_dir", [])
    if project_root not in w_imodel.endpoint.config.kwargs["add_dir"]:
        w_imodel.endpoint.config.kwargs["add_dir"].append(project_root)

    # Name assignment — caller may pre-compute, otherwise we dedupe by role.
    if explicit_name is not None:
        env.register_name(explicit_name)
        wname = explicit_name
    else:
        wname = env.assign_name(role)

    # Base system prompt: explicit override > profile > BARE.
    if system_prompt_override is not None:
        base_system = system_prompt_override
    elif not env.bare and w_profile and w_profile.system_prompt:
        base_system = w_profile.system_prompt
    else:
        base_system = BARE_WORKER_SYSTEM

    # Team mode APPENDS the coord section — does not replace the base.
    team_section = team_worker_system(env.team_data, wname)
    w_system = f"{base_system}\n\n{team_section}" if team_section else base_system

    wb = Branch(
        chat_model=w_imodel,
        system=w_system,
        log_config=DataLoggerConfig(auto_save_on_exit=False),
        name=wname,
    )
    env.session.include_branches(wb)

    # Register live persist hook on this new branch
    if env._live_persist:
        _register_branch_hook(env._live_persist, wb)

    return wb, w_model, w_profile


# ── Phase G: finalize ───────────────────────────────────────────────────


def finalize_orchestration(
    env: OrchestrationEnv,
    *,
    kind: str,
    prompt: str,
    extras: dict | None = None,
    emit_hints: bool = True,
) -> tuple[list[tuple[str, str, str]], str]:
    """Phase G — persist branches, write manifest, update last-branch pointer.

    Parameters
    ----------
    kind
        The pattern kind recorded in the manifest: ``"fanout"`` |
        ``"flow"`` | future. Used by ``li runs show`` to render the
        right summary view.
    prompt
        Original user prompt. Captured in the manifest for review.
    extras
        Pattern-specific manifest fields (agent list, synthesis model,
        control-round count, etc.). Merged under ``run.json`` alongside
        the shared keys.
    emit_hints
        When True, print ``[to resume] li agent -r <id> "..."`` for the
        orchestrator and each worker branch. Disable when a caller
        wants custom post-run output.

    Returns
    -------
    (branch_ids, orc_branch_id) where branch_ids is
    ``[(provider, branch_id, name), ...]``.
    """
    # Late import avoids the _common ↔ _orchestration cycle.
    from ._common import persist_session_branches

    branch_ids = persist_session_branches(env.session, env.run)
    orc_branch_id = str(env.orc_branch.id)

    manifest: dict = {
        "kind": kind,
        "prompt": prompt,
        "model_spec": env.default_model_spec,
        "orchestrator_branch_id": orc_branch_id,
        "branches": [
            {"id": bid, "provider": prov, "name": bname}
            for prov, bid, bname in branch_ids
        ],
    }
    save_last_branch_pointer(env.run.run_id, orc_branch_id)

    if emit_hints:
        hint(f'\n[orchestrator] li agent -r {orc_branch_id} "..."')
        for provider, bid, bname in branch_ids:
            if bid != orc_branch_id:
                hint(f'[{bname}]      li agent -r {bid} "..."')

    return branch_ids, orc_branch_id


# ── Live SQLite persist ──────────────────────────────────────────────


async def start_live_persist(
    env: OrchestrationEnv,
    *,
    invocation_kind: str | None = None,
    playbook_name: str | None = None,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
) -> None:
    """Open state.db, create session row, register hooks on existing branches.

    New branches created via build_worker_branch auto-register via the
    env._live_persist check there.
    """
    from lionagi.state.db import StateDB

    try:
        db = StateDB()
        await db.open()

        session = env.session
        session_id = str(session.id)
        session_dict = session.to_dict(mode="db")

        session_prog_id = str(uuid.uuid4())
        await db.create_progression(session_prog_id)
        await db.create_session({
            "id": session_id,
            "created_at": session_dict["created_at"],
            "node_metadata": session_dict.get("node_metadata"),
            "name": session_dict.get("name"),
            "user": session_dict.get("user"),
            "progression_id": session_prog_id,
            "first_msg_id": None,
            "last_msg_id": None,
            "invocation_kind": invocation_kind,
            "playbook_name": playbook_name,
            "agent_name": agent_name,
            "artifacts_path": artifacts_path,
            "status": "running",
            "started_at": time.time(),
        })

        ctx: dict[str, Any] = {
            "db": db,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_ids": {},
            "hooks": [],
        }
        env._live_persist = ctx

        for branch in session.branches:
            _register_branch_hook(ctx, branch)
    except Exception:
        env._live_persist = None


def _register_branch_hook(ctx: dict[str, Any], branch: Branch) -> None:
    """Register async message hook on a branch.

    Branch row + progression are lazily created on the first message
    (since this function may be called from sync build_worker_branch
    where we can't await DB operations).
    """
    db = ctx["db"]
    session_id = ctx["session_id"]
    session_prog_id = ctx["session_prog_id"]
    branch_id = str(branch.id)

    branch_prog_id = str(uuid.uuid4())
    ctx["branch_prog_ids"][branch_id] = branch_prog_id
    initialized = {"done": False}

    async def _ensure_branch_row():
        if initialized["done"]:
            return
        initialized["done"] = True

        await db.create_progression(branch_prog_id)

        branch_dict = branch.to_dict(mode="db")
        node_meta = branch_dict.get("node_metadata") or {}
        if isinstance(node_meta, str):
            node_meta = json.loads(node_meta)
        if "chat_model" in branch_dict:
            node_meta["chat_model"] = branch_dict["chat_model"]
        node_meta = json.loads(json.dumps(node_meta, default=str))

        system_msg_id = None
        if branch.system:
            sys_dict = branch.system.to_dict(mode="db")
            system_msg_id = sys_dict["id"]
            await db.insert_message(sys_dict)

        await db.create_branch({
            "id": branch_id,
            "created_at": branch_dict["created_at"],
            "node_metadata": node_meta,
            "user": branch_dict.get("user"),
            "name": branch_dict.get("name"),
            "session_id": session_id,
            "progression_id": branch_prog_id,
            "system_msg_id": system_msg_id,
        })

    async def _on_message(msg):
        try:
            await _ensure_branch_row()
            msg_dict = msg.to_dict(mode="db")
            msg_id = msg_dict["id"]
            await db.insert_message(msg_dict)
            await db.append_to_progression(branch_prog_id, msg_id)
            await db.append_to_progression(session_prog_id, msg_id)
        except Exception:
            pass

    branch.on_message_added.append(_on_message)
    ctx["hooks"].append((branch, _on_message))


async def stop_live_persist(
    env: OrchestrationEnv,
    *,
    status: str = "completed",
) -> None:
    """Update session bookmarks, lifecycle columns, and close DB."""
    ctx = env._live_persist
    if ctx is None:
        return
    try:
        db = ctx["db"]
        session_prog_id = ctx["session_prog_id"]

        all_msgs = await db.get_progression(session_prog_id)
        update_kwargs: dict[str, Any] = {
            "status": status,
            "ended_at": time.time(),
        }
        if all_msgs:
            update_kwargs["first_msg_id"] = all_msgs[0]
            update_kwargs["last_msg_id"] = all_msgs[-1]
        await db.update_session(ctx["session_id"], **update_kwargs)

        for branch, hook in ctx["hooks"]:
            try:
                branch.on_message_added.remove(hook)
            except ValueError:
                pass

        await db.close()
    except Exception:
        pass
    env._live_persist = None

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
from lionagi.ln.concurrency import Lock
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.state import provenance as _provenance
from lionagi.state.artifact_verifier import (
    missing_artifact_evidence,
    missing_artifact_summary,
    verify_artifact_contract,
)

from .._agents import AgentProfile, load_agent_profile
from .._logging import hint, warn
from .._providers import build_imodel_from_spec, resolve_persisted_effort
from .._runs import RunDir, allocate_run, save_last_branch_pointer


def _resolve_session_model(provider: str | None, model: str | None) -> str | None:
    """ADR-0022: canonical 'provider/model' for the session.model column.

    Thin wrapper over :func:`provenance.resolve_model_spec` so callers
    don't repeat the import. Returns None when both args are None.
    """
    return _provenance.resolve_model_spec(provider, model)


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

    # Time budget: total seconds for the entire flow (from --timeout or
    # playbook timeout:). None means no budget was configured — workers
    # will not receive a BUDGET preamble.
    total_budget: int | None = None

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
    total_budget: int | None = None,
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
        raise ValueError("Provide a model spec or use -a/--agent to load a profile with a model.")

    orc_imodel = build_imodel_from_spec(
        model_spec,
        yolo=yolo,
        verbose=verbose,
        effort_override=effort,
        theme=theme,
        fast=fast,
    )
    # Resolve the effort value to persist: captures post-clamp values
    # (e.g. "max"→"xhigh" for codex) and forces None for no-effort providers.
    _orc_provider = orc_imodel.endpoint.config.provider
    effort = resolve_persisted_effort(_orc_provider, orc_imodel, effort)
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
        total_budget=total_budget,
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
    agent_modes: list[str] | None = None,
    agent_permissions: str | None = None,
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
    project_root = str(Path(env.cwd).resolve()) if env.cwd else str(Path.cwd().resolve())
    w_imodel.endpoint.config.kwargs.setdefault("add_dir", [])
    if project_root not in w_imodel.endpoint.config.kwargs["add_dir"]:
        w_imodel.endpoint.config.kwargs["add_dir"].append(project_root)

    # Name assignment — caller may pre-compute, otherwise we dedupe by role.
    if explicit_name is not None:
        env.register_name(explicit_name)
        wname = explicit_name
    else:
        wname = env.assign_name(role)

    # ── Casts AgentSpec fallback ──────────────────────────────────────
    # When no profile was resolved and no system_prompt_override was given,
    # check if the role is a known casts role and compose an AgentSpec from it.
    # This enables modes + emission contracts for the new casts-native roles.
    w_spec = None
    if (
        not env.bare
        and w_profile is None
        and system_prompt_override is None
        and agent_modes is not None
    ):
        try:
            from lionagi.casts.pattern import list_roles as _list_roles

            if role in set(_list_roles()):
                from lionagi.agent.spec import AgentSpec

                # Determine default tool zone by role heuristic
                _write_roles = {
                    "implementer",
                    "refactorer",
                    "migrator",
                    "prototyper",
                    "deployer",
                    "operator",
                }
                _reader_roles = {
                    "researcher",
                    "analyst",
                    "auditor",
                    "investigator",
                    "reviewer",
                    "critic",
                }
                if role in _write_roles:
                    _tools: tuple[str, ...] = ("coding",)
                elif role in _reader_roles:
                    _tools = ("reader", "search")
                else:
                    _tools = ()

                w_spec = AgentSpec.compose(
                    role,
                    modes=agent_modes,
                    permissions=agent_permissions,
                    tools=_tools,
                )
        except Exception:
            import logging as _logging

            _logging.getLogger("lionagi.cli").debug(
                "Casts AgentSpec fallback skipped for role %r: %s",
                role,
                "profile path is authoritative",
                exc_info=True,
            )

    # Base system prompt: explicit override > casts spec > profile > BARE.
    if system_prompt_override is not None:
        base_system = system_prompt_override
    elif w_spec is not None:
        from lionagi.session.prompts import LION_SYSTEM_MESSAGE

        base_system = LION_SYSTEM_MESSAGE.strip() + "\n\n" + w_spec.build_system_message()
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

    # ── Casts permissions + emission wiring ──────────────────────────
    if w_spec is not None:
        # Translate permissions to endpoint kwargs for claude/claude_code providers
        if w_spec.permissions is not None:
            _provider = getattr(getattr(w_imodel, "endpoint", None), "config", None)
            _provider_name = getattr(_provider, "provider", "") or ""
            if "claude" in _provider_name.lower():
                from lionagi.agent.adapters.claude_code import translate_permissions

                _perm_kwargs = translate_permissions(w_spec.permissions)
                if _provider is not None:
                    for _k, _v in _perm_kwargs.items():
                        w_imodel.endpoint.config.kwargs[_k] = _v
            else:
                warn(
                    f"Permission preset {w_spec.permissions.mode!r} set for agent "
                    f"{agent_id!r} but provider {_provider_name!r} does not support "
                    "permission translation — permissions will not be enforced."
                )

        # Grant emission capabilities when the role declares them
        _op = w_spec.emission_operable()
        if _op is not None and hasattr(wb, "grant_capabilities"):
            wb.grant_capabilities(_op)

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
    """Phase G — persist branch snapshots + last-branch pointer + hints.

    Branch state persists via the live SQLite hooks during execution (ADR-0004),
    but the resume hints emitted here say ``li agent -r <id>`` — which routes
    through ``find_branch()`` and currently looks for
    ``runs/<run>/branches/<id>.json``. We write one canonical snapshot per
    branch so the hint resolves. The cost is a single small JSON write per
    branch at exit; the run-time message stream itself stays SQLite-only.

    Parameters
    ----------
    kind
        Pattern kind (``"fanout"`` | ``"flow"``).
    prompt
        Original user prompt.
    extras
        Pattern-specific extras (agents, operations). Persisted to SQLite
        session node_metadata so Studio can render the execution DAG.
    emit_hints
        When True, print ``[to resume] li agent -r <id> "..."`` for the
        orchestrator and each worker branch.

    Returns
    -------
    (branch_ids, orc_branch_id) where branch_ids is
    ``[(provider, branch_id, name), ...]`` derived from the in-memory
    session.
    """
    import logging

    # Ensure branches_dir exists (allocate_run did, but be defensive).
    env.run.ensure_state_dirs()
    log = logging.getLogger("lionagi.cli")

    branch_ids: list[tuple[str, str, str]] = []
    for branch in env.session.branches:
        provider = branch.chat_model.endpoint.config.provider
        branch_ids.append((provider, str(branch.id), branch.name))

        # Write the resume snapshot. Failure here must NOT abort the
        # finalize — the run already completed; missing JSON only
        # affects ``li agent -r`` for this branch.
        try:
            snap_path = env.run.branch_path(str(branch.id))
            snap_path.write_text(json.dumps(branch.to_dict(), default=str))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "finalize: branch snapshot write failed for %s: %s",
                branch.id,
                exc,
                exc_info=True,
            )

    if extras:
        env._finalize_extras = extras

    orc_branch_id = str(env.orc_branch.id)
    save_last_branch_pointer(env.run.run_id, orc_branch_id)

    if emit_hints:
        hint(f'\n[orchestrator] li agent -r {orc_branch_id} "..."')
        for _provider, bid, bname in branch_ids:
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
    artifact_contract: dict[str, Any] | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
) -> None:
    """Open state.db, create session row, register hooks on existing branches.

    New branches created via build_worker_branch auto-register via the
    env._live_persist check there.

    On any setup failure, the DB connection is closed before returning
    so the aiosqlite background thread does not leak (non-daemon thread
    leak prevents Python interpreter shutdown — "CLI hangs forever").
    """
    import logging

    from lionagi.state.db import StateDB

    db: StateDB | None = None
    try:
        db = StateDB()
        await db.open()

        session = env.session
        session_id = str(session.id)
        session_dict = session.to_dict(mode="db")

        session_prog_id = str(uuid.uuid4())
        await db.create_progression(session_prog_id)
        if project:
            _proj, _proj_src = project, "explicit"
        else:
            from lionagi.cli._project import detect_project

            _proj, _proj_src = detect_project()
        await db.create_session(
            {
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
                "artifact_contract_json": artifact_contract,
                "status": "running",
                "started_at": time.time(),
                # ADR-0020: optional skill orchestration parent
                "invocation_id": invocation_id,
                # ADR-0022: orchestrator-level provenance. For multi-model
                # flows the per-branch model is the actual; this is the
                # "primary" / "default" that the runs list shows.
                "model": _resolve_session_model(provider, model),
                "provider": provider,
                "effort": effort,
                "agent_hash": _provenance.agent_definition_hash(agent_name),
                # ADR-0026: project detection.
                "project": _proj,
                "project_source": _proj_src,
            }
        )

        ctx: dict[str, Any] = {
            "db": db,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_ids": {},
            "hooks": [],
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
        }
        env._live_persist = ctx

        for branch in session.branches:
            _register_branch_hook(ctx, branch)
    except Exception as exc:
        logging.getLogger("lionagi.cli").warning(
            "live persist setup failed (%s) — disabling persistence for this run",
            exc,
            exc_info=True,
        )
        env._live_persist = None
        if db is not None:
            try:
                await db.close()
            except Exception as close_exc:  # noqa: BLE001
                logging.getLogger("lionagi.cli").warning(
                    "fallback db.close after setup failure also failed: %s",
                    close_exc,
                )


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
    init_lock = Lock()

    async def _ensure_branch_row():
        # Serialize concurrent first messages on the same branch so
        # only one of them runs the DB writes. The flag flips to True
        # ONLY after the writes commit — a transient failure leaves
        # initialized=False so the next message retries.
        if initialized["done"]:
            return
        async with init_lock:
            if initialized["done"]:
                return

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

            # ADR-0022: per-branch provenance — pulled from the runtime
            # endpoint config so multi-model flows correctly disclose what
            # *each* branch actually used, not the orchestrator default.
            br_model: str | None = None
            br_provider: str | None = None
            try:
                ep_cfg = branch.chat_model.endpoint.config
                br_provider = getattr(ep_cfg, "provider", None)
                br_model_raw = (ep_cfg.kwargs or {}).get("model")
                br_model = _provenance.resolve_model_spec(br_provider, br_model_raw)
            except Exception as _provenance_exc:  # noqa: BLE001
                # Provenance is best-effort — never block the branch row.
                import logging

                logging.getLogger("lionagi.cli").debug(
                    "branch provenance lookup failed for %s: %s",
                    branch_id,
                    _provenance_exc,
                )
            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": branch_dict["created_at"],
                    "node_metadata": node_meta,
                    "user": branch_dict.get("user"),
                    "name": branch_dict.get("name"),
                    "session_id": session_id,
                    "progression_id": branch_prog_id,
                    "system_msg_id": system_msg_id,
                    "model": br_model,
                    "provider": br_provider,
                    # branch.name is the agent role within the flow
                    # ("r1", "critic", "explorer", ...).
                    "agent_name": branch_dict.get("name"),
                }
            )
            initialized["done"] = True

    async def _on_message(msg):
        # Live-persist failures are logged (not raised) so a DB write
        # blip cannot abort an in-flight orchestration. The error is
        # visible in -v runs without crashing the worker.
        try:
            await _ensure_branch_row()
            msg_dict = msg.to_dict(mode="db")
            msg_id = msg_dict["id"]
            await db.insert_message(msg_dict)
            await db.append_to_progression(branch_prog_id, msg_id)
            await db.append_to_progression(session_prog_id, msg_id)
            # ADR-0019: activity heartbeat for staleness detection.
            await db.touch_session_activity(session_id, at=msg_dict.get("created_at"))
            # ADR-0009: keep branches.system_msg_id pointing at the
            # current system if the runtime replaces it mid-flow.
            if msg_dict.get("role") == "system":
                await db.update_branch(branch_id, system_msg_id=msg_id)
        except Exception as exc:
            import logging

            logging.getLogger("lionagi.cli").warning(
                "live persist write failed for branch %s: %s",
                branch_id,
                exc,
                exc_info=True,
            )

    branch.on_message_added.append(_on_message)
    ctx["hooks"].append((branch, _on_message))


async def stop_live_persist(
    env: OrchestrationEnv,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
) -> str:
    """Update session bookmarks, lifecycle columns, and close DB.

    Returns the *effective* terminal status — ADR-0029 §7's verification
    override can flip a clean ``completed`` into ``failed`` when required
    artifacts are missing. Callers MUST use the returned status for the
    process exit code.

    The DB close is in its own ``finally`` so it always runs — even if
    the bookmark update or hook removal fails. Leaving the DB unclosed
    leaks the aiosqlite worker (non-daemon thread) and prevents the
    Python interpreter from shutting down.
    """
    import logging

    ctx = env._live_persist
    if ctx is None:
        return status
    log = logging.getLogger("lionagi.cli")
    db = ctx["db"]
    try:
        session_prog_id = ctx["session_prog_id"]
        session_id = ctx["session_id"]

        all_msgs = await db.get_progression(session_prog_id)
        update_kwargs: dict[str, Any] = {
            "ended_at": time.time(),
        }
        if all_msgs:
            update_kwargs["first_msg_id"] = all_msgs[0]
            update_kwargs["last_msg_id"] = all_msgs[-1]

        extras = getattr(env, "_finalize_extras", None)
        if extras:
            update_kwargs["node_metadata"] = json.dumps(extras)

        await db.update_session(session_id, **update_kwargs)

        from lionagi.cli.agent import _resolve_run_reason

        reason_code, reason_summary, evidence_refs = _resolve_run_reason(
            status=status,
            exception=exception,
        )
        metadata: dict[str, Any] | None = None
        if exception is not None:
            metadata = {"exception_class": type(exception).__name__}

        session_row = await db.get_session(session_id) or {}
        verification = verify_artifact_contract(
            session_row.get("artifact_contract_json"),
            artifacts_root=session_row.get("artifacts_path"),
        )
        await db.update_artifact_verification(session_id, verification)

        final_status = status
        final_reason_code = reason_code
        final_reason_summary = reason_summary
        final_evidence_refs = evidence_refs
        if verification and verification["status"] == "failed":
            missing = verification["missing_required"]
            if status == "completed":
                from lionagi.state.reasons import RunReasons

                final_status = "failed"
                final_reason_code = RunReasons.FAILED_MISSING_ARTIFACT
                final_reason_summary = missing_artifact_summary(missing)
                final_evidence_refs = missing_artifact_evidence(missing)
            else:
                metadata = dict(metadata or {})
                metadata["artifact_verification_status"] = verification["status"]
                metadata["missing_required_artifact_ids"] = [
                    str(entry.get("id", "")) for entry in missing
                ]

        await db.update_status(
            "session",
            session_id,
            new_status=final_status,
            reason_code=final_reason_code,
            reason_summary=final_reason_summary,
            evidence_refs=final_evidence_refs,
            source="executor",
            actor=session_id,
            metadata=metadata,
        )

        # Remove ALL matching registrations of each hook (list.remove
        # would only drop the first; a duplicate registration would
        # leave a closed-DB hook live).
        for branch, hook in ctx["hooks"]:
            branch.on_message_added[:] = [h for h in branch.on_message_added if h is not hook]
    except Exception as exc:
        log.warning("live persist teardown failed: %s", exc, exc_info=True)
        return status
    finally:
        try:
            await db.close()
        except Exception as exc:
            log.warning("live persist db.close failed: %s", exc, exc_info=True)
        env._live_persist = None
    return final_status

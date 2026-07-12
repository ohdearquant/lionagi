# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pattern-agnostic orchestration primitives (setup, worker build, finalize)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uuid import UUID

    from lionagi.casts.pack import Pack
    from lionagi.session.exchange import Exchange
    from lionagi.tools.communication.messenger import LionMessenger

from lionagi import Branch, Session
from lionagi._errors import ConfigurationError
from lionagi.agent import AgentSpec, create_agent
from lionagi.operations.builder import OperationGraphBuilder
from lionagi.protocols.generic.log import DataLoggerConfig
from lionagi.state import provenance as _provenance

from .._logging import hint
from .._providers import (
    AgentProfile,
    build_imodel_from_spec,
    list_agents,
    load_agent_profile,
    parse_model_spec,
    resolve_persisted_effort,
)
from .._runs import (
    RunDir,
    _make_message_handler,
    _open_shared_db,
    _resolve_project,
    allocate_run,
    save_last_branch_pointer,
    teardown_persist,
)

__all__ = (
    "OrchestrationEnv",
    "resolve_worker_spec",
    "setup_orchestration",
    "build_worker_branch",
    "make_help_coordinator",
    "finalize_orchestration",
    "start_live_persist",
    "stop_live_persist",
    "EFFORT_GUIDANCE",
    "EFFORT_MAP",
    "team_guidance",
    "team_worker_system",
    "available_roles",
    "role_roster",
    "mode_roster",
    "casts_role_system",
    "role_config",
    "resolve_modes",
    "parse_orchestrator_provider",
)


def parse_orchestrator_provider(model_spec: str) -> tuple[str | None, str | None]:
    """Parse model_spec into (model, provider) for session-row provenance."""
    ms = parse_model_spec(model_spec) if model_spec else None
    if ms is None:
        return None, None
    provider = ms.model.split("/", 1)[0] if "/" in ms.model else None
    return ms.model, provider


def available_roles() -> list[str]:
    """Casts roles + user profiles the orchestrator may assign to."""
    from lionagi.casts.pattern import list_roles

    return sorted(set(list_roles()) | set(list_agents()))


def _role_blurb(role: str, default_model: str) -> str:
    try:
        p = load_agent_profile(role)
        return f"user profile (model: {p.model or default_model})"
    except FileNotFoundError:
        pass
    from lionagi.casts.pattern import Role

    try:
        desc = Role.load(role).description
    except ValueError:
        return ""
    first = desc.split(". ", 1)[0].strip()
    return (first[:160] + "…") if len(first) > 161 else first


def role_roster(default_model: str) -> str:
    lines = [f"- {r}: {_role_blurb(r, default_model)}" for r in available_roles()]
    return "Available roles (set each TaskAssignment.assignee to one):\n" + "\n".join(lines)


def mode_roster(pack: Pack | None = None) -> str:
    """Valid cognitive-mode names for the planner prompt, including each
    role's ``modes_allow`` restriction so the planner only assigns modes the
    executor will accept (``resolve_modes`` drops disallowed ones)."""
    from lionagi.casts.pattern import list_modes

    text = (
        "Optional per-task cognitive modes (TaskAssignment.modes). Use ONLY these "
        "names, and only when a subtask needs a specific reasoning style — else "
        "leave empty and the role's defaults apply: " + ", ".join(list_modes()) + "."
    )
    known = set(list_modes())
    restricted = []
    for r in available_roles():
        cfg = role_config(r, pack)
        if cfg is not None and cfg.modes_allow:
            # Advertise only names the mode catalog recognizes — resolve_modes
            # existence-checks every mode, so an unknown allowlist entry from a
            # custom pack would be advertised here yet dropped at execution.
            valid = sorted(m for m in cfg.modes_allow if m in known)
            if valid:
                restricted.append(f"{r} accepts only {', '.join(valid)}")
            else:
                restricted.append(f"{r} accepts no per-task modes (leave empty)")
    if restricted:
        text += (
            " Per-role restrictions (modes outside a role's list are dropped at "
            "execution): " + "; ".join(restricted) + "."
        )
    return text


_DEFAULT_PACK_LOADED = False
_DEFAULT_PACK = None


def _default_pack():
    global _DEFAULT_PACK_LOADED, _DEFAULT_PACK
    if not _DEFAULT_PACK_LOADED:
        _DEFAULT_PACK_LOADED = True
        try:
            from importlib.resources import as_file, files

            from lionagi.casts.pack import Pack

            packaged = files("lionagi.casts").joinpath("packs", "default.yaml")
            with as_file(packaged) as p:
                _DEFAULT_PACK = Pack.from_file(p)
        except Exception:
            _DEFAULT_PACK = None
    return _DEFAULT_PACK


def role_config(role: str, pack: Pack | None = None) -> Any:
    """``RoleConfig`` for *role* from *pack* (or the default pack), or None."""
    p = pack if pack is not None else _default_pack()
    return p.config(role) if p else None


def resolve_modes(
    role: str, override: list[str] | None = None, pack: Pack | None = None
) -> list[str]:
    """Cognitive modes for *role*: validated per-task override, else pack defaults.

    A per-task *override* is gated by the role's ``modes_allow`` (empty =
    unrestricted); every mode is existence-checked against the casts roster.
    Invalid/disallowed modes are dropped with a warning.
    """
    import logging

    from lionagi.casts.pattern import Mode

    cfg = role_config(role, pack)
    allow = set(cfg.modes_allow) if (cfg and cfg.modes_allow) else None
    gated = bool(override)
    requested = list(override) if override else (list(cfg.default_modes) if cfg else [])
    log = logging.getLogger("lionagi.cli")
    out: list[str] = []
    for m in requested:
        if gated and allow is not None and m not in allow:
            log.warning(
                "mode %r not permitted for role %r (allow=%s); dropping", m, role, sorted(allow)
            )
            continue
        try:
            Mode.load(m)
        except ValueError:
            log.warning("unknown mode %r for role %r; dropping", m, role)
            continue
        out.append(m)
    return out


def _is_casts_role(role: str) -> bool:
    """True if *role* is a loadable built-in casts Role (vs a user profile)."""
    from lionagi.casts.pattern import Role

    try:
        Role.load(role)
    except ValueError:
        return False
    return True


def casts_role_system(role: str, modes: list[str] | None = None) -> str | None:
    """Composed system message for a casts role, or None if not built-in."""
    from lionagi.casts.pattern import Role

    try:
        r = Role.load(role)
    except ValueError:
        return None
    from lionagi.agent import AgentSpec

    msg = AgentSpec.compose(r, modes=list(modes) if modes else None).build_system_message()
    if not msg:
        return None
    from lionagi.session.prompts import LION_SYSTEM_MESSAGE

    return LION_SYSTEM_MESSAGE.strip() + "\n\n" + msg


EFFORT_GUIDANCE: str = (
    "EFFORT TIERS: Use per-op guidance for behavioral framing. "
    "low=skim structure quickly; medium=careful read; high=thorough "
    "analysis; xhigh=deep multi-step reasoning. Match effort to task weight. "
)

EFFORT_MAP: dict[str, str] = {
    "low": "Skim quickly, structured output.",
    "medium": "Read carefully, balance depth/speed.",
    "high": "Thorough analysis, take your time.",
    "xhigh": "Deep reasoning, maximum effort.",
    "max": "Deep reasoning, maximum effort.",
}


def team_guidance(team_name: str | None) -> str:
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
    """TEAM coordination section to append to worker system prompt, or None."""
    if not team_data:
        return None
    from ._common import TEAM_COORD_SECTION  # avoid import cycle

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


def resolve_worker_spec(
    token: str,
) -> tuple[str, AgentProfile | None]:
    """Resolve a worker token to (model_spec, profile_or_None)."""
    if "/" in token:
        return token, None
    try:
        profile = load_agent_profile(token)
        # No hardcoded fallback — callers apply their own default so it doesn't rot.
        return profile.model or token, profile
    except FileNotFoundError:
        return token, None


@dataclass
class OrchestrationEnv:
    """Shared state and config for one orchestration run."""

    # Persistence + Lion objects
    run: RunDir
    session: Session
    orc_branch: Branch
    builder: OperationGraphBuilder

    orc_profile: AgentProfile | None
    default_model_spec: str

    bare: bool
    effort: str | None
    theme: str | None
    yolo: bool
    bypass: bool
    verbose: bool
    fast: bool
    cwd: str | None
    team_data: dict | None = None

    # In-process team messaging (parallel to team_data's file-based channel).
    # All three are set together when team mode is active; None otherwise.
    exchange: Exchange | None = None
    messenger: LionMessenger | None = None
    roster: dict[str, UUID] | None = None

    # None falls through to the default pack for role_config / resolve_modes.
    pack: Pack | None = None

    # None = no budget configured; workers skip the BUDGET preamble.
    total_budget: int | None = None
    _live_persist: dict | None = field(default=None, repr=False)
    _name_counts: dict[str, int] = field(default_factory=dict)
    _all_names: list[str] = field(default_factory=list)

    def assign_name(self, role: str) -> str:
        self._name_counts[role] = self._name_counts.get(role, 0) + 1
        n = self._name_counts[role]
        name = f"{role}-{n}" if n > 1 else role
        self._all_names.append(name)
        return name

    def register_name(self, name: str) -> None:
        self._all_names.append(name)

    @property
    def all_names(self) -> list[str]:
        return list(self._all_names)


async def setup_orchestration(
    *,
    pattern_name: str,
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
    pack: str | None = None,
) -> OrchestrationEnv:
    """Resolve orchestrator config, allocate run, build branch+session."""
    from lionagi.ln.concurrency.errors import cache_cancelled_exc_class

    cache_cancelled_exc_class()

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
        raise ConfigurationError(
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
    _orc_provider = orc_imodel.endpoint.config.provider
    effort = resolve_persisted_effort(_orc_provider, orc_imodel, effort)
    if cwd:
        orc_imodel.endpoint.config.kwargs.setdefault("repo", Path(cwd))

    run = allocate_run(save_dir=save_dir)
    run.ensure_artifact_root()

    orc_log_config = DataLoggerConfig(auto_save_on_exit=False)
    if orc_profile:
        # User profile: verbatim system prompt, no casts composition.
        orc_branch = Branch(
            chat_model=orc_imodel,
            system=orc_profile.system_prompt,
            log_config=orc_log_config,
            name="orchestrator",
        )
    else:
        # Built-in "orchestrator" casts role via AgentSpec.compose + factory.
        orc_spec = AgentSpec.compose("orchestrator", grant_emissions=False)
        orc_branch = await create_agent(
            orc_spec,
            load_settings=False,
            chat_model=orc_imodel,
            log_config=orc_log_config,
        )
        orc_branch.name = "orchestrator"
    _session_id_env = os.environ.get("LIONAGI_SESSION_ID")
    session = (
        Session(id=_session_id_env, default_branch=orc_branch)
        if _session_id_env
        else Session(default_branch=orc_branch)
    )
    builder = OperationGraphBuilder(pattern_name)

    loaded_pack: Pack | None = None
    if pack:
        from lionagi.casts.pack import Pack as _Pack

        loaded_pack = _Pack.from_file(pack)

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
        pack=loaded_pack,
    )


async def build_worker_branch(
    env: OrchestrationEnv,
    *,
    agent_id: str,
    role: str,
    model_override: str | None = None,
    explicit_name: str | None = None,
    system_prompt_override: str | None = None,
    grant_spawn: bool = False,
    modes: list[str] | None = None,
) -> tuple[Branch, str, AgentProfile | None, bool]:
    """Resolve model/profile/system and build a worker Branch.

    The fourth return value is ``messenger_bound``: True when this worker
    actually got the in-process messenger tool registered (team messaging
    active AND a non-CLI worker), so callers building `operate` DAG nodes
    know to enable action serialization for this branch.
    """
    from ._common import BARE_WORKER_SYSTEM

    # Pack per-role config (ADR-0043): model/effort/modes defaults for casts
    # roles. Ignored in bare mode (workers are the raw CLI spec there).
    w_cfg = None if env.bare else role_config(role, env.pack)

    w_profile: AgentProfile | None = None
    if env.bare:
        w_model = model_override or env.default_model_spec
    else:
        resolved_model, w_profile = resolve_worker_spec(role)
        if model_override:
            w_model = model_override
        elif w_profile:
            w_model = resolved_model
        elif w_cfg and w_cfg.model:
            w_model = w_cfg.model
        else:
            w_model = env.default_model_spec

    w_effort = env.effort
    if not env.bare and not env.effort:
        if w_profile and w_profile.effort:
            w_effort = w_profile.effort
        elif w_cfg and w_cfg.effort:
            w_effort = w_cfg.effort
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
    artifact_dir = env.run.agent_artifact_dir(agent_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    w_imodel.endpoint.config.kwargs["repo"] = artifact_dir
    project_root = str(Path(env.cwd).resolve()) if env.cwd else str(Path.cwd().resolve())
    w_imodel.endpoint.config.kwargs.setdefault("add_dir", [])
    if project_root not in w_imodel.endpoint.config.kwargs["add_dir"]:
        w_imodel.endpoint.config.kwargs["add_dir"].append(project_root)

    if explicit_name is not None:
        env.register_name(explicit_name)
        wname = explicit_name
    else:
        wname = env.assign_name(role)

    resolved_modes = [] if env.bare else resolve_modes(role, modes, env.pack)
    team_section = team_worker_system(env.team_data, wname)

    # Casts-role workers route through the factory; verbatim-prompt workers set
    # the string directly (no Role to compose from).
    verbatim_system: str | None = None
    if system_prompt_override is not None:
        verbatim_system = system_prompt_override
    elif not env.bare and w_profile and w_profile.system_prompt:
        verbatim_system = w_profile.system_prompt
    elif env.bare or not _is_casts_role(role):
        verbatim_system = BARE_WORKER_SYSTEM

    log_config = DataLoggerConfig(auto_save_on_exit=False)
    if verbatim_system is None:
        # Casts-role path: factory prepends LION_SYSTEM and renders the policy
        # block; grant_emissions off — spawn rights granted below if needed.
        spec = AgentSpec.compose(
            role,
            modes=resolved_modes,
            grant_emissions=False,
            system_prompt=team_section,
        )
        wb = await create_agent(
            spec,
            load_settings=False,
            chat_model=w_imodel,
            log_config=log_config,
        )
        wb.name = wname
    else:
        w_system = f"{verbatim_system}\n\n{team_section}" if team_section else verbatim_system
        wb = Branch(
            chat_model=w_imodel,
            system=w_system,
            log_config=log_config,
            name=wname,
        )

    env.session.include_branches(wb)

    if grant_spawn:
        from lionagi.orchestration import grant_spawn as _grant_spawn

        _grant_spawn(wb)

    if env._live_persist:
        register_branch_hook(env._live_persist, wb)

    # In-process team messaging: only API-model workers can call tools
    # (operate() only surfaces branch.acts for non-CLI providers); CLI
    # workers keep the existing file-based `li team` channel untouched.
    exchange = getattr(env, "exchange", None)
    messenger = getattr(env, "messenger", None)
    messenger_bound = False
    if exchange is not None and messenger is not None and not getattr(w_imodel, "is_cli", False):
        exchange.register(wb.id)
        env.roster[wname] = wb.id
        msg_tool = messenger.bind(wb, env.roster, sender_name=wname)
        wb.register_tools(msg_tool)
        messenger_bound = True

    return wb, w_model, w_profile, messenger_bound


def make_help_coordinator(env: OrchestrationEnv) -> Any:
    """Build the rung-2 coordinator callback for ``LionMessenger``'s "help" event.

    Plain Python routing logic — no LLM call, matching the shape
    ``ReactiveExecutor._schedule_escalation`` already uses for flow-mode.
    Every help signal is logged for bring-up visibility; a "blocked"-urgency
    signal is additionally folded into ``env._escalated_evidence``, the same
    list the human rung already surfaces post-hoc in the run summary (rung 4
    stays post-hoc by default). Model-bump (rung 3) and
    synchronous human paging are out of scope for this coordinator.
    """

    def _on_help(*, name: str, sender_id: Any, reason: str, urgency: str = "fyi") -> None:
        _log_orch.info(
            "help signal from %s (urgency=%s): %s",
            name,
            urgency,
            reason,
        )
        if urgency == "blocked":
            entry = {"kind": "help_signal", "id": name, "label": reason}
            existing = getattr(env, "_escalated_evidence", None) or []
            env._escalated_evidence = [*existing, entry]

    return _on_help


def finalize_orchestration(
    env: OrchestrationEnv,
    *,
    kind: str,
    prompt: str,
    extras: dict | None = None,
    emit_hints: bool = True,
) -> tuple[list[tuple[str, str, str]], str]:
    """Persist branch snapshots + last-branch pointer + hints."""
    env.run.ensure_state_dirs()
    log = logging.getLogger("lionagi.cli")

    branch_ids: list[tuple[str, str, str]] = []
    for branch in env.session.branches:
        provider = branch.chat_model.endpoint.config.provider
        branch_ids.append((provider, str(branch.id), branch.name))

        # Snapshot failure must not abort finalize; only `li agent -r` is affected.
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


_log_orch = logging.getLogger("lionagi.cli")


async def setup_orchestration_persist(
    session: Any,
    *,
    invocation_kind: str | None = None,
    playbook_name: str | None = None,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
    branches: list[Any] | None = None,
    extra_node_metadata: dict | None = None,
) -> dict | None:
    db = None
    try:
        db = await _open_shared_db()

        session_id = str(session.id)
        session_dict = session.to_dict(mode="db")

        session_prog_id = str(uuid.uuid4())
        await db.create_progression(session_prog_id)

        _proj, _proj_src = _resolve_project(project)
        from lionagi.cli.kill import current_pid_markers as _pid_markers

        _identity_markers = _pid_markers()
        _node_meta = {
            **(session_dict.get("node_metadata") or {}),
            **_identity_markers,
            **(extra_node_metadata or {}),
        }
        await db.create_session(
            {
                "id": session_id,
                "created_at": session_dict["created_at"],
                "node_metadata": _node_meta,
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
                "invocation_id": invocation_id,
                "model": _provenance.resolve_model_spec(provider, model),
                "provider": provider,
                "effort": effort,
                "agent_hash": _provenance.agent_definition_hash(agent_name),
                "project": _proj,
                "project_source": _proj_src,
            }
        )

        ctx: dict[str, Any] = {
            "db": db,
            "session": session,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_ids": {},
            "hooks": [],
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
            "identity_markers": _identity_markers,
        }

        # Bind to the already-open DB so signals write without a per-signal open.
        session.observer.bind_db_persistence(session_id, db=db)

        for branch in branches or []:
            register_branch_hook(ctx, branch)

        return ctx
    except Exception as exc:
        _log_orch.warning(
            "live persist setup failed (%s) — disabling persistence for this run",
            exc,
            exc_info=True,
        )
        if db is not None:
            try:
                await db.close()
            except Exception as close_exc:
                _log_orch.warning(
                    "fallback db.close after setup failure also failed: %s", close_exc
                )
            # Drop the now-closed handle so get_shared_db() can't hand it out.
            from lionagi.state.db import unregister_shared_db

            unregister_shared_db(db)
        return None


def register_branch_hook(ctx: dict[str, Any], branch: Any) -> None:
    from lionagi.ln.concurrency import Lock

    db = ctx["db"]
    session_id = ctx["session_id"]
    session_prog_id = ctx["session_prog_id"]
    branch_id = str(branch.id)

    branch_prog_id = str(uuid.uuid4())
    ctx["branch_prog_ids"][branch_id] = branch_prog_id
    initialized = {"done": False}
    init_lock = Lock()

    async def _ensure_branch_row():
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

            br_model: str | None = None
            br_provider: str | None = None
            try:
                from lionagi.state import provenance as _provenance

                ep_cfg = branch.chat_model.endpoint.config
                br_provider = getattr(ep_cfg, "provider", None)
                br_model_raw = (ep_cfg.kwargs or {}).get("model")
                br_model = _provenance.resolve_model_spec(br_provider, br_model_raw)
            except Exception as _prov_exc:
                _log_orch.debug("branch provenance lookup failed for %s: %s", branch_id, _prov_exc)

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
                    "agent_name": branch_dict.get("name"),
                }
            )
            initialized["done"] = True

    _on_message = _make_message_handler(
        db,
        branch_id,
        session_id,
        branch_prog_id,
        session_prog_id,
        on_first_msg=_ensure_branch_row,
    )

    from lionagi.hooks import route_message_persistence

    handler = route_message_persistence(ctx["session"], branch, _on_message)
    ctx["hooks"].append((branch, handler))


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
    extra_node_metadata: dict | None = None,
) -> None:
    ctx = await setup_orchestration_persist(
        env.session,
        invocation_kind=invocation_kind,
        playbook_name=playbook_name,
        agent_name=agent_name,
        artifacts_path=artifacts_path,
        artifact_contract=artifact_contract,
        invocation_id=invocation_id,
        model=model,
        provider=provider,
        effort=effort,
        project=project,
        branches=list(env.session.branches),
        extra_node_metadata=extra_node_metadata,
    )
    env._live_persist = ctx


async def stop_live_persist(
    env: OrchestrationEnv,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
) -> str:
    ctx = env._live_persist
    extras = getattr(env, "_finalize_extras", None)
    escalated_evidence = getattr(env, "_escalated_evidence", None)
    final_status = await teardown_persist(
        ctx,
        status=status,
        exception=exception,
        extras=extras,
        escalated_evidence=escalated_evidence,
        cwd=env.cwd,
    )
    env._live_persist = None
    return final_status

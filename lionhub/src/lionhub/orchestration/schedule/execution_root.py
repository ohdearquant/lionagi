from __future__ import annotations

import logging
import os
from pathlib import Path

_engine_log = logging.getLogger("lionagi.studio.scheduler.engine")


class SchedulerCwdInheritRefusedError(RuntimeError):
    """A schedule with an explicit execution root resolved none of its directories."""

    def __init__(
        self,
        schedule_id: str | None,
        configured_root: str | None,
        daemon_cwd: str,
    ) -> None:
        self.schedule_id = schedule_id
        self.configured_root = configured_root
        self.daemon_cwd = daemon_cwd
        super().__init__(
            f"Schedule {schedule_id}: configured execution root "
            f"{configured_root!r} could not be resolved to an existing "
            f"directory, and the only remaining fallback is inheriting the "
            f"daemon working directory {daemon_cwd!r}. Refusing to run the "
            f"scheduled action under a substituted working directory; point "
            f"this schedule at an existing action_cwd/action_project. "
            f"LIONAGI_SCHEDULER_CWD does not apply to a schedule that carries "
            f"its own execution root."
        )


def _is_usable_execution_root(root: str | None) -> bool:
    """A usable execution root is an existing absolute directory."""
    if not root:
        return False
    path = Path(root)
    return path.is_absolute() and path.is_dir()


async def _resolve_action_cwd(schedule: dict) -> str | None:
    """Resolve the working directory for a scheduled subprocess spawn."""
    action_cwd = schedule.get("action_cwd")
    if action_cwd:
        if _is_usable_execution_root(action_cwd):
            return action_cwd
        _engine_log.warning(
            "Schedule %s: persisted execution root %r is not usable -- it must "
            "be an existing absolute directory. It may be a pruned worktree, "
            "or a relative path, which would resolve against the daemon's own "
            "cwd rather than the configured root. Trying action_project, then "
            "refusing rather than spawning into a missing or substituted "
            "directory.",
            schedule.get("id"),
            action_cwd,
        )
    elif action_cwd is not None:
        # Present-but-empty root: the truthiness check above is deliberate,
        # since Path("") is Path(".") and would otherwise pass as usable.
        _engine_log.warning(
            "Schedule %s: persisted execution root is empty, which is not a "
            "usable directory; trying action_project, then refusing rather "
            "than spawning into a missing or substituted directory.",
            schedule.get("id"),
        )

    action_project = schedule.get("action_project")
    if action_project:
        from lionhub.orchestration.project_store import get_project

        project = await get_project(action_project)
        if project:
            path = project.get("path")
            if path:
                if _is_usable_execution_root(path):
                    return path
                _engine_log.warning(
                    "Schedule %s: action_project %r is registered at %r, which "
                    "is not a usable execution root -- it must be an existing "
                    "absolute directory. The path may no longer exist (e.g. a "
                    "pruned worktree), or be relative, which would resolve "
                    "against the daemon's own cwd. Registered project paths "
                    "are not validated on the way in, so this is checked here. "
                    "Refusing rather than spawning into a missing or "
                    "substituted directory.",
                    schedule.get("id"),
                    action_project,
                    path,
                )

    if action_cwd is not None or action_project is not None:
        # Gate on `is not None`, not truthiness, so a present-but-empty root fails closed here too
        # rather than falling into the ownerless branch below.
        raise SchedulerCwdInheritRefusedError(
            schedule_id=schedule.get("id"),
            configured_root=action_cwd if action_cwd is not None else action_project,
            daemon_cwd=str(Path.cwd()),
        )

    # Ownerless (pre-migration) rows only: fall back to an operator-set default.
    env_cwd = os.environ.get("LIONAGI_SCHEDULER_CWD")
    if _is_usable_execution_root(env_cwd):
        return env_cwd

    _engine_log.warning(
        "Schedule %s has no persisted execution root (action_cwd) -- a "
        "pre-migration row -- and no action_project or LIONAGI_SCHEDULER_CWD "
        "resolved either; the scheduled action will inherit the daemon's own "
        "working directory and may fail to spawn (`uv run li` finds no "
        "project) if that directory has none. DEPRECATED: this schedule "
        "should be backfilled (restart the daemon) or updated with an "
        "explicit execution root.",
        schedule.get("id"),
    )
    return None

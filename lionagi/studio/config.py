from __future__ import annotations

import os
from pathlib import Path


def _system_local_tz_name() -> str:
    """Best-effort resolution of the system's IANA timezone name.

    Checks ``$TZ`` first, then falls back to reading the ``/etc/localtime``
    symlink (the standard way Unix hosts point at their zoneinfo entry).
    Returns "UTC" if neither resolves — the daemon still runs correctly,
    just without local-time cron semantics until LIONAGI_SCHEDULER_TZ or the
    host's timezone is configured.
    """
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env
    try:
        localtime = Path("/etc/localtime").resolve()
    except OSError:
        return "UTC"
    parts = localtime.parts
    if "zoneinfo" in parts:
        idx = parts.index("zoneinfo")
        return "/".join(parts[idx + 1 :])
    return "UTC"


STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
DATA_ROOT: Path = Path(os.environ.get("LIONAGI_DATA_ROOT", "~/.lionagi")).expanduser()
SHOWS_ROOT: Path = Path(os.environ.get("LIONAGI_SHOWS_ROOT", "~/khive-work/shows")).expanduser()

_raw_origins = os.environ.get("CORS_ORIGINS", "")
CORS_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:3765",
        # The hosted static SPA (lion-studio.khive.ai) drives a user's local
        # daemon from a browser tab on this origin; an exact https origin,
        # never a wildcard or subdomain pattern.
        "https://lion-studio.khive.ai",
    ]
)

# ── Launch admission config ───────────────────────────────────────────────────
# Maximum number of on-demand launch tasks that may run in parallel.
# When saturated, POST /api/launches returns 429.
MAX_LAUNCHES: int = int(os.environ.get("LIONAGI_STUDIO_MAX_LAUNCHES", "4"))

# ── Lifecycle reaper config ───────────────────────────────────────────────────
# Default invocation deadline in seconds (2 hours). Override per action kind
# via LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS (e.g. _AGENT_SECONDS).
INVOCATION_DEADLINE_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS", "7200")
)
# Grace period before a running invocation with zero child sessions is reaped.
ZERO_SESSION_GRACE_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_ZERO_SESSION_GRACE_SECONDS", "300")
)
# Staleness threshold for phantom session classification.
PHANTOM_STALE_HOURS: float = float(os.environ.get("LIONAGI_STUDIO_PHANTOM_STALE_HOURS", "1.0"))
# Minimum seconds between consecutive periodic reaper runs (throttle).
REAPER_INTERVAL_SECONDS: int = int(os.environ.get("LIONAGI_STUDIO_REAPER_INTERVAL_SECONDS", "300"))

# ── Scheduler cron timezone ───────────────────────────────────────────────────
# Cron expressions (trigger_type="cron") are interpreted in this IANA timezone;
# the stored next_fire_at column always remains a UTC epoch regardless. Defaults
# to the system's local timezone (resolved from $TZ, else /etc/localtime);
# override for deployments where the daemon host's timezone doesn't match
# operator intent.
SCHEDULER_TZ: str = os.environ.get("LIONAGI_SCHEDULER_TZ") or _system_local_tz_name()

# ── DB maintenance config ─────────────────────────────────────────────────────
# Size threshold in bytes above which /api/stats raises a size_alert (500 MB).
DB_SIZE_ALERT_BYTES: int = int(
    os.environ.get("LIONAGI_STUDIO_DB_SIZE_ALERT_BYTES", str(500 * 1024 * 1024))
)
# Minimum seconds between automatic WAL checkpoints from the scheduler tick.
CHECKPOINT_INTERVAL_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_CHECKPOINT_INTERVAL_SECONDS", "3600")
)
# Sessions/runs older than this many days (with terminal status) will be pruned.
PRUNE_KEEP_DAYS: int = int(os.environ.get("LIONAGI_STUDIO_PRUNE_KEEP_DAYS", "30"))

# ── Ambient Claude Code mirror ────────────────────────────────────────────────
# When on, studio tails ~/.claude/projects in-process so Claude Code sessions show
# up (and stream live) without a separate `li mirror`. Bounded by the window below,
# so startup catches up the recent window only and never backfills full history.
MIRROR_CLAUDE_ENABLED: bool = os.environ.get(
    "LIONAGI_STUDIO_MIRROR_CLAUDE", "1"
).strip().lower() not in ("0", "false", "no", "off", "")
MIRROR_CLAUDE_SINCE: str = os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_SINCE", "24h")
MIRROR_CLAUDE_INTERVAL: float = float(os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_INTERVAL", "5"))

from __future__ import annotations

import os
from pathlib import Path

STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
DATA_ROOT: Path = Path(os.environ.get("LIONAGI_DATA_ROOT", "~/.lionagi")).expanduser()
SHOWS_ROOT: Path = Path(os.environ.get("LIONAGI_SHOWS_ROOT", "~/khive-work/shows")).expanduser()

_raw_origins = os.environ.get("CORS_ORIGINS", "")
CORS_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:5173", "http://localhost:3000", "http://localhost:3765"]
)

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

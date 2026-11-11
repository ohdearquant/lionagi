from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lionhub.orchestration.schedule.timing import (
    _SCHEDULER_TZ_RESOLUTION,  # noqa: F401
    SCHEDULER_TZ,  # noqa: F401
    SCHEDULER_TZ_ENV_VAR,  # noqa: F401
    SCHEDULER_TZ_RESOLVED_AT,  # noqa: F401
    SCHEDULER_TZ_SOURCE,  # noqa: F401
    SCHEDULER_TZ_SOURCE_DETAIL,  # noqa: F401
    SYSTEM_LOCALTIME_LINK,  # noqa: F401
    TZ_SOURCE_SCHEDULE_DECLARED,  # noqa: F401
    TZ_SOURCE_SCHEDULER_ENV,  # noqa: F401
    TZ_SOURCE_SYSTEM_LOCALTIME,  # noqa: F401
    TZ_SOURCE_TZ_ENV,  # noqa: F401
    TZ_SOURCE_UTC_FALLBACK,  # noqa: F401
    TZ_SOURCE_UTC_UNLOADABLE_NAME,  # noqa: F401
    TZ_UTC_FALLBACK_SOURCES,  # noqa: F401
    SystemTimezoneUnreadableError,  # noqa: F401
    TimezoneResolution,  # noqa: F401
    _as_utc_iso,  # noqa: F401
    _localtime_is_readable,  # noqa: F401
    _named_zone_rules,  # noqa: F401
    _path_zone_rules,  # noqa: F401
    _process_started_at,  # noqa: F401
    _resolve_scheduler_tz,  # noqa: F401
    _resolve_system_local_tz,  # noqa: F401
    _resolved,  # noqa: F401
    _system_local_tz_name,  # noqa: F401
    _tz_search_roots,  # noqa: F401
    _valid_zone_key,  # noqa: F401
    _zone_file_for_name,  # noqa: F401
    _zone_key_is_unambiguous,  # noqa: F401
    _zone_name_from_link_structure,  # noqa: F401
    _zone_name_from_path,  # noqa: F401
    scheduler_timezone_report,  # noqa: F401
)
from lionhub.orchestration.scheduler.settings import (
    _DB_BYTES_PER_RETAINED_DAY,  # noqa: F401 -- preserve the compatibility import
    _DB_SIZE_ALERT_FLOOR_BYTES,  # noqa: F401 -- preserve the compatibility import
    _DB_SIZE_ALERT_HEADROOM,  # noqa: F401 -- preserve the compatibility import
    _FALSE_FLAG_VALUES,  # noqa: F401 -- preserve the compatibility import
    _PRUNE_ARCHIVE_DIR_RAW,  # noqa: F401 -- preserve the compatibility import
    _TRUE_FLAG_VALUES,  # noqa: F401 -- preserve the compatibility import
    CHECKPOINT_INTERVAL_SECONDS,  # noqa: F401 -- preserve the compatibility import
    DB_SIZE_ALERT_BYTES,  # noqa: F401 -- preserve the compatibility import
    DISPATCH_RETENTION_DEAD_LETTER_DAYS,  # noqa: F401 -- preserve the compatibility import
    DISPATCH_RETENTION_SUCCESS_DAYS,  # noqa: F401 -- preserve the compatibility import
    INVOCATION_DEADLINE_SECONDS,  # noqa: F401 -- preserve the compatibility import
    MAX_ADHOC_CONCURRENT,  # noqa: F401 -- preserve the compatibility import
    MAX_LAUNCHES,  # noqa: F401 -- preserve the compatibility import
    MAX_SCHEDULED_CONCURRENT,  # noqa: F401 -- preserve the compatibility import
    PHANTOM_STALE_HOURS,  # noqa: F401 -- preserve the compatibility import
    PLAY_STALE_HOURS,  # noqa: F401 -- preserve the compatibility import
    PRUNE_ARCHIVE_DIR,  # noqa: F401 -- preserve the compatibility import
    PRUNE_CHUNK_ROWS,  # noqa: F401 -- preserve the compatibility import
    PRUNE_KEEP_DAYS,  # noqa: F401 -- preserve the compatibility import
    REAPER_INTERVAL_SECONDS,  # noqa: F401 -- preserve the compatibility import
    RETENTION_INTERVAL_SECONDS,  # noqa: F401 -- preserve the compatibility import
    SCHEDULE_RUN_STALE_HOURS,  # noqa: F401 -- preserve the compatibility import
    SHOW_STALE_HOURS,  # noqa: F401 -- preserve the compatibility import
    SHOWS_ROOT,  # noqa: F401 -- preserve the compatibility import
    TERMINAL_RECONCILE_BUDGET_SECONDS,  # noqa: F401 -- preserve the compatibility import
    TERMINAL_RECONCILE_ENABLED,  # noqa: F401 -- preserve the compatibility import
    ZERO_SESSION_GRACE_SECONDS,  # noqa: F401 -- preserve the compatibility import
    _derive_db_size_alert_bytes,  # noqa: F401 -- preserve the compatibility import
    _env_flag,  # noqa: F401 -- preserve the compatibility import
    _positive_deadline_seconds,  # noqa: F401 -- preserve the compatibility import
    invocation_deadline_seconds,  # noqa: F401 -- preserve the compatibility import
)

# The preview limit is read in one place so the mirror service and the studio agree on it.
from lionhub.serve.mirror_settings import MIRROR_PREVIEW_CHARS  # noqa: F401

STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")

OPERATOR_CWD_ENV_VAR = "LIONAGI_STUDIO_OPERATOR_CWD"
OPERATOR_CWD_DEFAULT: Path = Path.home().resolve()
OPERATOR_CWD_RULE_ENV = f"env:{OPERATOR_CWD_ENV_VAR}"
OPERATOR_CWD_RULE_DEFAULT = "daemon-config-default:user-home"


@dataclass(frozen=True, slots=True)
class OperatorExecutionRootResolution:
    root: Path | None
    rule: str
    configured_value: str


def _usable_operator_execution_root(value: str | Path) -> Path | None:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        return None
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def resolve_operator_execution_root_config() -> OperatorExecutionRootResolution:
    """Freeze the daemon-wide Operator root choice and its provenance."""
    configured = os.environ.get(OPERATOR_CWD_ENV_VAR)
    if configured is not None:
        return OperatorExecutionRootResolution(
            root=_usable_operator_execution_root(configured),
            rule=OPERATOR_CWD_RULE_ENV,
            configured_value=configured,
        )

    default = str(OPERATOR_CWD_DEFAULT)
    return OperatorExecutionRootResolution(
        root=_usable_operator_execution_root(default),
        rule=OPERATOR_CWD_RULE_DEFAULT,
        configured_value=default,
    )


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


# Ambient transcript mirror
# When on, studio tails the local agent transcript trees in-process so those
# sessions show up (and stream live) without a separate `li mirror`. Bounded by
# the window below, so startup catches up the recent window only and never
# backfills full history — which matters most for codex, whose rollout corpus
# runs to tens of thousands of files.


def _mirror_import_ambient_default() -> bool:
    """Whether an unconfigured mirror may read the user's CLI transcript trees.

    The conventional ``~/.lionagi`` profile shares the same user boundary as
    ``~/.claude`` and ``~/.codex``. An explicitly selected ``LIONAGI_HOME`` is
    isolated unless the operator opts back in. Resolution failures fail closed.
    """
    configured_home = os.environ.get("LIONAGI_HOME")
    if configured_home is None:
        return True
    try:
        selected = Path(configured_home).expanduser().resolve()
        ambient = (Path.home() / ".lionagi").resolve()
    except (OSError, RuntimeError):
        return False
    return selected == ambient


def _optional_mirror_root(env_var: str) -> Path | None:
    """Resolve an optional transcript root without echoing its value on error."""
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        raise ValueError(f"{env_var} could not be resolved") from None


MIRROR_CLAUDE_ENABLED: bool = _env_flag("LIONAGI_STUDIO_MIRROR_CLAUDE", default=True)
MIRROR_CLAUDE_SINCE: str = os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_SINCE", "24h")
MIRROR_CLAUDE_INTERVAL: float = float(os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_INTERVAL", "5"))
# Which transcript trees the ambient mirror reads: "both", "claude", or "codex".
# An unrecognized value used to fall back to "both", which is the widest of the
# three: a misspelled "claude" silently read the codex tree as well. Refuse it
# instead, for the same reason the flags above refuse one.
_MIRROR_SOURCE_CHOICES = ("both", "claude", "codex")
_MIRROR_SOURCE_RAW: str = os.environ.get("LIONAGI_STUDIO_MIRROR_SOURCE", "both").strip().lower()
if _MIRROR_SOURCE_RAW not in _MIRROR_SOURCE_CHOICES:
    raise ValueError(
        f"LIONAGI_STUDIO_MIRROR_SOURCE must be one of {list(_MIRROR_SOURCE_CHOICES)}, "
        f"got {_MIRROR_SOURCE_RAW!r}"
    )
MIRROR_SOURCE: str = _MIRROR_SOURCE_RAW
MIRROR_CLAUDE_ROOT: Path | None = _optional_mirror_root("LIONAGI_STUDIO_MIRROR_CLAUDE_ROOT")
MIRROR_CODEX_ROOT: Path | None = _optional_mirror_root("LIONAGI_STUDIO_MIRROR_CODEX_ROOT")
MIRROR_IMPORT_AMBIENT: bool = _env_flag(
    "LIONAGI_STUDIO_MIRROR_IMPORT_AMBIENT", default=_mirror_import_ambient_default()
)

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

# The daemon's own settings live in lionhub; these names stay importable from here.
from lionhub.serve.daemon.settings import (
    _MIRROR_SOURCE_CHOICES,  # noqa: F401 -- preserve the compatibility import
    _MIRROR_SOURCE_RAW,  # noqa: F401 -- preserve the compatibility import
    CORS_ORIGINS,  # noqa: F401 -- preserve the compatibility import
    HOST,  # noqa: F401 -- preserve the compatibility import
    MIRROR_CLAUDE_ENABLED,  # noqa: F401 -- preserve the compatibility import
    MIRROR_CLAUDE_INTERVAL,  # noqa: F401 -- preserve the compatibility import
    MIRROR_CLAUDE_ROOT,  # noqa: F401 -- preserve the compatibility import
    MIRROR_CLAUDE_SINCE,  # noqa: F401 -- preserve the compatibility import
    MIRROR_CODEX_ROOT,  # noqa: F401 -- preserve the compatibility import
    MIRROR_IMPORT_AMBIENT,  # noqa: F401 -- preserve the compatibility import
    MIRROR_SOURCE,  # noqa: F401 -- preserve the compatibility import
    STUDIO_PORT,  # noqa: F401 -- preserve the compatibility import
    _mirror_import_ambient_default,  # noqa: F401 -- preserve the compatibility import
    _optional_mirror_root,  # noqa: F401 -- preserve the compatibility import
    _raw_origins,  # noqa: F401 -- preserve the compatibility import
)

# The preview limit is read in one place so the mirror service and the studio agree on it.
from lionhub.serve.mirror_settings import MIRROR_PREVIEW_CHARS  # noqa: F401

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

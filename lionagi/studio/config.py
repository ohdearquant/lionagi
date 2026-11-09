from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

# The preview limit is read in one place so the mirror service and the studio agree on it.
from lionhub.serve.mirror_settings import MIRROR_PREVIEW_CHARS  # noqa: F401

from .services.schedule_timing import (
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

STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
SHOWS_ROOT: Path = Path(os.environ.get("LIONAGI_SHOWS_ROOT", "~/khive-work/shows")).expanduser()

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

# Launch admission config
# Maximum number of on-demand launch tasks that may run in parallel.
# When saturated, POST /api/launches returns 429.
MAX_LAUNCHES: int = int(os.environ.get("LIONAGI_STUDIO_MAX_LAUNCHES", "4"))

# Maximum concurrent SCHEDULED fires (cron/interval/github_poll/manual-trigger).
# Independent of MAX_LAUNCHES (which caps only the on-demand /api/launches surface).
# 0 = unlimited. When saturated, a due fire defers to the next tick (never dropped).
MAX_SCHEDULED_CONCURRENT: int = int(os.environ.get("LIONAGI_STUDIO_MAX_SCHEDULED_CONCURRENT", "4"))

# Maximum concurrent ad-hoc task-worker executions (lionagi.studio.scheduler.worker),
# reserved from its own capacity pool -- deliberately independent of
# MAX_SCHEDULED_CONCURRENT. A shared cap between the two lanes lets a
# continuously replenished stream of scheduled fires reacquire every freed
# slot before the worker pass gets one, starving ad-hoc work indefinitely.
# 0 = unlimited (worker executions impose no concurrency limit of their own).
MAX_ADHOC_CONCURRENT: int = int(os.environ.get("LIONAGI_STUDIO_MAX_ADHOC_CONCURRENT", "4"))


# Lifecycle reaper config
# Default invocation deadline in seconds (2 hours). Override per action kind
# via LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS (e.g. _AGENT_SECONDS).
# Pairs with per-schedule budget_usd/budget_tokens (schedules table, see
# SchedulerEngine._check_budget): the budget gate is a pre-fire cumulative
# check, not a mid-run kill, so this deadline is what bounds a single run's
# worst-case spend.
def _positive_deadline_seconds(value: str | int | float, *, source: str) -> float:
    """Parse one execution deadline and reject values that cannot bound work."""
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source} must be a positive number of seconds, got {value!r}") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{source} must be positive and finite, got {value!r}")
    return seconds


INVOCATION_DEADLINE_SECONDS: float = _positive_deadline_seconds(
    os.environ.get("LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS", "7200"),
    source="LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS",
)


def invocation_deadline_seconds(
    action_kind: str | None,
    *,
    global_default: float | int | None = None,
) -> float:
    """Resolve and validate the execution deadline for one action kind.

    Per-kind environment overrides are intentionally read at action admission,
    matching the command allow-list's revocation behavior. A malformed or
    non-positive override fails before spawn instead of turning the deadline
    into an unbounded wait.
    """
    default = INVOCATION_DEADLINE_SECONDS if global_default is None else global_default
    default_seconds = _positive_deadline_seconds(
        default,
        source="global invocation deadline",
    )
    if not action_kind:
        return default_seconds
    env_key = f"LIONAGI_STUDIO_INVOCATION_DEADLINE_{action_kind.upper()}_SECONDS"
    raw = os.environ.get(env_key)
    if raw is None:
        return default_seconds
    return _positive_deadline_seconds(raw, source=env_key)


# Grace period before a running invocation with zero child sessions is reaped.
ZERO_SESSION_GRACE_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_ZERO_SESSION_GRACE_SECONDS", "300")
)
# Staleness threshold for phantom session classification.
PHANTOM_STALE_HOURS: float = float(os.environ.get("LIONAGI_STUDIO_PHANTOM_STALE_HOURS", "1.0"))
# Staleness threshold for the play-level reaper. Liveness-first means a play
# whose child session process is still alive is never reaped regardless of
# this value; it only bites orphaned/dead-runner rows.
PLAY_STALE_HOURS: float = float(os.environ.get("LIONAGI_STUDIO_PLAY_STALE_HOURS", "6.0"))
# Staleness threshold for the schedule_run reaper. Unlike sessions/plays,
# there's no per-row process-liveness signal, so this is a pure wall-clock
# backstop for a row stuck at status="running" after a scheduler crash --
# deliberately generous rather than a tight SLA.
SCHEDULE_RUN_STALE_HOURS: float = float(
    os.environ.get("LIONAGI_STUDIO_SCHEDULE_RUN_STALE_HOURS", "24.0")
)
# Staleness threshold for the show-level reaper, which re-derives terminal
# state from on-disk play/verdict evidence since a show's status is derived
# only once, at mirror-row creation. Liveness-first: never reaps a show with
# any child play whose session process is still alive.
SHOW_STALE_HOURS: float = float(os.environ.get("LIONAGI_STUDIO_SHOW_STALE_HOURS", "6.0"))
# Minimum seconds between consecutive periodic reaper runs (throttle).
REAPER_INTERVAL_SECONDS: int = int(os.environ.get("LIONAGI_STUDIO_REAPER_INTERVAL_SECONDS", "300"))
# Seconds one terminal-notification reconciliation pass keeps reading pages of
# owed deliveries. Checked between pages, so a pass can overrun it by up to one page
# of four deliveries (docs/internals/runtime.md). Startup awaits the pass before
# serving and the periodic pass runs inside the scheduler tick; both wait it out.
TERMINAL_RECONCILE_BUDGET_SECONDS: float = float(
    os.environ.get("LIONAGI_STUDIO_TERMINAL_RECONCILE_BUDGET_SECONDS", "10")
)

# DB maintenance config
# Minimum seconds between automatic WAL checkpoints from the scheduler tick.
CHECKPOINT_INTERVAL_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_CHECKPOINT_INTERVAL_SECONDS", "3600")
)
# Sessions/runs older than this many days (with terminal status) will be pruned.
PRUNE_KEEP_DAYS: int = int(os.environ.get("LIONAGI_STUDIO_PRUNE_KEEP_DAYS", "30"))

# Whole-file bytes per retained day, measured on one deployment (~272 MB/day
# over 38 days); a measurement rather than a policy, so re-measure as it decays.
_DB_BYTES_PER_RETAINED_DAY: int = int(
    os.environ.get("LIONAGI_STUDIO_DB_BYTES_PER_RETAINED_DAY", str(272 * 1024 * 1024))
)
# Multiple of steady state above which the size is more than retention explains.
_DB_SIZE_ALERT_HEADROOM: float = 1.5
# Floor, so a short or zero keep window cannot derive a threshold that always alerts.
_DB_SIZE_ALERT_FLOOR_BYTES: int = 512 * 1024 * 1024


def _derive_db_size_alert_bytes(keep_days: int) -> int:
    """Size at which a store is larger than *keep_days* of retention explains."""
    return max(
        _DB_SIZE_ALERT_FLOOR_BYTES,
        int(keep_days * _DB_BYTES_PER_RETAINED_DAY * _DB_SIZE_ALERT_HEADROOM),
    )


# Bytes above which /api/stats raises a size_alert, derived from the retention
# policy so the two cannot disagree and a legitimate steady state cannot fire it.
DB_SIZE_ALERT_BYTES: int = int(
    os.environ.get(
        "LIONAGI_STUDIO_DB_SIZE_ALERT_BYTES",
        str(_derive_db_size_alert_bytes(PRUNE_KEEP_DAYS)),
    )
)
# Directory to archive pruned rows to before deletion. Unset (default) preserves
# the pre-archive prune behaviour exactly. When set, prune refuses to delete any
# row unless the archive for that pass was written and verified first.
_PRUNE_ARCHIVE_DIR_RAW = os.environ.get("LIONAGI_STUDIO_PRUNE_ARCHIVE_DIR", "").strip()
PRUNE_ARCHIVE_DIR: Path | None = (
    Path(_PRUNE_ARCHIVE_DIR_RAW).expanduser() if _PRUNE_ARCHIVE_DIR_RAW else None
)
# Max session ids deleted per committed chunk during prune. Bounds each
# transaction so the write lock is released between chunks and an interrupted
# prune keeps the chunks that already committed.
PRUNE_CHUNK_ROWS: int = max(1, int(os.environ.get("LIONAGI_STUDIO_PRUNE_CHUNK_ROWS", "100")))
# Minimum seconds between automatic retention prunes from the scheduler tick;
# 0 disables the automatic pass and leaves prune to the admin route. The gap is
# measured from when a prune last committed, read back from the admin event log,
# not from when this process started: a daemon restarted more often than the
# interval would otherwise never reach a pass. A database that has never been
# pruned starts its clock at process start rather than firing immediately, so
# adopting this on an installation with a large backlog has a predictable first
# pass instead of one during startup.
RETENTION_INTERVAL_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_RETENTION_INTERVAL_SECONDS", "86400")
)

# dispatch_outbox retention (ADR-0059 delta 3). Two windows: terminal-success
# rows (delivered/acked) are low-signal once past the window, so they use a
# shorter default; dead-lettered/expired rows carry operator-action signal
# (a failure worth investigating) and are kept longer. pending/delivering
# rows are never retention-eligible regardless of these values — they may
# still be claimed or retried by a live scheduler tick.
DISPATCH_RETENTION_SUCCESS_DAYS: int = int(
    os.environ.get("LIONAGI_STUDIO_DISPATCH_RETENTION_SUCCESS_DAYS", "7")
)
DISPATCH_RETENTION_DEAD_LETTER_DAYS: int = int(
    os.environ.get("LIONAGI_STUDIO_DISPATCH_RETENTION_DEAD_LETTER_DAYS", "30")
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


_TRUE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_FLAG_VALUES = frozenset({"0", "false", "no", "off", ""})


def _env_flag(env_var: str, *, default: bool) -> bool:
    """Read a boolean env var, refusing values that are neither true nor false.

    Deciding by exclusion — anything that is not a known false spelling counts
    as true — turns a typo into an opt-in. These flags govern whether Studio
    reads the user's own transcript trees, so the direction a mistake fails in
    is the whole question: "disabled", "none" and "of" all mean off to whoever
    typed them, and all read as on under an exclusion test.
    """
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_FLAG_VALUES:
        return True
    if value in _FALSE_FLAG_VALUES:
        return False
    raise ValueError(
        f"{env_var} must be one of {sorted(_TRUE_FLAG_VALUES)} or "
        f"{sorted(_FALSE_FLAG_VALUES - {''})} (empty means off), got {raw!r}"
    )


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

# Durable replay of terminal notifications into the global settings notifier. Off by
# default: the push path does not yet record what it delivered, so with this on a pass
# replays every pushed notification and each configured notifier runs a second time.
# The consumer API in lionagi.state.lifecycle is unaffected.
TERMINAL_RECONCILE_ENABLED: bool = _env_flag("LIONAGI_STUDIO_TERMINAL_RECONCILE", default=False)

from __future__ import annotations

import logging
import os
import zoneinfo
from pathlib import Path

_logger = logging.getLogger(__name__)

SYSTEM_LOCALTIME_LINK = Path("/etc/localtime")


def _tz_search_roots() -> list[Path]:
    """The zoneinfo directories a zone name is resolved against.

    ``zoneinfo.TZPATH`` is the authority here: it is where the stdlib itself
    looks, so a name expressed relative to one of these roots is a name that
    will actually load. Each entry is included both as written and as it
    resolves, because these are commonly symlinks — on macOS
    ``/usr/share/zoneinfo`` points at ``/usr/share/zoneinfo.default``, and a
    path resolved through the link matches only the second form.
    """
    roots: list[Path] = []
    for entry in zoneinfo.TZPATH:
        candidate = Path(entry)
        for form in (candidate, _resolved(candidate)):
            if form is not None and form not in roots:
                roots.append(form)
    return roots


def _resolved(path: Path) -> Path | None:
    """``path.resolve()``, or None when the filesystem refuses to answer.

    Symlink loops surface as RuntimeError on some Python versions and OSError
    on others, so both are treated as "no answer". This is called while
    computing a module constant at import, where an escaping exception makes
    the package unimportable rather than merely mis-zoned.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _zone_file_for_name(name: str) -> Path | None:
    """The tzfile the stdlib will actually open for *name*.

    ``ZoneInfo`` takes the first match in ``TZPATH`` order, so this walks the
    roots in that same order rather than guessing.
    """
    for entry in zoneinfo.TZPATH:
        candidate = Path(entry) / name
        if candidate.is_file():
            return _resolved(candidate)
    return None


def _zone_name_from_path(path: Path, roots: list[Path]) -> str | None:
    """Express *path* as a zone name that reopens *path*.

    Containment alone is not enough. When several roots are configured, an
    earlier one holding the same key shadows a later one, so a name derived
    from the root that happens to contain the file can load a different
    tzfile with different rules — the same silent-wrong-zone failure this
    resolver exists to prevent, one level in. A candidate is therefore
    accepted only if resolving it the way the stdlib will arrives back at the
    file we started from.
    """
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        name = "/".join(relative.parts)
        if name and _zone_file_for_name(name) == path:
            return name
    return None


def _system_local_tz_name() -> str:
    """Best-effort resolution of the system's IANA timezone name.

    Checks ``$TZ`` first, then reads the ``/etc/localtime`` symlink — the
    standard way Unix hosts point at their zoneinfo entry — and expresses it
    relative to the zoneinfo roots the stdlib searches.

    Deriving the name from the search roots rather than from a directory name
    removes a whole class of guessing: the tree is called ``zoneinfo`` on most
    hosts and something else on others, so any test against the name either
    misses real trees or accepts directories that merely look like one.
    Containment has neither failure, but it is not sufficient on its own —
    with several roots configured, a name the containing root justifies can
    still reopen an earlier root's file, so the name is additionally required
    to round-trip back to the same tzfile.

    Returns "UTC" if nothing resolves to a loadable zone. The daemon still
    runs correctly then, but cron expressions are interpreted in UTC rather
    than local time, so the fallback is logged: an unrequested UTC is
    otherwise indistinguishable from a configured one.
    """
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env

    localtime = _resolved(SYSTEM_LOCALTIME_LINK)
    if localtime is not None:
        name = _zone_name_from_path(localtime, _tz_search_roots())
        if name is not None:
            try:
                zoneinfo.ZoneInfo(name)
            except Exception:  # noqa: BLE001
                # This runs at import to compute a module constant, so nothing
                # may escape: an unreadable or malformed tzfile would otherwise
                # make the package unimportable rather than merely mis-zoned.
                name = None
            if name is not None:
                return name

    _logger.warning(
        "Could not determine the system timezone from %s; scheduler times will "
        "be interpreted as UTC. Set LIONAGI_SCHEDULER_TZ to an IANA zone name "
        "(for example America/New_York) to choose one explicitly.",
        SYSTEM_LOCALTIME_LINK,
    )
    return "UTC"


STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
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

# Maximum concurrent SCHEDULED fires (cron/interval/github_poll/manual-trigger).
# Independent of MAX_LAUNCHES (which caps only the on-demand /api/launches surface).
# 0 = unlimited. When saturated, a due fire defers to the next tick (never dropped).
MAX_SCHEDULED_CONCURRENT: int = int(os.environ.get("LIONAGI_STUDIO_MAX_SCHEDULED_CONCURRENT", "4"))

# ── Lifecycle reaper config ───────────────────────────────────────────────────
# Default invocation deadline in seconds (2 hours). Override per action kind
# via LIONAGI_STUDIO_INVOCATION_DEADLINE_<KIND>_SECONDS (e.g. _AGENT_SECONDS).
# Pairs with per-schedule budget_usd/budget_tokens (schedules table, see
# SchedulerEngine._check_budget): the budget gate is a pre-fire cumulative
# check, not a mid-run kill, so this deadline is what bounds a single run's
# worst-case spend.
INVOCATION_DEADLINE_SECONDS: int = int(
    os.environ.get("LIONAGI_STUDIO_INVOCATION_DEADLINE_SECONDS", "7200")
)
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
# Staleness threshold for the schedule_run reaper -- a schedule_run row can be
# left at status="running" forever when the scheduler process dies after its
# occurrence-insert transaction commits but before its own terminal write
# lands (e.g. mid-spawn). There is no process-liveness signal to check
# against for a schedule_run row the way sessions/plays have (the scheduler
# daemon itself is the "process"; its own restart is what triggers reaping),
# so this is a pure wall-clock backstop, deliberately generous rather than a
# tight SLA.
SCHEDULE_RUN_STALE_HOURS: float = float(
    os.environ.get("LIONAGI_STUDIO_SCHEDULE_RUN_STALE_HOURS", "24.0")
)
# Staleness threshold for the show-level reaper. A show's status is derived
# only once, at mirror-row creation (`shows.import_shows()`); a show
# mirrored while its plays are still in flight is never re-evaluated once
# those plays later merge or abort on disk. This reaper re-derives the
# terminal state from on-disk play/verdict evidence past this staleness
# window. Liveness-first means a show with any child play whose session
# process is still alive is never reaped regardless of this value.
SHOW_STALE_HOURS: float = float(os.environ.get("LIONAGI_STUDIO_SHOW_STALE_HOURS", "6.0"))
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

# ── Ambient Claude Code mirror ────────────────────────────────────────────────
# When on, studio tails ~/.claude/projects in-process so Claude Code sessions show
# up (and stream live) without a separate `li mirror`. Bounded by the window below,
# so startup catches up the recent window only and never backfills full history.
MIRROR_CLAUDE_ENABLED: bool = os.environ.get(
    "LIONAGI_STUDIO_MIRROR_CLAUDE", "1"
).strip().lower() not in ("0", "false", "no", "off", "")
MIRROR_CLAUDE_SINCE: str = os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_SINCE", "24h")
MIRROR_CLAUDE_INTERVAL: float = float(os.environ.get("LIONAGI_STUDIO_MIRROR_CLAUDE_INTERVAL", "5"))

from __future__ import annotations

import logging
import math
import os
import stat
import time
import zipfile
import zlib
import zoneinfo
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

# The preview limit is read in one place so the mirror service and the studio agree on it.
from lionagi.cli._mirror_settings import MIRROR_PREVIEW_CHARS  # noqa: F401

from ._timezone_rules import MAX_TZIF_BYTES, read_tzif, rules_equal, tzif_rules

_logger = logging.getLogger(__name__)

SYSTEM_LOCALTIME_LINK = Path("/etc/localtime")

SCHEDULER_TZ_ENV_VAR = "LIONAGI_SCHEDULER_TZ"

# Where a resolved scheduler zone came from. The name alone is not diagnostic:
# a UTC that an operator asked for and a UTC nothing else could be read are the
# same string, and only the second one means cron rows are firing on an hour
# nobody chose.
TZ_SOURCE_SCHEDULER_ENV = "env:LIONAGI_SCHEDULER_TZ"
TZ_SOURCE_TZ_ENV = "env:TZ"
TZ_SOURCE_SYSTEM_LOCALTIME = "system:localtime"
TZ_SOURCE_UTC_FALLBACK = "fallback:utc"
# A schedule row that names its own zone (the declarative layer's
# ``resolved_timezone``) is resolved in that zone rather than the process-wide
# default, so the two are different provenances for the same effective name.
TZ_SOURCE_SCHEDULE_DECLARED = "schedule:resolved_timezone"
# The requested name — from either of the two sources above — is not a zone
# this host can load, so cron fields are being interpreted in UTC instead of
# the zone that was asked for. Every other UTC in this vocabulary is a UTC
# somebody chose; this one is the only one that means the request was lost.
TZ_SOURCE_UTC_UNLOADABLE_NAME = "fallback:utc:unloadable-name"

# The two sources that mean "UTC because nothing else could be resolved", as
# opposed to "UTC because that is what was asked for".
TZ_UTC_FALLBACK_SOURCES = frozenset({TZ_SOURCE_UTC_FALLBACK, TZ_SOURCE_UTC_UNLOADABLE_NAME})


class SystemTimezoneUnreadableError(RuntimeError):
    """The host's localtime file is readable but has no unambiguous zone name.

    Raised while resolving the scheduler's default zone, which happens once at
    import. A host in this state has a timezone that could not be identified,
    and interpreting cron expressions in UTC instead would shift every fire by
    the host's offset and drop a day from any row scheduled inside it — all of
    it silent. Refusing to start is the loud alternative.
    """


@dataclass(frozen=True)
class TimezoneResolution:
    """A resolved zone name together with the evidence that produced it."""

    name: str
    source: str
    detail: str | None = None


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
    on others, so both are treated as "no answer". A path that cannot even be
    resolved is a host with nothing to say about its zone, not a host whose
    answer was misread.
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
        if os.path.isfile(candidate):
            return _resolved(candidate)
    return None


def _path_zone_rules(path: Path, cache: dict[Path, tuple | None]) -> tuple | None:
    if path not in cache:
        cache[path] = tzif_rules(read_tzif(path))
    return cache[path]


def _valid_zone_key(name: str) -> bool:
    return bool(name) and all(part not in ("", ".", "..") for part in name.split("/"))


def _named_zone_rules(name: str, cache: dict[Path, tuple | None]) -> tuple | None:
    """Read the first configured file, or its public tzdata package resource."""
    if not _valid_zone_key(name):
        return None
    selected = _zone_file_for_name(name)
    if selected is not None:
        return _path_zone_rules(selected, cache)
    try:
        resource = resources.files("tzdata.zoneinfo")
        for part in name.split("/"):
            resource = resource.joinpath(part)
        if isinstance(resource, Path):
            return _path_zone_rules(resource, cache)
        with resource.open("rb") as stream:
            return tzif_rules(stream.read(MAX_TZIF_BYTES + 1))
    except (
        ImportError,
        OSError,
        ValueError,
        RuntimeError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        # A zipped package can fail on a member with a failed checksum, a
        # compressed stream the reader cannot handle (``NotImplementedError``,
        # a ``RuntimeError``), or a member it refuses to open because it is
        # encrypted. None of those is evidence about the host's zone.
        return None


def _zone_key_is_unambiguous(name: str, host_rules: tuple, cache: dict[Path, tuple | None]) -> bool:
    """Check the host's rules against a key and every configured resolved alias."""
    roots = _tz_search_roots()
    pending = [name]
    checked: set[str] = set()
    while pending:
        key = pending.pop()
        if key in checked:
            continue
        checked.add(key)
        files = []
        for entry in zoneinfo.TZPATH:
            candidate = Path(entry) / key
            if os.path.isfile(candidate):
                resolved = _resolved(candidate)
                if resolved is None:
                    return False
                files.append(resolved)
        if not files:
            if not rules_equal(_named_zone_rules(key, cache), host_rules):
                return False
        elif any(not rules_equal(_path_zone_rules(path, cache), host_rules) for path in files):
            return False
        for path in files:
            for root in roots:
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                resolved_key = "/".join(relative.parts)
                if resolved_key and resolved_key not in checked:
                    pending.append(resolved_key)
    return True


def _zone_name_from_path(
    path: Path, roots: list[Path], cache: dict[Path, tuple | None] | None = None
) -> str | None:
    """Express *path* as a zone name that loads the same timezone rules.

    Containment alone isn't enough: with several roots configured, an
    earlier one holding the same key can shadow a later one, so a candidate
    is accepted only if resolving it the way the stdlib will arrives back at
    rules the host reads identically: the same UTC offset, daylight offset
    and name at every transition of either file, with the same POSIX footer
    text.
    """
    cache = {} if cache is None else cache
    host_rules = _path_zone_rules(path, cache)
    if host_rules is None:
        return None
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        name = "/".join(relative.parts)
        selected = _zone_file_for_name(name) if name else None
        if selected is not None and rules_equal(_path_zone_rules(selected, cache), host_rules):
            return name
    return None


def _zone_name_from_link_structure(
    link: Path, resolved: Path | None, cache: dict[Path, tuple | None] | None = None
) -> str | None:
    """Zone name read from the shape of the localtime link itself.

    Reached when no search-root-relative key safely identifies the target: a Python whose
    ``TZPATH`` points elsewhere (conda and similar builds), or macOS mid
    tzdata-update, where the ``/etc/localtime`` chain and the
    ``/usr/share/zoneinfo`` chain resolve into different versioned trees.
    The name is whatever follows a component spelled exactly ``zoneinfo`` —
    a suffixed directory like ``zoneinfo.backup`` stays a refusal, keeping
    the containment rule's judgment that unrelated lookalike trees are not
    zone sources — and it is accepted only if the stdlib can load it from a
    configured filesystem root or the tzdata package with the host's rules.
    Duplicate names, including a configured alias's resolved key, must agree
    with the host's rules and POSIX footer as those two are compared here.
    """
    cache = {} if cache is None else cache
    host_rules = _path_zone_rules(resolved, cache) if resolved is not None else None
    if host_rules is None:
        return None
    # Resolved first, link text second. The primary path already derives the
    # name from where the link resolves rather than from what it says, and this
    # fallback answers the same question, so the two must not disagree about
    # which spelling wins. They differ whenever the link names an alias -- a
    # localtime pointing at ``zoneinfo/US/Eastern`` that resolves to
    # ``zoneinfo/America/New_York`` -- where both spellings load but only one is
    # the zone the host actually uses. Link text can supply an alias when the
    # resolved spelling is absent or unloadable, but must independently match
    # the actual localtime rules before it can be accepted.
    # An ambiguous resolved spelling is a refusal, never a reason to try an alias.
    candidates: list[Path] = []
    if resolved is not None:
        candidates.append(resolved)
    try:
        from_link_text = link.readlink()
    except OSError:
        pass
    else:
        if from_link_text not in candidates:
            candidates.append(from_link_text)
    for target in candidates:
        parts = target.parts
        for index in range(len(parts) - 1, -1, -1):
            if parts[index] != "zoneinfo":
                continue
            name = "/".join(parts[index + 1 :])
            if not name:
                break
            named_rules = _named_zone_rules(name, cache)
            if named_rules is None:
                break
            if not rules_equal(named_rules, host_rules):
                return None
            if not _zone_key_is_unambiguous(name, host_rules, cache):
                return None
            return name
    return None


def _localtime_is_readable() -> bool:
    """Whether the host's localtime file exists and can actually be read.

    Distinguishes "no zone opinion" (fallback to UTC) from "has an opinion
    that couldn't be read" (raise, don't misread it as UTC). Requires a
    regular file before opening: a FIFO with no writer would block forever
    here, mid-import, with no fallback ever reached.
    """
    try:
        if not stat.S_ISREG(SYSTEM_LOCALTIME_LINK.stat().st_mode):
            return False
        fd = os.open(SYSTEM_LOCALTIME_LINK, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return False
            handle.read(1)
    except OSError:
        return False
    return True


def _resolve_system_local_tz() -> TimezoneResolution:
    """Resolve the system's IANA timezone name, with its provenance.

    Checks ``$TZ`` first, then reads the ``/etc/localtime`` symlink and
    expresses it as a name relative to the zoneinfo roots the stdlib
    searches (see ``_zone_name_from_path``), rather than guessing from a
    directory name.

    Falls back to UTC only when there was nothing to read. When the
    localtime file reads fine but yields no unambiguous loadable zone name, raises
    instead — a silent UTC there would run every cron row on an hour nobody
    chose. ``LIONAGI_SCHEDULER_TZ`` short-circuits this whole path.
    """
    tz_env = os.environ.get("TZ")
    if tz_env:
        return TimezoneResolution(tz_env, TZ_SOURCE_TZ_ENV, "TZ")

    cache: dict[Path, tuple | None] = {}
    localtime = _resolved(SYSTEM_LOCALTIME_LINK)
    if localtime is not None:
        name = _zone_name_from_path(localtime, _tz_search_roots(), cache)
        if name is not None:
            return TimezoneResolution(name, TZ_SOURCE_SYSTEM_LOCALTIME, str(SYSTEM_LOCALTIME_LINK))

    name = _zone_name_from_link_structure(SYSTEM_LOCALTIME_LINK, localtime, cache)
    if name is not None:
        return TimezoneResolution(
            name,
            TZ_SOURCE_SYSTEM_LOCALTIME,
            f"{SYSTEM_LOCALTIME_LINK} (zone name from link path)",
        )

    if _localtime_is_readable():
        raise SystemTimezoneUnreadableError(
            f"{SYSTEM_LOCALTIME_LINK} is readable but its rules could not be matched to a "
            "loadable, unambiguous IANA timezone. Refusing to start rather than "
            "interpreting schedules in an "
            f"unrequested UTC. Set {SCHEDULER_TZ_ENV_VAR} to an IANA zone name "
            "(for example America/New_York) to choose one explicitly."
        )

    _logger.warning(
        "Could not determine the system timezone from %s; scheduler times will "
        "be interpreted as UTC. Set %s to an IANA zone name (for example "
        "America/New_York) to choose one explicitly.",
        SYSTEM_LOCALTIME_LINK,
        SCHEDULER_TZ_ENV_VAR,
    )
    return TimezoneResolution("UTC", TZ_SOURCE_UTC_FALLBACK, str(SYSTEM_LOCALTIME_LINK))


def _system_local_tz_name() -> str:
    return _resolve_system_local_tz().name


def _resolve_scheduler_tz() -> TimezoneResolution:
    """The zone cron expressions are interpreted in, and where it came from.

    An explicit ``LIONAGI_SCHEDULER_TZ`` wins over host detection and is never
    second-guessed, which is what makes it usable as the escape hatch on a
    host whose own zone cannot be read.
    """
    override = os.environ.get(SCHEDULER_TZ_ENV_VAR)
    if override:
        return TimezoneResolution(override, TZ_SOURCE_SCHEDULER_ENV, SCHEDULER_TZ_ENV_VAR)
    return _resolve_system_local_tz()


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

# Scheduler cron timezone
# Cron expressions (trigger_type="cron") are interpreted in this IANA timezone;
# next_fire_at is always stored as a UTC epoch regardless. Resolved once here
# and frozen for the process lifetime, so it's reported on /api/admin/health
# rather than only logged at start.
_SCHEDULER_TZ_RESOLUTION = _resolve_scheduler_tz()
SCHEDULER_TZ: str = _SCHEDULER_TZ_RESOLUTION.name
SCHEDULER_TZ_SOURCE: str = _SCHEDULER_TZ_RESOLUTION.source
SCHEDULER_TZ_SOURCE_DETAIL: str | None = _SCHEDULER_TZ_RESOLUTION.detail
SCHEDULER_TZ_RESOLVED_AT: float = time.time()


def _process_started_at() -> float | None:
    """This process's start time as seconds since the epoch, or None.

    This is a reported wall-clock instant, not a process identity, so it wants
    the plain epoch reading rather than the identity reader in ``ln._proc``:
    that one deliberately returns a value chosen to be comparable rather than
    meaningful, and on some platforms it is relative to boot, which would date
    the daemon to 1970 once handed to :func:`datetime.fromtimestamp`.
    """
    try:
        import psutil

        return psutil.Process().create_time()
    except Exception:  # noqa: BLE001
        return None


def _as_utc_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    from lionagi.ln import coerce_created_at

    return coerce_created_at(epoch).isoformat()


def scheduler_timezone_report() -> dict[str, Any]:
    """The scheduler's effective cron timezone, as this process holds it.

    Reads the module attributes the scheduler itself reads when computing fire
    times, so the report cannot drift into describing a resolution the
    scheduler is not using. Re-deriving the zone here would answer a different
    question — what the host says now — which is exactly the question that
    looks fine while a long-lived daemon carries a stale value.
    """
    return {
        "name": SCHEDULER_TZ,
        "source": SCHEDULER_TZ_SOURCE,
        "source_detail": SCHEDULER_TZ_SOURCE_DETAIL,
        "resolved_at": _as_utc_iso(SCHEDULER_TZ_RESOLVED_AT),
        "daemon_started_at": _as_utc_iso(_process_started_at()),
    }


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

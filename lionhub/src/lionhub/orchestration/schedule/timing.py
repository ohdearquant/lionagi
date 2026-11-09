from __future__ import annotations

import logging
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lionhub.orchestration.schedule._timezone_rules import (
    MAX_TZIF_BYTES,
    read_tzif,
    rules_equal,
    tzif_rules,
)

_logger = logging.getLogger("lionagi.studio.config")

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


_engine_log = logging.getLogger("lionagi.studio.scheduler.engine")


@dataclass(frozen=True)
class ScheduleTimezone:
    """The zone one schedule's cron fields are interpreted in, plus its provenance."""

    name: str
    source: str
    tzinfo: ZoneInfo


def resolve_schedule_timezone(schedule: dict) -> ScheduleTimezone:
    """Resolve the zone *schedule*'s cron expression is interpreted in; a pure read."""
    declared = schedule.get("resolved_timezone")
    if declared:
        requested, source = declared, TZ_SOURCE_SCHEDULE_DECLARED
    else:
        requested, source = SCHEDULER_TZ, SCHEDULER_TZ_SOURCE
    try:
        return ScheduleTimezone(requested, source, ZoneInfo(requested))
    except (ZoneInfoNotFoundError, ValueError):
        _engine_log.warning(
            "Schedule %s: timezone %r (from %s) is not a zone this host can "
            "load; interpreting its cron expression in UTC instead. Every "
            "fire time this schedule computes is shifted by the offset of "
            "the zone that was asked for.",
            schedule.get("id"),
            requested,
            source,
        )
        return ScheduleTimezone("UTC", TZ_SOURCE_UTC_UNLOADABLE_NAME, ZoneInfo("UTC"))


def resolve_schedule_cadence_seconds(schedule: dict) -> float | None:
    """Fixed-period cadence *schedule* fires on, or None if it has none."""
    trigger_type = schedule.get("trigger_type")
    if trigger_type == "interval":
        return schedule.get("interval_sec")
    if trigger_type == "github_poll":
        return schedule.get("poll_interval_sec") or schedule.get("interval_sec") or 300
    return None

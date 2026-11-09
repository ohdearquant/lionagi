"""System timezone resolution for the scheduler's default cron zone.

The scheduler interprets cron expressions in ``SCHEDULER_TZ``, which defaults
to the host's local zone read off ``/etc/localtime``. A misread there is
invisible: every fire still succeeds, just at the wrong hour, so these pin the
read itself rather than the scheduler's use of the result.

Fixtures build a zoneinfo tree under ``tmp_path`` and point the search path at
it with ``zoneinfo.reset_tzpath``. That is the seam that matters: it moves the
private path the loader consults, not just the public constant, so both the
name derivation and the loadability check run against the constructed tree
instead of the host's. Trees are filled with real tzdata bytes for the same
reason, which lets a test give two roots genuinely different rules under one
key.
"""

import logging
import os
import signal
import subprocess
import sys
import zipfile
import zlib
import zoneinfo
from importlib import resources
from pathlib import Path

import pytest
from lionhub_tests.orchestration.schedule import _test_support_config_timezone as _support
from lionhub_tests.orchestration.schedule._test_support_config_timezone import (
    _HOST_TZPATH,
    _Blocked,
    _host_zone_bytes,
)

# Fixtures shared with the support module, bound here so they apply to this module only.
tz_host = _support.tz_host

import lionhub.orchestration.schedule.timing as config


def test_missing_localtime_falls_back_to_utc_with_a_warning(tz_host, caplog):
    """An unrequested UTC is indistinguishable from a configured one, so the
    fallback says so rather than being silent."""
    _link, set_roots, _zone_file = tz_host
    set_roots("zoneinfo")

    with caplog.at_level(logging.WARNING, logger="lionagi.studio.config"):
        assert config._system_local_tz_name() == "UTC"

    assert "LIONAGI_SCHEDULER_TZ" in caplog.text


def test_moved_bindings_reexport_their_owner() -> None:
    import lionhub.orchestration.project_store as project_store
    import lionhub.orchestration.schedule.execution_root as execution_root
    import lionhub.orchestration.schedule.timing as timing
    from lionagi.studio import config as legacy_config
    from lionagi.studio.scheduler import engine
    from lionagi.studio.services import projects

    config_names = (
        "SYSTEM_LOCALTIME_LINK",
        "SCHEDULER_TZ_ENV_VAR",
        "TZ_SOURCE_SCHEDULER_ENV",
        "TZ_SOURCE_TZ_ENV",
        "TZ_SOURCE_SYSTEM_LOCALTIME",
        "TZ_SOURCE_UTC_FALLBACK",
        "TZ_SOURCE_SCHEDULE_DECLARED",
        "TZ_SOURCE_UTC_UNLOADABLE_NAME",
        "TZ_UTC_FALLBACK_SOURCES",
        "SystemTimezoneUnreadableError",
        "TimezoneResolution",
        "_tz_search_roots",
        "_resolved",
        "_zone_file_for_name",
        "_path_zone_rules",
        "_valid_zone_key",
        "_named_zone_rules",
        "_zone_key_is_unambiguous",
        "_zone_name_from_path",
        "_zone_name_from_link_structure",
        "_localtime_is_readable",
        "_resolve_system_local_tz",
        "_system_local_tz_name",
        "_resolve_scheduler_tz",
        "_SCHEDULER_TZ_RESOLUTION",
        "SCHEDULER_TZ",
        "SCHEDULER_TZ_SOURCE",
        "SCHEDULER_TZ_SOURCE_DETAIL",
        "SCHEDULER_TZ_RESOLVED_AT",
        "_process_started_at",
        "_as_utc_iso",
        "scheduler_timezone_report",
    )
    for name in config_names:
        assert getattr(legacy_config, name) is getattr(timing, name), name

    for name in (
        "ScheduleTimezone",
        "resolve_schedule_timezone",
        "resolve_schedule_cadence_seconds",
    ):
        assert getattr(engine, name) is getattr(timing, name), name

    for name in (
        "SchedulerCwdInheritRefusedError",
        "_is_usable_execution_root",
        "_resolve_action_cwd",
    ):
        assert getattr(engine, name) is getattr(execution_root, name), name

    for name in ("_ENSURE_TABLE_SQL", "_ensure_table", "get_project"):
        assert getattr(projects, name) is getattr(project_store, name), name


def test_service_import_boundaries_in_fresh_processes() -> None:
    import json

    service_modules = (
        "lionhub.orchestration.schedule.timing",
        "lionhub.orchestration.project_store",
        "lionhub.orchestration.schedule.execution_root",
    )
    forbidden = {
        "lionagi.studio.scheduler.engine",
        "lionagi.studio.scheduler.subprocess",
        "lionagi.studio.scheduler.github",
        "lionagi.studio.scheduler.admit",
        "lionagi.studio.app",
        "lionagi.studio.services.projects",
    }
    probe = """\
import importlib, json, sys
module = sys.argv[1]
importlib.import_module(module)
forbidden = set(json.loads(sys.argv[2]))
if module.endswith("project_store"):
    forbidden.update({"fastapi", "pydantic"})
loaded = sorted(forbidden.intersection(sys.modules))
print(json.dumps(loaded))
assert not loaded, loaded
"""
    repo = Path(__file__).resolve().parents[2]
    for module in service_modules:
        result = subprocess.run(
            [sys.executable, "-c", probe, module, json.dumps(sorted(forbidden))],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    positive = subprocess.run(
        [
            sys.executable,
            "-c",
            "import lionagi.studio.scheduler.engine, sys; assert 'lionagi.studio.scheduler.engine' in sys.modules; print('engine loaded')",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert positive.returncode == 0, positive.stdout + positive.stderr
    assert "engine loaded" in positive.stdout


@pytest.mark.parametrize("config_first", [True, False])
def test_scheduler_timezone_snapshot_is_resolved_once_in_both_import_orders(
    config_first: bool,
) -> None:
    probe = """\
import sys
calls = []
def count(frame, event, arg):
    if event == "call" and frame.f_code.co_name == "_resolve_scheduler_tz" and frame.f_globals.get("__name__") == "lionhub.orchestration.schedule.timing":
        calls.append(frame.f_code.co_name)
sys.setprofile(count)
if sys.argv[1] == "config-first":
    import lionagi.studio.config
    import lionhub.orchestration.schedule.timing
else:
    import lionhub.orchestration.schedule.timing
    import lionagi.studio.config
sys.setprofile(None)
print(f"_resolve_scheduler_tz calls: {len(calls)}")
assert len(calls) == 1, calls
"""
    order = "config-first" if config_first else "timing-first"
    result = subprocess.run(
        [sys.executable, "-c", probe, order],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "_resolve_scheduler_tz calls: 1" in result.stdout

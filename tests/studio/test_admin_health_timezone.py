# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The scheduler's effective cron timezone, reported on /api/admin/health.

``SCHEDULER_TZ`` is resolved once at import and frozen for the life of the
process, so a daemon can carry a zone that neither the source tree nor the
host agrees with any more, and every cron row fires on the wrong hour without
a single error. The only record of what a running scheduler actually decided
used to be one log line at start, which is why these pin the value onto a
live endpoint — and pin that it is the *same* value the scheduler computes
fire times with, not a fresh read that would answer a different question.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import lionagi.state.db as state_db_mod

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from lionagi.studio import config  # noqa: E402

NY = ZoneInfo("America/New_York")


def _install_resolution(monkeypatch: pytest.MonkeyPatch) -> config.TimezoneResolution:
    """Re-run the import-time resolution under the current environment and
    make it this process's value, the way a real daemon start does."""
    resolution = config._resolve_scheduler_tz()
    monkeypatch.setattr(config, "SCHEDULER_TZ", resolution.name)
    monkeypatch.setattr(config, "SCHEDULER_TZ_SOURCE", resolution.source)
    monkeypatch.setattr(config, "SCHEDULER_TZ_SOURCE_DETAIL", resolution.detail)
    return resolution


def _make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    import lionagi.studio.app as app_mod

    fake_db = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", fake_db)

    app = app_mod.create_app()
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8765")


def _health_timezone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    client = _make_client(monkeypatch, tmp_path)
    response = client.get("/api/admin/health")
    assert response.status_code == 200
    return response.json()["scheduler_timezone"]


# Each source is reported distinguishably


def test_explicit_scheduler_env_var_is_reported_as_its_own_source(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONAGI_SCHEDULER_TZ", "Asia/Tokyo")
    _install_resolution(monkeypatch)

    payload = _health_timezone(monkeypatch, tmp_path)

    assert payload["name"] == "Asia/Tokyo"
    assert payload["source"] == config.TZ_SOURCE_SCHEDULER_ENV
    assert payload["source_detail"] == "LIONAGI_SCHEDULER_TZ"


def test_tz_environment_variable_is_reported_as_its_own_source(monkeypatch, tmp_path):
    monkeypatch.delenv("LIONAGI_SCHEDULER_TZ", raising=False)
    monkeypatch.setenv("TZ", "Europe/Berlin")
    _install_resolution(monkeypatch)

    payload = _health_timezone(monkeypatch, tmp_path)

    assert payload["name"] == "Europe/Berlin"
    assert payload["source"] == config.TZ_SOURCE_TZ_ENV
    assert payload["source_detail"] == "TZ"


def test_host_localtime_is_reported_as_its_own_source(monkeypatch, tmp_path, tz_tree):
    """The zone came off the host's own localtime file — nobody asked for it,
    but it was successfully read, which is the difference that matters."""
    monkeypatch.delenv("LIONAGI_SCHEDULER_TZ", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    link = tz_tree("America/New_York")
    _install_resolution(monkeypatch)

    payload = _health_timezone(monkeypatch, tmp_path)

    assert payload["name"] == "America/New_York"
    assert payload["source"] == config.TZ_SOURCE_SYSTEM_LOCALTIME
    assert payload["source_detail"] == str(link)


def test_surrendered_utc_is_distinguishable_from_a_chosen_one(monkeypatch, tmp_path, tz_tree):
    """Both report the name "UTC". Only the source says whether an operator
    chose it or the daemon gave up looking, and that is the whole defect."""
    monkeypatch.delenv("TZ", raising=False)

    monkeypatch.delenv("LIONAGI_SCHEDULER_TZ", raising=False)
    tz_tree(None)
    _install_resolution(monkeypatch)
    surrendered = _health_timezone(monkeypatch, tmp_path)

    monkeypatch.setenv("LIONAGI_SCHEDULER_TZ", "UTC")
    _install_resolution(monkeypatch)
    chosen = _health_timezone(monkeypatch, tmp_path)

    assert surrendered["name"] == chosen["name"] == "UTC"
    assert surrendered["source"] == config.TZ_SOURCE_UTC_FALLBACK
    assert chosen["source"] == config.TZ_SOURCE_SCHEDULER_ENV
    assert surrendered["source"] != chosen["source"]


# The payload carries enough to date the value


def test_payload_carries_resolution_and_process_start_times(monkeypatch, tmp_path):
    """A frozen value is only diagnostic if you can tell when it froze — a
    restart is the only thing that re-reads it."""
    payload = _health_timezone(monkeypatch, tmp_path)

    for key in ("resolved_at", "daemon_started_at"):
        assert payload[key] is not None
        parsed = datetime.fromisoformat(payload[key])
        assert parsed.tzinfo is not None
        assert parsed <= datetime.now(tz=timezone.utc)


# The reported value is the one the scheduler actually uses


def test_reported_zone_is_the_one_fire_times_are_computed_in(monkeypatch, tmp_path):
    """Recomputing the zone at the health endpoint would report what the host
    says now, while the scheduler keeps using what it read at start. The two
    disagree exactly when it matters, so health must read the scheduler's
    value rather than derive its own."""
    pytest.importorskip("croniter", reason="studio extra not installed")
    from lionagi.studio.scheduler.engine import SchedulerEngine

    # A value no environment would produce: if health re-derived the zone from
    # $TZ or /etc/localtime it could not possibly report this one back.
    monkeypatch.setattr(config, "SCHEDULER_TZ", "America/New_York")
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    payload = _health_timezone(monkeypatch, tmp_path)
    assert payload["name"] == "America/New_York"

    svc = AsyncMock()
    svc.update_schedule = AsyncMock()
    engine = SchedulerEngine(svc=svc)
    schedule = {
        "id": "sched-tz",
        "name": "tz-agreement",
        "trigger_type": "cron",
        "cron_expr": "30 6 * * *",
    }

    ref = datetime(2026, 7, 22, 2, 0, 0, tzinfo=NY).timestamp()
    next_at = engine._compute_next_fire(schedule, ref)
    assert next_at is not None

    # 06:30 in the zone health reports — and next_fire_at stays a UTC epoch.
    fired_in_reported_zone = datetime.fromtimestamp(next_at, tz=ZoneInfo(payload["name"]))
    assert (fired_in_reported_zone.hour, fired_in_reported_zone.minute) == (6, 30)

    # The failure this whole report exists to expose: a UTC interpretation of
    # the same expression is a different absolute instant.
    utc_reading = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc).timestamp()
    assert next_at != utc_reading


@pytest.fixture
def tz_tree(tmp_path, monkeypatch):
    """Point the host-localtime read at a constructed tree under tmp_path.

    Yields a callable taking a zone name (or None for a host with no localtime
    file at all) and returning the localtime path the resolver will consult.
    """
    import zoneinfo

    host_tzpath = tuple(zoneinfo.TZPATH)
    link = tmp_path / "localtime"
    monkeypatch.setattr(config, "SYSTEM_LOCALTIME_LINK", link)

    def build(zone: str | None):
        if zone is not None:
            source = next(
                (Path(entry) / zone for entry in host_tzpath if (Path(entry) / zone).is_file()),
                None,
            )
            if source is None:
                pytest.skip(f"no tzfile for {zone} under {host_tzpath}")
            target = tmp_path / "zoneinfo" / zone
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            link.symlink_to(target)
        zoneinfo.reset_tzpath(to=[str(tmp_path / "zoneinfo")])
        zoneinfo.ZoneInfo.clear_cache()
        return link

    yield build

    zoneinfo.reset_tzpath()
    zoneinfo.ZoneInfo.clear_cache()

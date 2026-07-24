"""System timezone resolution for the scheduler's default cron zone.

The scheduler interprets cron expressions in ``SCHEDULER_TZ``, which defaults
to the host's local zone read off ``/etc/localtime``. A misread there is
invisible: every fire still succeeds, just at the wrong hour, so these pin the
read itself rather than the scheduler's use of the result.

Fixtures build a zoneinfo tree under ``tmp_path`` and point the search roots at
it. Zone names are real ones so that the loadability check, which consults the
host's actual tzdata, exercises the same path it does in production.
"""

import logging

import pytest

from lionagi.studio import config


@pytest.fixture
def tz_host(tmp_path, monkeypatch):
    """A fake host: a localtime link, and control over the search roots."""
    monkeypatch.delenv("TZ", raising=False)
    link = tmp_path / "localtime"
    monkeypatch.setattr(config, "SYSTEM_LOCALTIME_LINK", link)

    def set_roots(*names):
        monkeypatch.setattr(config.zoneinfo, "TZPATH", tuple(str(tmp_path / n) for n in names))

    def zone_file(tree, zone="America/New_York"):
        path = tmp_path / tree / zone
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    return link, set_roots, zone_file


def test_suffixed_zoneinfo_directory_resolves(tz_host):
    """macOS points /etc/localtime at a tree resolving through
    ``zoneinfo.default``. Deriving the name from the search roots means the
    directory's name never has to be guessed at."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo.default"))
    set_roots("zoneinfo.default")

    assert config._system_local_tz_name() == "America/New_York"


def test_plain_zoneinfo_directory_resolves(tz_host):
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo"))
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "America/New_York"


def test_search_root_reached_through_a_symlink_resolves(tz_host, tmp_path):
    """Search roots are commonly symlinks. A localtime path resolved through
    the link matches only the resolved form of the root, so both are tried."""
    link, set_roots, zone_file = tz_host
    real = zone_file("zoneinfo.default")
    (tmp_path / "zoneinfo").symlink_to(tmp_path / "zoneinfo.default")
    link.symlink_to(real)
    set_roots("zoneinfo")  # the root as configured is the link, not the target

    assert config._system_local_tz_name() == "America/New_York"


def test_directory_that_merely_looks_like_a_tree_is_not_one(tz_host):
    """A directory named like a zoneinfo tree but not among the search roots
    must not produce a zone. Matching on the name would accept it and return a
    loadable but wrong zone, which is worse than returning nothing."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo.backup"))
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"


def test_zone_follows_the_resolved_path_not_the_link_text(tz_host, tmp_path):
    """When the link's text and where it lands disagree, where it lands wins.

    Reading the name off the unresolved text would report the zone the path is
    named after rather than the zone the host actually uses.
    """
    link, set_roots, zone_file = tz_host
    london = zone_file("zoneinfo.default", "Europe/London")
    named_new_york = tmp_path / "zoneinfo.default" / "America" / "New_York"
    named_new_york.parent.mkdir(parents=True, exist_ok=True)
    named_new_york.symlink_to(london)
    link.symlink_to(named_new_york)
    set_roots("zoneinfo.default")

    assert config._system_local_tz_name() == "Europe/London"


def test_tz_environment_variable_takes_precedence(tz_host, monkeypatch):
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo"))
    set_roots("zoneinfo")
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    assert config._system_local_tz_name() == "Asia/Tokyo"


def test_missing_link_falls_back_to_utc_with_a_warning(tz_host, caplog):
    _, set_roots, _ = tz_host
    set_roots("zoneinfo")

    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config._system_local_tz_name() == "UTC"

    assert "LIONAGI_SCHEDULER_TZ" in caplog.text


def test_symlink_loop_falls_back_instead_of_raising(tz_host, tmp_path):
    """This runs at import to compute a module constant, so a hostile
    filesystem must not make the package unimportable."""
    link, set_roots, _ = tz_host
    other = tmp_path / "other"
    link.symlink_to(other)
    other.symlink_to(link)
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"


def test_unloadable_zone_name_falls_back_to_utc(tz_host):
    """A name derived from a real search root that no tzdata knows must not be
    handed on; the scheduler could only report the name, not its origin."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo", "Mars/Olympus_Mons"))
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"

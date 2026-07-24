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
import zoneinfo
from pathlib import Path

import pytest

from lionagi.studio import config

# Captured before any test moves the search path, so the real trees stay
# reachable as a source of tzfile bytes.
_HOST_TZPATH = tuple(zoneinfo.TZPATH)


def _host_zone_bytes(zone: str) -> bytes:
    for entry in _HOST_TZPATH:
        candidate = Path(entry) / zone
        if candidate.is_file():
            return candidate.read_bytes()
    raise AssertionError(
        f"no tzfile for {zone} under {_HOST_TZPATH}; these tests build zone "
        "trees from the host's tzdata and cannot run without it"
    )


@pytest.fixture
def tz_host(tmp_path, monkeypatch):
    """A fake host: a localtime link, and control over the search path."""
    monkeypatch.delenv("TZ", raising=False)
    link = tmp_path / "localtime"
    monkeypatch.setattr(config, "SYSTEM_LOCALTIME_LINK", link)

    def set_roots(*names):
        """Point the stdlib's zone search at these trees, in this order."""
        zoneinfo.reset_tzpath(to=[str(tmp_path / n) for n in names])
        zoneinfo.ZoneInfo.clear_cache()

    def zone_file(tree, zone="America/New_York", data_from=None):
        """A real tzfile at *tree*/*zone*, carrying *data_from*'s rules."""
        path = tmp_path / tree / zone
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_host_zone_bytes(data_from or zone))
        return path

    yield link, set_roots, zone_file

    zoneinfo.reset_tzpath()
    zoneinfo.ZoneInfo.clear_cache()


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
    target = zone_file("zoneinfo.default")
    (tmp_path / "zoneinfo").symlink_to(tmp_path / "zoneinfo.default")
    link.symlink_to(target)
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "America/New_York"


def test_multi_level_zone_name_keeps_all_its_parts(tz_host):
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo", "America/Indiana/Knox"))
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "America/Indiana/Knox"


def test_directory_that_merely_looks_like_a_tree_is_not_one(tz_host):
    """A path under some unrelated directory named ``zoneinfo.backup`` is not
    a zone source. Only the configured search roots are."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo.backup"))
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"


def test_zone_follows_the_resolved_path_not_the_link_text(tz_host, tmp_path):
    """The link's text can name one zone while resolving to another. What the
    host actually uses is where it resolves, so that is what is read."""
    link, set_roots, zone_file = tz_host
    zone_file("zoneinfo", "Asia/Tokyo")
    (tmp_path / "zoneinfo" / "America").mkdir(parents=True, exist_ok=True)
    (tmp_path / "zoneinfo" / "America" / "New_York").symlink_to(
        tmp_path / "zoneinfo" / "Asia" / "Tokyo"
    )
    link.symlink_to(tmp_path / "zoneinfo" / "America" / "New_York")
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "Asia/Tokyo"


def test_shadowed_key_is_refused_rather_than_loading_another_zone(tz_host):
    """Two roots can hold the same key with different rules. The name derived
    from the root that contains localtime would be reopened from the earlier
    root, so accepting it would schedule on rules the host does not use."""
    link, set_roots, zone_file = tz_host
    zone_file("first", "America/New_York", data_from="Asia/Tokyo")
    link.symlink_to(zone_file("second", "America/New_York"))
    set_roots("first", "second")

    assert config._system_local_tz_name() == "UTC"


def test_unshadowed_key_still_resolves_with_several_roots(tz_host):
    """The refusal above is about a collision, not about having more than one
    root: a key only the containing root holds still resolves."""
    link, set_roots, zone_file = tz_host
    zone_file("first", "Asia/Tokyo")
    link.symlink_to(zone_file("second", "America/New_York"))
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


def test_unreadable_zone_data_falls_back_instead_of_raising(tz_host, tmp_path):
    """The name is computed at import to build a module constant, so a
    malformed tzfile must not make the package unimportable."""
    link, set_roots, _zone_file = tz_host
    path = tmp_path / "zoneinfo" / "America" / "New_York"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a tzfile")
    link.symlink_to(path)
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"


def test_symlink_loop_falls_back_instead_of_raising(tz_host, tmp_path):
    """A looping localtime link raises from ``resolve()`` rather than
    returning. At import that would take the whole package down."""
    link, set_roots, _zone_file = tz_host
    other = tmp_path / "loop_b"
    link.symlink_to(other)
    other.symlink_to(link)
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"


def test_tz_environment_variable_wins(tz_host, monkeypatch):
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo"))
    set_roots("zoneinfo")
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    assert config._system_local_tz_name() == "Asia/Tokyo"


def test_missing_localtime_falls_back_to_utc_with_a_warning(tz_host, caplog):
    """An unrequested UTC is indistinguishable from a configured one, so the
    fallback says so rather than being silent."""
    _link, set_roots, _zone_file = tz_host
    set_roots("zoneinfo")

    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config._system_local_tz_name() == "UTC"

    assert "LIONAGI_SCHEDULER_TZ" in caplog.text

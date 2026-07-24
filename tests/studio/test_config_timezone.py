"""System timezone resolution for the scheduler's default cron zone.

The scheduler interprets cron expressions in ``SCHEDULER_TZ``, which defaults
to the host's local zone read off ``/etc/localtime``. A misread there is
invisible: every fire still succeeds, just at the wrong hour, so these pin the
read itself rather than the scheduler's use of the result.
"""

import logging
from pathlib import Path

import pytest

from lionagi.studio import config


@pytest.fixture
def localtime_link(tmp_path, monkeypatch):
    """Point the resolver at a symlink under tmp_path instead of /etc."""
    link = tmp_path / "localtime"
    monkeypatch.setattr(config, "SYSTEM_LOCALTIME_LINK", link)
    monkeypatch.delenv("TZ", raising=False)
    return link


def _zone_file(root: Path, tree: str) -> Path:
    path = root / tree / "America" / "New_York"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_suffixed_zoneinfo_directory_still_yields_the_zone(tmp_path, localtime_link):
    """A zoneinfo tree whose directory carries a suffix must still resolve.

    macOS points /etc/localtime at a tree that resolves through
    ``zoneinfo.default``. Requiring the directory to be named exactly
    ``zoneinfo`` yields no zone there and silently falls back to UTC.
    """
    localtime_link.symlink_to(_zone_file(tmp_path, "zoneinfo.default"))

    assert config._system_local_tz_name() == "America/New_York"


def test_plain_zoneinfo_directory_yields_the_zone(tmp_path, localtime_link):
    localtime_link.symlink_to(_zone_file(tmp_path, "zoneinfo"))

    assert config._system_local_tz_name() == "America/New_York"


def test_zone_is_read_through_a_chained_link(tmp_path, localtime_link):
    """The link may chain through a directory that renames the tree.

    Only the unresolved target carries the tree's name in that case, so both
    the raw target and the fully resolved path have to be considered.
    """
    real = _zone_file(tmp_path, "zoneinfo.default")
    alias = tmp_path / "zoneinfo"
    alias.symlink_to(real.parent.parent)
    localtime_link.symlink_to(alias / "America" / "New_York")

    assert config._system_local_tz_name() == "America/New_York"


def test_tz_environment_variable_takes_precedence(tmp_path, localtime_link, monkeypatch):
    localtime_link.symlink_to(_zone_file(tmp_path, "zoneinfo"))
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    assert config._system_local_tz_name() == "Asia/Tokyo"


def test_missing_link_falls_back_to_utc_with_a_warning(localtime_link, caplog):
    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config._system_local_tz_name() == "UTC"

    assert "LIONAGI_SCHEDULER_TZ" in caplog.text


def test_path_outside_a_zoneinfo_tree_falls_back_to_utc(tmp_path, localtime_link, caplog):
    target = tmp_path / "etc" / "America" / "New_York"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    localtime_link.symlink_to(target)

    with caplog.at_level(logging.WARNING, logger=config.__name__):
        assert config._system_local_tz_name() == "UTC"

    assert "LIONAGI_SCHEDULER_TZ" in caplog.text


def test_unloadable_zone_name_falls_back_to_utc(tmp_path, localtime_link):
    """A parse that yields a name no tzdata knows must not be handed on.

    Returning it would push the failure into the scheduler, which can only
    report the bad name rather than the path it came from.
    """
    localtime_link.symlink_to(_zone_file(tmp_path, "zoneinfo").parent / "Nowhere")

    assert config._system_local_tz_name() == "UTC"

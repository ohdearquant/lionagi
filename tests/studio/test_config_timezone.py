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
    a zone source. Only the configured search roots are — and since the host
    did point somewhere readable, failing to name it is a refusal, not a
    default."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo.backup"))
    set_roots("zoneinfo")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


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


def test_link_structure_fallback_also_prefers_the_resolved_spelling(tz_host, tmp_path):
    """The fallback answers the same question as the primary path, so it has to
    answer it the same way.

    Reached only when no search root contains the target, this derives a name
    from the shape of the path instead. An alias link is where the two spellings
    diverge: ``US/Eastern`` and ``America/New_York`` both load, but the second is
    the zone the host is actually using, and the primary path already reports it
    that way. Two code paths disagreeing about which spelling wins is worse than
    either answer, because which one you get depends on whether the search root
    happened to contain the file.
    """
    link, set_roots, zone_file = tz_host
    # The search root carries both spellings as real files, so the loadability
    # check inside the fallback accepts either and cannot be what decides this.
    zone_file("roots", "America/New_York")
    zone_file("roots", "US/Eastern", data_from="America/New_York")
    # The host's actual chain lives in a tree that is NOT a search root, which
    # is what makes the primary path come up empty and hands the question to the
    # link-structure fallback. This is the macOS shape: /etc/localtime resolving
    # into a versioned tree beside the one TZPATH points at.
    outside = tmp_path / "outside" / "zoneinfo"
    (outside / "America").mkdir(parents=True, exist_ok=True)
    (outside / "America" / "New_York").write_bytes(_host_zone_bytes("America/New_York"))
    (outside / "US").mkdir(parents=True, exist_ok=True)
    (outside / "US" / "Eastern").symlink_to(outside / "America" / "New_York")
    link.symlink_to(outside / "US" / "Eastern")
    set_roots("roots")

    # Precondition: the primary path really does fail here, or this test would
    # pass while measuring the wrong code.
    assert config._zone_name_from_path(link.resolve(), config._tz_search_roots()) is None

    assert config._system_local_tz_name() == "America/New_York"


def test_link_structure_fallback_still_uses_link_text_when_resolving_yields_no_name(
    tz_host, tmp_path
):
    """The arm that keeps link text as a candidate rather than dropping it.

    When the resolved path lands outside any component named ``zoneinfo`` it
    yields no name, and the link's own text is the only thing left that does.
    Without this the reordering above would turn a host that resolves fine today
    into a refusal.
    """
    link, set_roots, zone_file = tz_host
    zone_file("roots", "America/New_York")
    # Resolves to a file under no component named zoneinfo, so the resolved
    # candidate yields nothing and only the link's own text names a zone.
    (tmp_path / "opaque").mkdir(parents=True, exist_ok=True)
    real = tmp_path / "opaque" / "tzfile"
    real.write_bytes(_host_zone_bytes("America/New_York"))
    outside = tmp_path / "outside" / "zoneinfo"
    (outside / "America").mkdir(parents=True, exist_ok=True)
    (outside / "America" / "New_York").symlink_to(real)
    link.symlink_to(outside / "America" / "New_York")
    set_roots("roots")

    assert config._zone_name_from_path(link.resolve(), config._tz_search_roots()) is None

    assert config._system_local_tz_name() == "America/New_York"


def test_shadowed_key_is_refused_rather_than_loading_another_zone(tz_host):
    """Two roots can hold the same key with different rules. The name derived
    from the root that contains localtime would be reopened from the earlier
    root, so accepting it would schedule on rules the host does not use."""
    link, set_roots, zone_file = tz_host
    zone_file("first", "America/New_York", data_from="Asia/Tokyo")
    link.symlink_to(zone_file("second", "America/New_York"))
    set_roots("first", "second")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


def test_unshadowed_key_still_resolves_with_several_roots(tz_host):
    """The refusal above is about a collision, not about having more than one
    root: a key only the containing root holds still resolves."""
    link, set_roots, zone_file = tz_host
    zone_file("first", "Asia/Tokyo")
    link.symlink_to(zone_file("second", "America/New_York"))
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


def test_malformed_zone_data_raises_rather_than_defaulting(tz_host, tmp_path):
    """The link opens, so the host stated a zone; the bytes behind it just
    don't load. Substituting UTC there misreads an answer that exists."""
    link, set_roots, _zone_file = tz_host
    path = tmp_path / "zoneinfo" / "America" / "New_York"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a tzfile")
    link.symlink_to(path)
    set_roots("zoneinfo")

    with pytest.raises(config.SystemTimezoneUnreadableError) as excinfo:
        config._system_local_tz_name()

    message = str(excinfo.value)
    assert str(link) in message
    assert "LIONAGI_SCHEDULER_TZ" in message


def test_symlink_loop_falls_back_instead_of_raising(tz_host, tmp_path):
    """A looping localtime link raises from ``resolve()`` rather than
    returning, and nothing can be read through it — no answer to misread, so
    the fallback stands."""
    link, set_roots, _zone_file = tz_host
    other = tmp_path / "loop_b"
    link.symlink_to(other)
    other.symlink_to(link)
    set_roots("zoneinfo")

    assert config._system_local_tz_name() == "UTC"


class _Blocked(BaseException):
    """Raised from the alarm handler below.

    Deliberately not an ``Exception``, and specifically not an ``OSError``.
    ``TimeoutError`` is an ``OSError`` subclass, so raising one here is caught
    by the very ``except OSError`` this test exists to prove is not reached,
    and the test passes whether or not the code under it blocks.
    """


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="needs SIGALRM to bound the hang")
def test_a_fifo_at_localtime_falls_back_without_blocking(tz_host, tmp_path):
    """A FIFO must be classified without being opened.

    Opening a FIFO with no writer blocks. This resolution runs while a module
    constant is being built, so a block here hangs the process at import with
    neither the error nor the fallback ever reached — strictly worse than
    either. The alarm is what makes the regression fail instead of hanging the
    suite that is supposed to catch it.
    """
    link, set_roots, _zone_file = tz_host
    fifo = tmp_path / "localtime_fifo"
    os.mkfifo(fifo)
    link.symlink_to(fifo)
    set_roots("zoneinfo")

    def _too_slow(_signum, _frame):
        raise _Blocked("reading /etc/localtime blocked; a FIFO was opened")

    previous = signal.signal(signal.SIGALRM, _too_slow)
    signal.alarm(5)
    try:
        assert config._system_local_tz_name() == "UTC"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_unreadable_localtime_falls_back_instead_of_raising(tz_host, tmp_path):
    """Present but unreadable is still nothing we can read an opinion out of.
    The crash case is a file that opens and then names no loadable zone."""
    if os.geteuid() == 0:
        pytest.skip("root reads through mode 000, so the unreadable case cannot be staged")
    link, set_roots, _zone_file = tz_host
    path = tmp_path / "zoneinfo" / "America" / "New_York"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a tzfile")
    path.chmod(0o000)
    link.symlink_to(path)
    set_roots("zoneinfo")

    try:
        assert config._system_local_tz_name() == "UTC"
    finally:
        path.chmod(0o644)


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


def test_target_outside_every_root_still_names_the_zone(tz_host):
    """The localtime target can live in a tree no search root contains — a
    Python whose TZPATH points elsewhere (conda-style builds), or macOS mid
    tzdata-update with the two chains in different versioned trees. The link
    path itself still names the zone, and the name is accepted because the
    configured roots can load it."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo"))
    zone_file("other-tree")
    set_roots("other-tree")

    resolution = config._resolve_system_local_tz()
    assert resolution.name == "America/New_York"
    assert resolution.source == config.TZ_SOURCE_SYSTEM_LOCALTIME
    assert "link path" in resolution.detail


def test_macos_version_skew_between_chains_resolves(tz_host, tmp_path):
    """macOS layout: /etc/localtime resolves through a middle symlink into one
    versioned tree while the search root points at another. Containment fails
    on the version mismatch; the link's own path still says which zone."""
    link, set_roots, zone_file = tz_host
    zone_file("tz/2025a/zoneinfo")
    middle = tmp_path / "var-zoneinfo"
    middle.symlink_to(tmp_path / "tz" / "2025a" / "zoneinfo")
    link.symlink_to(middle / "America" / "New_York")
    zone_file("tz/2025b/zoneinfo")
    set_roots("tz/2025b/zoneinfo")

    assert config._system_local_tz_name() == "America/New_York"


def test_link_named_zone_that_no_root_can_load_still_raises(tz_host):
    """The link-path fallback never invents a zone: a name the configured
    roots cannot load keeps the refusal, exactly as before."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo", "Nowhere/Fake", data_from="America/New_York"))
    set_roots("empty-tree")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


def test_suffixed_lookalike_tree_still_refuses_after_fallback(tz_host):
    """The fallback keys on a component spelled exactly ``zoneinfo``; a
    lookalike such as ``zoneinfo.backup`` stays a refusal (the containment
    rule's judgment, unchanged)."""
    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("zoneinfo.backup"))
    set_roots("zoneinfo")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()

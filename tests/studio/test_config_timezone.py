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
import zipfile
import zlib
import zoneinfo
from importlib import resources
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
    original_resources = resources.files

    def no_package_fallback(name):
        if name == "tzdata.zoneinfo":
            raise ModuleNotFoundError(name)
        return original_resources(name)

    monkeypatch.setattr(resources, "files", no_package_fallback)

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


@pytest.mark.parametrize("opaque_target", [False, True])
def test_link_structure_fallback_refuses_shadowed_keys(tz_host, tmp_path, opaque_target):
    """A loadable name must not select different rules from an earlier root."""
    link, set_roots, zone_file = tz_host
    zone_file("first", "America/New_York", data_from="Asia/Tokyo")
    zone_file("second", "America/New_York")
    outside = zone_file("outside/zoneinfo", "America/New_York")
    if opaque_target:
        target = tmp_path / "local-zone"
        outside.rename(target)
        outside.symlink_to(target)
    link.symlink_to(outside)
    set_roots("first", "second")

    assert config._zone_name_from_path(link.resolve(), config._tz_search_roots()) is None
    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


def test_link_structure_fallback_accepts_unshadowed_key_with_several_roots(tz_host):
    link, set_roots, zone_file = tz_host
    zone_file("first", "Asia/Tokyo")
    zone_file("second", "America/New_York")
    link.symlink_to(zone_file("outside/zoneinfo", "America/New_York"))
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


def test_link_structure_fallback_accepts_roots_pointing_to_the_same_file(tz_host, tmp_path):
    """Two root spellings that reopen the same tzfile do not shadow each other."""
    link, set_roots, zone_file = tz_host
    zone_file("first", "America/New_York")
    (tmp_path / "second").symlink_to(tmp_path / "first")
    link.symlink_to(zone_file("outside/zoneinfo", "America/New_York"))
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


@pytest.mark.parametrize("resolved_alias", [False, True])
def test_link_structure_refuses_alias_of_a_shadowed_canonical_key(
    tz_host, tmp_path, resolved_alias
):
    link, set_roots, zone_file = tz_host
    first = zone_file("first", data_from="Asia/Tokyo")
    zone_file("second")
    alias = tmp_path / "first" / "US" / "Eastern"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(first)
    outside = zone_file("outside/zoneinfo", "US/Eastern", data_from="America/New_York")
    if resolved_alias:
        canonical = zone_file("outside/zoneinfo")
        outside.unlink()
        outside.symlink_to(canonical)
    link.symlink_to(outside)
    set_roots("first", "second")

    with pytest.raises(config.SystemTimezoneUnreadableError, match="unambiguous"):
        config._system_local_tz_name()


@pytest.mark.parametrize("hard_link", [False, True])
def test_link_structure_accepts_equivalent_zone_files(tz_host, tmp_path, hard_link):
    link, set_roots, zone_file = tz_host
    first = zone_file("first")
    second = zone_file("second")
    if hard_link:
        second.unlink()
        os.link(first, second)
    link.symlink_to(zone_file("outside/zoneinfo"))
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


def test_ambiguous_resolved_spelling_cannot_fall_back_to_another_alias(tz_host):
    link, set_roots, zone_file = tz_host
    zone_file("first", data_from="Asia/Tokyo")
    zone_file("second")
    zone_file("first", "US/Eastern", data_from="America/New_York")
    canonical = zone_file("outside/zoneinfo")
    alias = zone_file("outside/zoneinfo", "US/Eastern", data_from="America/New_York")
    alias.unlink()
    alias.symlink_to(canonical)
    link.symlink_to(alias)
    set_roots("first", "second")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


def test_equivalent_alias_still_checks_its_shadowed_canonical_key(tz_host, tmp_path):
    link, set_roots, zone_file = tz_host
    canonical = zone_file("first")
    zone_file("second", data_from="Asia/Tokyo")
    alias = tmp_path / "first" / "US" / "Eastern"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(canonical)
    link.symlink_to(zone_file("outside/zoneinfo", "US/Eastern", data_from="America/New_York"))
    set_roots("first", "second")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


@pytest.mark.parametrize("hard_link", [False, True])
def test_primary_path_accepts_equivalent_zone_files(tz_host, hard_link):
    link, set_roots, zone_file = tz_host
    first = zone_file("first")
    second = zone_file("second")
    if hard_link:
        second.unlink()
        os.link(first, second)
    link.symlink_to(second)
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


@pytest.mark.parametrize("outside", [False, True])
def test_inaccessible_search_root_does_not_escape_resolution(
    tz_host, tmp_path, monkeypatch, outside
):
    link, set_roots, zone_file = tz_host
    readable = zone_file("readable")
    zone_file("locked")
    link.symlink_to(zone_file("outside/zoneinfo") if outside else readable)
    # In the primary path the inaccessible entry is encountered first; the
    # fallback encounters it while comparing roots after loading a valid zone.
    set_roots(*(("readable", "locked") if outside else ("locked", "readable")))
    original_stat = os.stat
    locked = str(tmp_path / "locked")

    def guarded_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and str(path).startswith(locked):
            raise PermissionError("timezone search root is inaccessible")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", guarded_stat)
    assert config._system_local_tz_name() == "America/New_York"


@pytest.mark.parametrize("package_zone", ["America/New_York", "Asia/Tokyo", None])
def test_link_structure_checks_package_rules_against_host(
    tz_host, tmp_path, monkeypatch, package_zone
):
    link, set_roots, zone_file = tz_host
    target = zone_file("outside/zoneinfo")
    link.symlink_to(target)
    set_roots("empty")
    package_root = tmp_path / "package"
    if package_zone:
        zone_file("package", data_from=package_zone)
    from importlib import resources

    monkeypatch.setattr(resources, "files", lambda name: package_root)
    if package_zone == "America/New_York":
        assert config._system_local_tz_name() == "America/New_York"
    else:
        with pytest.raises(config.SystemTimezoneUnreadableError):
            config._system_local_tz_name()


@pytest.mark.parametrize("oversized", [False, True])
def test_package_streams_are_bounded(tz_host, monkeypatch, oversized):
    import io

    link, set_roots, zone_file = tz_host
    host = zone_file("outside/zoneinfo")
    limit = 1 << 20
    data = host.read_bytes() + (bytes(limit) if oversized else b"")
    reads = []

    class PackageStream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    class PackageZone:
        def __init__(self, parts=()):
            self.parts = parts

        def joinpath(self, child):
            return PackageZone((*self.parts, child))

        def open(self, mode):
            assert mode == "rb"
            assert self.parts == ("America", "New_York")
            return PackageStream(data)

    monkeypatch.setattr(resources, "files", lambda name: PackageZone())
    link.symlink_to(host)
    set_roots("empty")
    if oversized:
        with pytest.raises(config.SystemTimezoneUnreadableError):
            config._system_local_tz_name()
    else:
        assert config._system_local_tz_name() == "America/New_York"
    assert reads and all(size == limit + 1 for size in reads)


@pytest.mark.parametrize("outside", [False, True])
def test_equivalent_rules_with_different_file_sizes_are_accepted(tz_host, outside):
    link, set_roots, zone_file = tz_host
    zone_file("first")
    second = zone_file("second")
    second.write_bytes(second.read_bytes() + b"\n")
    host = zone_file("outside/zoneinfo") if outside else second
    link.symlink_to(host)
    set_roots("first", "second")

    assert config._system_local_tz_name() == "America/New_York"


@pytest.mark.parametrize(
    "failure",
    [zipfile.BadZipFile("bad member"), zlib.error("bad stream"), RuntimeError("refused")],
    ids=["bad-zip-member", "bad-compressed-stream", "refused-member"],
)
def test_package_failure_is_not_evidence_about_the_zone(tz_host, monkeypatch, failure):
    """A tzdata installed inside a zip can fail while its member is read.

    A failed checksum, a compressed stream the reader cannot handle and a
    member it refuses to open all mean the same thing here: no evidence about
    the host's zone from this package. The host's file is readable, so the
    resolution refuses rather than quietly interpreting schedules in UTC. The
    refusal has to come from the failed read, so this asserts the package was
    reached at all; without that it passes wherever the package is never
    consulted.
    """
    from importlib import resources

    link, set_roots, zone_file = tz_host
    link.symlink_to(zone_file("outside/zoneinfo"))
    set_roots("empty")
    opened = []

    class FailingZone:
        def joinpath(self, child):
            return self

        def open(self, mode):
            opened.append("tzdata.zoneinfo")
            raise failure

    def failing_package(name):
        assert name == "tzdata.zoneinfo"
        return FailingZone()

    monkeypatch.setattr(resources, "files", failing_package)
    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()
    assert opened, "the package resource was never consulted"


def test_package_failure_does_not_escape_when_nothing_was_readable(tz_host, monkeypatch):
    """The same failure with no readable localtime falls back to UTC.

    The exception has to be absorbed on both paths: a package that cannot be
    read is not a reason for host resolution to raise out of its own error
    handling, and here there is no host file to refuse over.
    """
    from importlib import resources

    _link, set_roots, _zone_file = tz_host
    set_roots("empty")

    class FailingZone:
        def joinpath(self, child):
            return self

        def open(self, mode):
            raise zipfile.BadZipFile("bad member")

    monkeypatch.setattr(resources, "files", lambda name: FailingZone())
    assert config._system_local_tz_name() == "UTC"


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("matching", [False, True])
def test_outside_alias_requires_the_hosts_rules(tz_host, monkeypatch, canonical, matching):
    from importlib import resources

    link, set_roots, zone_file = tz_host
    zone_file("root", "US/Eastern", data_from="America/New_York" if matching else "Asia/Tokyo")
    alias = zone_file("outside/zoneinfo", "US/Eastern", data_from="America/New_York")
    if canonical:
        target = zone_file("outside/zoneinfo")
        alias.unlink()
        alias.symlink_to(target)
    link.symlink_to(alias)
    set_roots("root")

    def no_package(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(resources, "files", no_package)
    if matching:
        assert config._system_local_tz_name() == "US/Eastern"
    else:
        with pytest.raises(config.SystemTimezoneUnreadableError):
            config._system_local_tz_name()


def test_versioned_trees_accept_different_encodings_of_the_same_rules(tz_host):
    link, set_roots, zone_file = tz_host
    host = zone_file("old/zoneinfo")
    configured = zone_file("new/zoneinfo")
    # The 32-bit compatibility block is not the modern reader's rule table.
    # Replace it with the valid placeholder permitted by the TZif format.
    import struct

    data = configured.read_bytes()
    utc, standard, leap, count, types, chars = struct.unpack_from(">6I", data, 20)
    second_header = 44 + count * 5 + types * 6 + chars + leap * 8 + standard + utc
    version = data[4:5]
    assert version in (b"2", b"3", b"4")
    header = b"TZif" + version + bytes(15) + struct.pack(">6I", 0, 0, 0, 0, 1, 1)
    configured.write_bytes(header + bytes(7) + data[second_header:])
    assert configured.read_bytes() != host.read_bytes()
    link.symlink_to(host)
    set_roots("new/zoneinfo")

    assert config._system_local_tz_name() == "America/New_York"


def _slimmed(data):
    """*data* with the transitions its POSIX footer already covers removed.

    A maintained zone can store a transition that another build of the same
    rules leaves to the footer, so two files for one zone routinely carry
    different numbers of transitions and different type tables. Only the
    stored transitions before the footer's recurring rules start are kept.
    """
    import struct

    utc, standard, leap, count, types, chars = struct.unpack_from(">6I", data, 20)
    second_header = 44 + count * 5 + types * 6 + chars + leap * 8 + standard + utc
    utc2, standard2, leap2, count2, types2, chars2 = struct.unpack_from(
        ">6I", data, second_header + 20
    )
    times_offset = second_header + 44
    times = struct.unpack_from(f">{count2}q", data, times_offset)
    # New York's footer describes the rules in force since 2007, so the stored
    # transitions from 2008-01-01T00:00:00Z on are the ones it already covers;
    # dropping them leaves the same behaviour.
    keep = sum(1 for when in times if when < 1_199_145_600)
    if not 0 < keep < count2:
        pytest.skip("this host's zone file stores no transition after 2007")
    indices_offset = times_offset + count2 * 8
    types_offset = indices_offset + count2
    chars_offset = types_offset + types2 * 6
    leaps_offset = chars_offset + chars2
    standard_offset = leaps_offset + leap2 * 12
    end = standard_offset + standard2 + utc2
    footer_end = data.index(b"\n", end + 1)
    footer = data[end + 1 : footer_end]
    header = data[second_header : second_header + 5] + bytes(15)
    header += struct.pack(">6I", utc2, standard2, leap2, keep, types2, chars2)
    block = struct.pack(f">{keep}q", *times[:keep])
    block += data[indices_offset : indices_offset + keep]
    block += data[types_offset:end]
    return data[:second_header] + header + block + b"\n" + footer + b"\n"


def test_differently_encoded_copies_of_one_zone_are_accepted(tz_host):
    """Two builds of a zone can store different transitions for the same rules.

    A file that leaves a transition to its POSIX footer and one that stores it
    describe identical rules, so reading the host's file correctly must not
    depend on which encoding the copy in the search root happens to use.
    """
    link, set_roots, zone_file = tz_host
    host = zone_file("outside/zoneinfo")
    slim = _slimmed(host.read_bytes())
    assert slim != host.read_bytes()
    zone_file("root").write_bytes(slim)
    link.symlink_to(host)
    set_roots("root")

    assert config._system_local_tz_name() == "America/New_York"


def test_copies_that_keep_the_same_transitions_but_change_one_are_refused(tz_host):
    """The comparison above must not accept a copy whose rules differ.

    Shifting one stored transition by an hour changes the offsets the host
    reads from that instant on, which is the disagreement this check exists to
    catch, so the encoding-tolerant comparison still has to refuse it.
    """
    import struct

    link, set_roots, zone_file = tz_host
    host = zone_file("outside/zoneinfo")
    data = bytearray(host.read_bytes())
    utc, standard, leap, count, types, chars = struct.unpack_from(">6I", data, 20)
    second_header = 44 + count * 5 + types * 6 + chars + leap * 8 + standard + utc
    times_offset = second_header + 44
    first = struct.unpack_from(">q", data, times_offset)[0]
    struct.pack_into(">q", data, times_offset, first + 3600)
    zone_file("root").write_bytes(bytes(data))
    link.symlink_to(host)
    set_roots("root")

    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


@pytest.mark.parametrize("same_bytes", [False, True])
def test_reader_boundary_requires_exact_file_identity(tz_host, same_bytes):
    import io

    link, set_roots, zone_file = tz_host
    host = zone_file("outside/zoneinfo")
    original = host.read_bytes()
    assert b"LMT" in original
    unsupported = original.replace(b"LMT", "ÉT".encode())
    host.write_bytes(unsupported)
    root = zone_file("root")
    configured = unsupported if same_bytes else unsupported + b"\0"
    root.write_bytes(configured)
    assert isinstance(zoneinfo.ZoneInfo.from_file(io.BytesIO(unsupported)), zoneinfo.ZoneInfo)
    assert isinstance(zoneinfo.ZoneInfo.from_file(io.BytesIO(configured)), zoneinfo.ZoneInfo)
    link.symlink_to(host)
    set_roots("root")
    if same_bytes:
        assert config._system_local_tz_name() == "America/New_York"
    else:
        with pytest.raises(config.SystemTimezoneUnreadableError):
            config._system_local_tz_name()


@pytest.mark.parametrize("at_host", [False, True])
def test_oversized_zone_file_is_not_read_in_full(tz_host, monkeypatch, at_host):
    from pathlib import Path

    link, set_roots, zone_file = tz_host
    host = zone_file("outside/zoneinfo")
    root = zone_file("root")
    oversized = host if at_host else root
    oversized.write_bytes(oversized.read_bytes() + bytes((1 << 20) + 1))
    link.symlink_to(host)
    set_roots("root")
    # Reads go through ``os.open`` and ``os.fdopen``, so the spy has to sit on
    # those rather than on ``Path``, which nothing on this path calls.
    original_open = os.open

    def bounded_open(path, *args, **kwargs):
        if path == oversized:
            raise AssertionError("oversized timezone was read in full")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", bounded_open)
    with pytest.raises(config.SystemTimezoneUnreadableError):
        config._system_local_tz_name()


def test_fifo_inside_search_root_is_never_opened(tz_host, tmp_path, monkeypatch):
    link, set_roots, zone_file = tz_host
    fifo = tmp_path / "zoneinfo" / "America" / "New_York"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    zone_file("second")
    link.symlink_to(fifo)
    set_roots("zoneinfo", "second")
    original_open = os.open

    def refuse_fifo(path, *args, **kwargs):
        if path == fifo:
            raise AssertionError("attempted to open a FIFO as timezone data")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", refuse_fifo)
    assert config._system_local_tz_name() == "UTC"


@pytest.mark.parametrize("regular_alternative", [False, True])
def test_cycle_in_root_key_does_not_escape_resolution(tz_host, tmp_path, regular_alternative):
    link, set_roots, zone_file = tz_host
    first = tmp_path / "first" / "America" / "New_York"
    first.parent.mkdir(parents=True)
    alias = first.with_name("alias")
    first.symlink_to(alias)
    alias.symlink_to(first)
    host = zone_file("outside/zoneinfo")
    link.symlink_to(host)
    if regular_alternative:
        zone_file("second")
    set_roots("first", "second")
    if regular_alternative:
        assert config._system_local_tz_name() == "America/New_York"
    else:
        with pytest.raises(config.SystemTimezoneUnreadableError):
            config._system_local_tz_name()


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

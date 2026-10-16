"""Bounded file evidence for scheduler timezone inference."""

import io
import os
import struct
import zoneinfo
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

import pytest

from lionagi.studio import _timezone_rules as rules


def _header(version, count, types, chars):
    return b"TZif" + version + bytes(15) + struct.pack(">6I", 0, 0, 0, count, types, chars)


def _tzif(*, times=(0, 100000), offset=3600, is_dst=1, abbreviation=b"DST", footer=b""):
    names = b"STD\0" + abbreviation + b"\0"
    compatibility = _header(b"2", 0, 1, 1) + bytes(7)
    modern = _header(b"2", len(times), 2, len(names))
    modern += struct.pack(f">{len(times)}q", *times) + bytes(1 - i % 2 for i in range(len(times)))
    modern += struct.pack(">iBBiBB", 0, 0, 0, offset, is_dst, 4) + names
    return compatibility + modern + b"\n" + footer + b"\n"


def _minimal_compatibility(data):
    utc, standard, leap, count, types, chars = struct.unpack_from(">6I", data, 20)
    end = 44 + count * 5 + types * 6 + chars + leap * 8 + standard + utc
    return _header(data[4:5], 0, 1, 1) + bytes(7) + data[end:]


def _assert_loadable(data):
    assert isinstance(zoneinfo.ZoneInfo.from_file(io.BytesIO(data)), zoneinfo.ZoneInfo)


@pytest.mark.parametrize(
    "change",
    [
        {"times": (1, 100000)},
        {"offset": 3601},
        {"is_dst": 0},
        {"abbreviation": b"OTHER"},
        {"footer": b"STD0"},
    ],
)
def test_complete_rule_components_are_compared(change):
    original = _tzif()
    changed = _tzif(**change)
    _assert_loadable(original)
    _assert_loadable(changed)
    original_rules = rules.tzif_rules(original)
    changed_rules = rules.tzif_rules(changed)
    # Without this the assertion below also holds when both sides fell back to
    # the exact-bytes form, which would hide a reader that stopped parsing.
    assert original_rules[0] == "tzif"
    assert changed_rules[0] == "tzif"
    assert not rules.rules_equal(original_rules, changed_rules)


def test_footer_text_is_not_normalized():
    original = _tzif(footer=b"STD0DST,M3.2.0,M11.1.0")
    changed = _tzif(footer=b"STD0DST,M3.2.0/2,M11.1.0/2")
    _assert_loadable(original)
    _assert_loadable(changed)
    original_rules = rules.tzif_rules(original)
    changed_rules = rules.tzif_rules(changed)
    assert original_rules[0] == "tzif"
    assert changed_rules[0] == "tzif"
    assert not rules.rules_equal(original_rules, changed_rules)


def _host_zone_bytes(zone):
    for entry in zoneinfo.TZPATH:
        candidate = Path(entry) / zone
        if candidate.is_file():
            return candidate.read_bytes()
    pytest.skip(f"no tzfile for {zone} under this host's search path")


def _without_transitions_the_footer_covers(data):
    """*data* with the transitions past the 32-bit range removed.

    A zone's POSIX footer describes its recurrence, so a build may store those
    transitions or leave them to the footer. Dropping the records a footer
    already describes gives a second encoding of the same rules.
    """
    utc, standard, leap, count, types, chars = struct.unpack_from(">6I", data, 20)
    second_header = 44 + count * 5 + types * 6 + chars + leap * 8 + standard + utc
    utc2, standard2, leap2, count2, types2, chars2 = struct.unpack_from(
        ">6I", data, second_header + 20
    )
    times_offset = second_header + 44
    times = struct.unpack_from(f">{count2}q", data, times_offset)
    indices_offset = times_offset + count2 * 8
    types_offset = indices_offset + count2
    chars_offset = types_offset + types2 * 6
    standard_offset = chars_offset + chars2 + leap2 * 12
    end = standard_offset + standard2 + utc2
    footer_end = data.index(b"\n", end + 1)
    footer = data[end + 1 : footer_end]
    # 2038-01-19T03:14:08Z is the last instant the 32-bit block can describe;
    # the recurring rule beyond it lives in the footer.
    keep = sum(1 for when in times if when < 1_199_145_600)
    if not 0 < keep < count2:
        pytest.skip("this zone stores no transition past the 32-bit range")
    header = data[second_header : second_header + 5] + bytes(15)
    header += struct.pack(">6I", utc2, standard2, leap2, keep, types2, chars2)
    block = struct.pack(f">{keep}q", *times[:keep])
    block += data[indices_offset : indices_offset + keep]
    block += data[types_offset:end]
    return data[:second_header] + header + block + b"\n" + footer + b"\n"


def test_different_encodings_of_one_zone_are_equal():
    """A transition may be stored or left to the footer; both read the same."""
    stored = _host_zone_bytes("America/New_York")
    footer_only = _without_transitions_the_footer_covers(stored)
    assert footer_only != stored
    _assert_loadable(stored)
    _assert_loadable(footer_only)
    assert rules.rules_equal(rules.tzif_rules(stored), rules.tzif_rules(footer_only))


_YEARLY_FOOTER = b"STD0DST,M3.1.0/0,M9.1.0/0"


def _first_sunday(year, month):
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    return int(first.timestamp()) + (6 - first.weekday()) % 7 * 86400


def _yearly(first, last, *, skip=()):
    """The transitions ``_YEARLY_FOOTER`` makes in each year, as a table stores them."""
    times = []
    for year in range(first, last + 1):
        if year not in skip:
            # Midnight standard time in March, midnight summer time in September.
            times += [_first_sunday(year, 3), _first_sunday(year, 9) - 3600]
    return rules.tzif_rules(_tzif(times=tuple(times), footer=_YEARLY_FOOTER))


def test_a_year_one_table_leaves_out_is_not_passed_over():
    """A summer one table omits differs from the other file's footer.

    Read only at stored transitions, a table with no change in 2015 agrees
    with a file whose footer has summer time that year at every instant read.
    """
    slim, fat = _yearly(1990, 2000), _yearly(1990, 2030)
    footer_only = rules.tzif_rules(_tzif(times=(), footer=_YEARLY_FOOTER))
    assert rules.rules_equal(slim, fat)
    assert rules.rules_equal(fat, slim)
    skipped = _yearly(1990, 2030, skip={2015})
    assert not rules.rules_equal(slim, skipped)
    assert not rules.rules_equal(skipped, slim)
    # A file storing no transition reads its footer before the other table too.
    assert rules.rules_equal(footer_only, footer_only)
    assert not rules.rules_equal(footer_only, fat)
    assert not rules.rules_equal(fat, footer_only)


def test_modern_rules_ignore_compatibility_records_and_padding():
    original = _tzif()
    # The public loader ignores these obsolete local-time records too.
    compatibility_changed = bytearray(original)
    compatibility_changed[44:51] = b"\xff" * 7
    compatibility_changed[5:20] = b"\x01" * 15
    changed = bytes(compatibility_changed) + b"unused trailing bytes"
    _assert_loadable(original)
    _assert_loadable(changed)
    assert rules.rules_equal(rules.tzif_rules(original), rules.tzif_rules(changed))


def test_loadable_file_outside_reader_boundary_uses_exact_bytes():
    # The public loader accepts a UTF-8 designation. The bounded reader only
    # interprets ASCII designations, so this file must retain exact identity.
    unsupported = _tzif(abbreviation="ÉTÉ".encode())
    padded = unsupported + b"\0"
    _assert_loadable(unsupported)
    _assert_loadable(padded)
    assert rules.tzif_rules(unsupported) == ("exact-bytes", unsupported)
    assert rules.rules_equal(
        rules.tzif_rules(unsupported), rules.tzif_rules(bytes(bytearray(unsupported)))
    )
    assert not rules.rules_equal(rules.tzif_rules(unsupported), rules.tzif_rules(padded))


class _Unterminated(BaseException):
    """Raised by the guarded stream below, from inside the public loader."""


class _EofGuarded(io.BytesIO):
    """A stream the public loader cannot read forever.

    The loader scans the POSIX footer one byte at a time until it finds a
    newline and never stops at end of file, so a file whose footer is not
    terminated makes it loop for the life of the process. Counting the reads
    turns that loop into an exception instead of a hang, and a
    ``BaseException`` keeps it out of the reader's own ``except`` tuple so a
    regression is reported rather than absorbed.
    """

    def read(self, size=-1):
        chunk = super().read(size)
        if chunk or size == 0:
            self._empty = 0
        else:
            self._empty = getattr(self, "_empty", 0) + 1
            if self._empty > 10_000:
                raise _Unterminated("the loader read past the end of the footer")
        return chunk


@pytest.mark.parametrize(
    "truncate",
    [
        lambda data: data[:-1],
        lambda data: data[: data.rindex(b"\n", 0, len(data) - 1) + 2],
        lambda data: data[:-1] + b"x",
    ],
)
def test_unterminated_footer_never_reaches_the_public_loader(monkeypatch, truncate):
    """A footer with no closing newline is refused, not handed to the loader.

    The complete file is accepted, so the truncations above only remove the
    footer's terminating newline. Handing one of them to the public loader
    would loop inside it forever, which for a value read while a module
    constant is being built means a process that never starts.
    """
    complete = _tzif(footer=b"STD0DST,M3.2.0,M11.1.0")
    assert rules.tzif_rules(complete) is not None

    monkeypatch.setattr(rules, "io", type("io", (), {"BytesIO": _EofGuarded}))
    assert rules.tzif_rules(truncate(complete)) is None


def test_every_installed_package_zone_is_parsed_and_self_equivalent():
    tzdata = pytest.importorskip("tzdata")
    names = resources.files("tzdata").joinpath("zones").read_text().splitlines()
    assert names and len(names) == len(set(names))
    package = resources.files("tzdata.zoneinfo")
    accepted = 0
    for name in names:
        original = package.joinpath(*name.split("/")).read_bytes()
        parsed = rules.tzif_rules(original)
        assert parsed is not None and parsed[0] == "tzif", name
        for equivalent in (original, original + b"\0", _minimal_compatibility(original)):
            assert rules.rules_equal(rules.tzif_rules(equivalent), parsed), name
            _assert_loadable(equivalent)
            accepted += 1
    print(
        f"tzdata={tzdata.__version__} IANA={tzdata.IANA_VERSION} "
        f"zones={len(names)} parsed={len(names)} public_loader_accepted={accepted}"
    )


def test_read_limit_is_independent_of_file_identity(tmp_path, monkeypatch):
    path = tmp_path / "zone"
    path.write_bytes(b"x" * 65)
    original_open = os.open

    def forbidden_open(candidate, *args, **kwargs):
        if candidate == path:
            raise AssertionError("oversized file was opened")
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(os, "open", forbidden_open)
    assert rules.read_tzif(path, limit=64) is None


@pytest.mark.parametrize("modern", [False, True])
def test_declared_lengths_are_bounded_before_the_public_loader(monkeypatch, modern):
    if modern:
        data = bytearray(_tzif())
        # A 51-byte compatibility block precedes the modern header.
        data[51 + 32 : 51 + 36] = struct.pack(">I", (1 << 32) - 1)
        data = bytes(data)
    else:
        data = _header(b"2", (1 << 32) - 1, 1, 1)

    def forbidden_loader(*args, **kwargs):
        raise RuntimeError("unchecked lengths reached the public loader")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", type("Loader", (), {"from_file": forbidden_loader}))
    assert rules.tzif_rules(data) is None


def _compatibility_only(first_type):
    names = b"STD\0DST\0"
    data = _header(b"\0", 2, 2, len(names)) + struct.pack(">2i", 0, 100000)
    data += bytes((first_type, 0)) + struct.pack(">iBBiBB", 0, 0, 0, 3600, 1, 4)
    return data + names


@pytest.mark.parametrize("modern", [False, True])
def test_transition_types_are_bounded_before_the_public_loader(monkeypatch, modern):
    """A transition type one past the table never reaches the public loader.

    Before Python 3.13 the loader accepts it and the process crashes on the
    first lookup, so a check after the loader returns would come too late.
    """
    if modern:
        valid = _tzif()
        data = bytearray(valid)
        # Two 8-byte transition times follow the modern header at 51.
        assert data[51 + 44 + 16] == 1
        data[51 + 44 + 16] = 2
        data = bytes(data)
    else:
        valid, data = _compatibility_only(1), _compatibility_only(2)
    _assert_loadable(valid)
    assert rules.tzif_rules(valid) is not None

    def forbidden_loader(*args, **kwargs):
        raise RuntimeError("an out-of-range transition type reached the public loader")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", type("Loader", (), {"from_file": forbidden_loader}))
    with pytest.raises(RuntimeError):
        rules.tzif_rules(valid)
    assert rules.tzif_rules(data) is None


def test_stream_read_is_capped_if_a_file_grows(tmp_path, monkeypatch):
    from contextlib import contextmanager

    path = tmp_path / "zone"
    path.write_bytes(b"data")
    original_fdopen = os.fdopen
    reads = []

    @contextmanager
    def growing_stream(fd, mode):
        with original_fdopen(fd, mode) as stream:

            class Growing:
                def fileno(self):
                    return stream.fileno()

                def read(self, size):
                    reads.append(size)
                    path.write_bytes(b"x" * 128)
                    return stream.read(size)

            yield Growing()

    monkeypatch.setattr(os, "fdopen", growing_stream)
    assert rules.read_tzif(path, limit=64) is None
    assert reads == [65]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO support")
def test_open_race_to_fifo_remains_nonblocking(tmp_path, monkeypatch):
    path = tmp_path / "zone"
    path.write_bytes(b"data")
    original_open = os.open

    def replace_with_fifo(candidate, flags, *args, **kwargs):
        assert candidate == path
        path.unlink()
        os.mkfifo(path)
        assert flags & os.O_NONBLOCK
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_with_fifo)
    assert rules.read_tzif(path) is None


@pytest.mark.parametrize("data", [None, b"", b"not TZif", b"TZif" + b"\0" * 40])
def test_unloadable_data_has_no_evidence(data):
    assert rules.tzif_rules(data) is None

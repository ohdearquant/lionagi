"""Bounded file evidence for scheduler timezone inference."""

import io
import os
import struct
import zoneinfo
from importlib import resources

import pytest

from lionagi.studio import _timezone_rules as rules


def _header(version, count, types, chars):
    return b"TZif" + version + bytes(15) + struct.pack(">6I", 0, 0, 0, count, types, chars)


def _tzif(*, times=(0, 100000), offset=3600, is_dst=1, abbreviation=b"DST", footer=b""):
    names = b"STD\0" + abbreviation + b"\0"
    compatibility = _header(b"2", 0, 1, 1) + bytes(7)
    modern = _header(b"2", len(times), 2, len(names))
    modern += struct.pack(f">{len(times)}q", *times) + bytes((1, 0))
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
    assert rules.tzif_rules(original) != rules.tzif_rules(changed)


def test_footer_text_is_not_normalized():
    original = _tzif(footer=b"STD0DST,M3.2.0,M11.1.0")
    changed = _tzif(footer=b"STD0DST,M3.2.0/2,M11.1.0/2")
    _assert_loadable(original)
    _assert_loadable(changed)
    assert rules.tzif_rules(original) != rules.tzif_rules(changed)


def test_modern_rules_ignore_compatibility_records_and_padding():
    original = _tzif()
    # The public loader ignores these obsolete local-time records too.
    compatibility_changed = bytearray(original)
    compatibility_changed[44:51] = b"\xff" * 7
    compatibility_changed[5:20] = b"\x01" * 15
    changed = bytes(compatibility_changed) + b"unused trailing bytes"
    _assert_loadable(original)
    _assert_loadable(changed)
    assert rules.tzif_rules(original) == rules.tzif_rules(changed)


def test_loadable_file_outside_reader_boundary_uses_exact_bytes():
    # The public loader accepts a UTF-8 designation. The bounded reader only
    # interprets ASCII designations, so this file must retain exact identity.
    unsupported = _tzif(abbreviation="ÉTÉ".encode())
    padded = unsupported + b"\0"
    _assert_loadable(unsupported)
    _assert_loadable(padded)
    assert rules.tzif_rules(unsupported) == ("exact-bytes", unsupported)
    assert rules.tzif_rules(unsupported) == rules.tzif_rules(bytes(bytearray(unsupported)))
    assert rules.tzif_rules(unsupported) != rules.tzif_rules(padded)


def test_every_installed_package_zone_is_parsed_and_self_equivalent():
    tzdata = pytest.importorskip("tzdata")
    names = resources.files("tzdata").joinpath("zones").read_text().splitlines()
    assert names and len(names) == len(set(names))
    package = resources.files("tzdata.zoneinfo")
    accepted = 0
    for name in names:
        original = package.joinpath(*name.split("/")).read_bytes()
        parsed = rules.tzif_rules(original)
        assert parsed is not None and parsed[0] != "exact-bytes", name
        for equivalent in (original, original + b"\0", _minimal_compatibility(original)):
            assert rules.tzif_rules(equivalent) == parsed, name
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

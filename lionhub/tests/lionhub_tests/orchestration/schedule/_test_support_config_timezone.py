from __future__ import annotations

import zoneinfo
from importlib import resources
from pathlib import Path

import pytest

import lionhub.orchestration.schedule.timing as config

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


class _Blocked(BaseException):
    """Raised from the alarm handler below.

    Deliberately not an ``Exception``, and specifically not an ``OSError``.
    ``TimeoutError`` is an ``OSError`` subclass, so raising one here is caught
    by the very ``except OSError`` this test exists to prove is not reached,
    and the test passes whether or not the code under it blocks.
    """

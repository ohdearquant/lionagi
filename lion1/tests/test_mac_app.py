"""The Mac app's Info.plist: its App Transport Security exception is local networking and nothing
else, so the host on this Mac is reached as plain http and any other host takes https."""

import plistlib
from pathlib import Path

PLIST = Path(__file__).resolve().parents[1] / "apps" / "mac" / "Lion" / "Info.plist"


def test_the_apps_transport_exception_is_local_networking_alone():
    info = plistlib.loads(PLIST.read_bytes())
    assert info["NSAppTransportSecurity"] == {"NSAllowsLocalNetworking": True}
    assert info["NSLocalNetworkUsageDescription"]

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Seatbelt policy's hard-link rules, the per-volume link probe and the launch check."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from lionagi import _paths
from lionagi._errors import ConfigurationError
from lionhub.tools import _seatbelt, seatbelt_link_probe
from lionhub.tools._seatbelt import SeatbeltPolicy, seatbelt_available
from lionhub.tools.seatbelt_link_probe import (
    REQUIRED_ARMS,
    VolumeIdentityError,
    policy_digest,
    require_passing_link_record,
    write_record,
)

needs_seatbelt = pytest.mark.skipif(
    not seatbelt_available(), reason="needs macOS with /usr/bin/sandbox-exec"
)

_UUID = "00000000-0000-4000-8000-000000000001"
_PASSED = {name: {"passed": True, "evidence": "test"} for name in REQUIRED_ARMS}


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    """A lionagi home and a temporary directory of the test's own, both resolved."""
    base = tmp_path.resolve()
    (base / "home").mkdir()
    (base / "tmp").mkdir()
    monkeypatch.setattr(_paths, "LIONAGI_HOME", base / "home")
    monkeypatch.setattr(tempfile, "tempdir", str(base / "tmp"))
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(
        _seatbelt, "_uv_cache_dir", lambda *, _command_env_names=None: base / "uv-cache"
    )
    return base


@pytest.fixture
def one_volume(monkeypatch):
    """Every path on one volume, named without df or diskutil."""
    monkeypatch.setattr(
        seatbelt_link_probe, "volume_identity", lambda path: ("/Volumes/Test", _UUID)
    )


def _policy(base: Path) -> SeatbeltPolicy:
    workspace = base / "workspace"
    workspace.mkdir(exist_ok=True)
    return SeatbeltPolicy.for_workspace(workspace, per_leg_uv_cache=True)


def _record(base: Path, mount: str = "/Volumes/Test", uuid: str = _UUID, arms=None) -> Path:
    """A record for the write set's policy shape, as the probe writes one."""
    return write_record(
        mount, uuid, _PASSED if arms is None else arms, policy_digest(_policy(base))
    )


# ---------------------------------------------------------------------------- policy text
def test_the_policy_denies_a_hard_link_outside_its_roots_and_under_its_denied_paths():
    policy = SeatbeltPolicy(
        writable_roots=("/w", "/t"), read_only=("/w/.git",), deny_read=("/d", "/e")
    )
    text = policy.profile()

    assert (
        '(deny file-link (require-not (require-any (subpath (param "WRITABLE_ROOT_0")) '
        '(subpath (param "WRITABLE_ROOT_1")))))'
    ) in text
    for param in ("READ_ONLY_0", "DENY_READ_0", "DENY_READ_1"):
        assert f'(deny file-link (subpath (param "{param}")))' in text
    # After the write grant, so the link rules win over it.
    assert text.index("(deny file-link") > text.index("(allow file-write*\n")


def test_the_file_link_denies_are_the_last_rules_whatever_options_are_on():
    # Every option that renders rules turned on: nothing after the first file-link deny may
    # allow anything, so no later rule can lift it.
    text = SeatbeltPolicy(
        writable_roots=("/w", "/t"),
        read_only=("/w/.git",),
        network=True,
        pty=True,
        deny_read=("/d",),
    ).profile()
    tail = text[text.index("(deny file-link") :]

    assert "(allow" not in tail
    assert [line for line in tail.splitlines() if line] == [
        line for line in tail.splitlines() if line.startswith("(deny file-link")
    ]


def test_the_digest_covers_the_network_terminal_and_unreadable_rules(monkeypatch):
    policy = SeatbeltPolicy(writable_roots=("/w",), read_only=("/w/.git",))
    before = policy_digest(policy)
    monkeypatch.setattr(_seatbelt, "_PTY_POLICY", _seatbelt._PTY_POLICY + "(allow file-link)\n")

    assert policy_digest(policy) != before


def test_a_policy_with_no_writable_root_denies_every_hard_link():
    assert "(deny file-link)\n" in SeatbeltPolicy(writable_roots=()).profile()


# ---------------------------------------------------------------------------- the probe
@needs_seatbelt
def test_the_probe_passes_on_this_volume_and_its_record_lets_the_launch_through(home):
    mount, uuid, arms, digest = seatbelt_link_probe.probe(home / "scratch")

    assert set(arms) == set(REQUIRED_ARMS)
    assert all(arm["passed"] for arm in arms.values()), arms
    assert "Operation not permitted" in arms["link_out_of_roots_refused"]["evidence"]
    assert list((home / "scratch").iterdir()) == []  # the working directory is removed
    assert digest == policy_digest(_policy(home))
    path = write_record(mount, uuid, arms, digest)
    assert path.parent == home / "home" / "seatbelt-link-probes"

    require_passing_link_record(_policy(home))


@needs_seatbelt
def test_the_probe_fails_its_refusal_arms_when_the_policy_has_no_write_rules(home, monkeypatch):
    # A policy that grants every write cannot refuse a link; the arms must say so, not pass.
    monkeypatch.setattr(SeatbeltPolicy, "profile", lambda self: "(version 1)\n(allow default)\n")
    _, _, arms, _ = seatbelt_link_probe.probe(home / "scratch")

    assert arms["link_out_of_roots_refused"]["passed"] is False
    assert arms["link_from_read_only_refused"]["passed"] is False
    assert arms["link_through_symlink_refused"]["passed"] is False
    assert arms["link_to_outside_name_refused"]["passed"] is False
    assert arms["link_inside_root_works"]["passed"] is True


def test_the_probe_refuses_a_scratch_directory_inside_the_temp_directory(home):
    with pytest.raises(ConfigurationError, match="inside the temp directory"):
        seatbelt_link_probe.probe(home / "tmp" / "scratch")


@needs_seatbelt
def test_the_volume_of_a_real_path_is_named_by_its_mount_point_and_uuid(tmp_path):
    mount, uuid = seatbelt_link_probe.volume_identity(tmp_path)

    assert mount.startswith("/") and uuid


def test_a_volume_whose_uuid_cannot_be_read_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt_link_probe, "_mount_point", lambda path: "/Volumes/NoUUID")
    monkeypatch.setattr(seatbelt_link_probe, "_DISKUTIL", "/nonexistent/diskutil")

    with pytest.raises(VolumeIdentityError, match="cannot be read"):
        seatbelt_link_probe.volume_identity(tmp_path)


# ---------------------------------------------------------------------------- the launch check
def test_a_volume_with_no_record_is_refused_with_the_command_that_makes_one(home, one_volume):
    with pytest.raises(ConfigurationError) as refused:
        require_passing_link_record(_policy(home))

    text = str(refused.value)
    assert "/Volumes/Test" in text and "has no link probe record" in text
    assert "python -m lionhub.tools.seatbelt_link_probe --scratch" in text


def test_a_record_with_every_arm_passed_lets_the_launch_through(home, one_volume):
    _record(home)

    require_passing_link_record(_policy(home))


@pytest.mark.parametrize("arm", REQUIRED_ARMS)
def test_a_record_with_a_failed_or_missing_arm_is_refused(home, one_volume, arm):
    failed = {**_PASSED, arm: {"passed": False, "evidence": "test"}}
    _record(home, arms=failed)
    with pytest.raises(ConfigurationError, match=f"did not all pass \\({arm}\\)"):
        require_passing_link_record(_policy(home))

    missing = {name: value for name, value in _PASSED.items() if name != arm}
    _record(home, arms=missing)
    with pytest.raises(ConfigurationError, match=f"did not all pass \\({arm}\\)"):
        require_passing_link_record(_policy(home))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("kernel", "Darwin 0.0.0 another build", "another kernel"),
        ("policy_sha256", "0" * 64, "another policy text"),
        ("volume_uuid", "00000000-0000-4000-8000-000000000002", "another volume"),
        ("format", 0, "another format"),
    ],
)
def test_a_record_made_for_another_kernel_policy_volume_or_format_is_refused(
    home, one_volume, field, value, reason
):
    path = _record(home)
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))

    with pytest.raises(ConfigurationError, match=reason):
        require_passing_link_record(_policy(home))


def test_a_record_that_cannot_be_parsed_is_refused(home, one_volume):
    path = _record(home)
    path.write_text("{not json")

    with pytest.raises(ConfigurationError, match="cannot be read"):
        require_passing_link_record(_policy(home))


def test_a_policy_change_asks_for_a_new_probe(home, one_volume, monkeypatch):
    _record(home)
    original = SeatbeltPolicy.profile
    monkeypatch.setattr(
        SeatbeltPolicy, "profile", lambda self: original(self).replace("(deny file-link", ";")
    )

    with pytest.raises(ConfigurationError, match="another policy text"):
        require_passing_link_record(_policy(home))


@pytest.mark.parametrize("where", ["above", "equal", "inside"])
def test_a_writable_root_over_the_record_directory_is_refused(home, one_volume, where):
    _record(home)
    records = home / "home" / "seatbelt-link-probes"
    root = {"above": home / "home", "equal": records, "inside": records / "sub"}[where]
    root.mkdir(parents=True, exist_ok=True)
    policy = SeatbeltPolicy(writable_roots=(str(root),))

    with pytest.raises(ConfigurationError, match="link probe record directory"):
        require_passing_link_record(policy)


def test_every_writable_roots_volume_is_checked(home, monkeypatch):
    other = "00000000-0000-4000-8000-000000000003"
    policy = _policy(home)
    workspace = policy.writable_roots[0]
    monkeypatch.setattr(
        seatbelt_link_probe,
        "volume_identity",
        lambda path: (
            ("/Volumes/Work", other) if str(path) == workspace else ("/Volumes/Test", _UUID)
        ),
    )
    _record(home)

    with pytest.raises(ConfigurationError, match="/Volumes/Work"):
        require_passing_link_record(policy)

    _record(home, "/Volumes/Work", other)
    require_passing_link_record(policy)


def test_a_record_for_another_policy_shape_is_refused(home, one_volume):
    # A launch policy whose roots or read-only paths differ from the shape the probe measured
    # (here: the workspace's .git no longer read-only) has another digest.
    policy = _policy(home)
    _record(home)
    narrower = SeatbeltPolicy(writable_roots=policy.writable_roots)

    with pytest.raises(ConfigurationError, match="another policy text"):
        require_passing_link_record(narrower)


def test_the_probe_writes_exactly_the_required_arms(home, monkeypatch):
    # Without the sandbox: every link call answers as refused, which is enough to see the keys.
    monkeypatch.setattr(
        seatbelt_link_probe, "_ln", lambda policy, cwd, source, name: (1, "Operation not permitted")
    )
    arms, digest = seatbelt_link_probe._probe(home / "root")

    assert set(arms) == set(REQUIRED_ARMS)
    assert digest == policy_digest(_policy(home / "root"))


def test_a_failing_diskutil_is_named_as_the_cause(monkeypatch, tmp_path):
    monkeypatch.setattr(seatbelt_link_probe, "_mount_point", lambda path: "/Volumes/Test")
    monkeypatch.setattr(seatbelt_link_probe, "_DISKUTIL", "/usr/bin/false")

    with pytest.raises(VolumeIdentityError, match="info exited 1"):
        seatbelt_link_probe.volume_identity(tmp_path)


def test_roots_on_one_device_ask_for_the_volume_once(home, monkeypatch):
    asked: list = []

    def identity(path):
        asked.append(path)
        return "/Volumes/Test", _UUID

    monkeypatch.setattr(seatbelt_link_probe, "volume_identity", identity)
    _record(home)
    require_passing_link_record(_policy(home))

    assert len(asked) == 1

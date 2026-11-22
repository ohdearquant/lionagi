# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether the Seatbelt write-set policy refuses a hard link out of its roots on a volume.

A hard link gives a file a second name; made inside a writable root to a file outside every
root, or to one in a read-only path such as the workspace's ``.git``, it lets the confined
process write that file. On the internal disk Seatbelt refuses such a link by the policy's write
rules. On a volume mounted with ownership ignored (``noowners``, the default for an external
disk) it does not, and only the policy's file-link rules stop it. Which volumes honour those
rules is measured, not assumed: this probe makes a workspace and a file outside it on the
volume, tries the links under the policy the API write set runs, and writes a record keyed by
the volume. ``require_passing_link_record`` refuses a write-capable API launch when any
writable root's volume has no passing record for this kernel and this policy text.

Run ``python -m lionhub.tools.seatbelt_link_probe --scratch <directory on the volume>``; with
no ``--scratch`` the probe uses ``<lionagi home>/state/seatbelt-link-probe`` and so measures the
volume that holds the lionagi home.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from lionagi._errors import ConfigurationError

from ._seatbelt import SeatbeltPolicy, _within

__all__ = (
    "REQUIRED_ARMS",
    "VolumeIdentityError",
    "link_record_dir",
    "probe",
    "require_passing_link_record",
    "volume_identity",
    "write_record",
)

_RECORD_FORMAT = 1
_DF = "/bin/df"
_DISKUTIL = "/usr/sbin/diskutil"
_LN = "/bin/ln"
_TIMEOUT_SECONDS = 30
_EVIDENCE_CHARS = 240

#: Every arm a record must hold as passed for a launch to proceed.
REQUIRED_ARMS: tuple[str, ...] = (
    "link_out_of_roots_refused",
    "link_from_read_only_refused",
    "link_through_symlink_refused",
    "link_to_outside_name_refused",
    "link_inside_root_works",
)


class VolumeIdentityError(ConfigurationError):
    """The volume holding a path cannot be named by a mount point and a volume UUID."""


def link_record_dir() -> Path:
    """Where link records live: ``LIONAGI_HOME/seatbelt-link-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "seatbelt-link-probes"


def _mount_point(path: Path) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 -- an absolute system binary and a resolved path
            [_DF, "-P", str(path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VolumeIdentityError(
            f"the mount point of {str(path)!r} cannot be read: {exc}"
        ) from None
    lines = proc.stdout.splitlines()
    fields = lines[-1].split(None, 5) if proc.returncode == 0 and len(lines) >= 2 else []
    if len(fields) < 6 or not fields[5].startswith("/"):
        raise VolumeIdentityError(
            f"the mount point of {str(path)!r} cannot be read: {_DF} -P exited "
            f"{proc.returncode} {proc.stderr.strip()[:_EVIDENCE_CHARS]!r}"
        )
    return fields[5]


def volume_identity(path: str | os.PathLike[str]) -> tuple[str, str]:
    """The mount point and the volume UUID of the volume that holds ``path``.

    The UUID is read from ``diskutil info``; a volume without one (a network or a RAM volume)
    cannot be keyed and raises ``VolumeIdentityError``, so a launch there is refused.
    """
    resolved = Path(os.path.realpath(os.path.expanduser(path)))
    mount = _mount_point(resolved)
    try:
        proc = subprocess.run(  # noqa: S603 -- an absolute system binary and a mount point
            [_DISKUTIL, "info", "-plist", mount],
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise VolumeIdentityError(
                f"the volume UUID of {mount!r} cannot be read: {_DISKUTIL} info exited "
                f"{proc.returncode} "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:_EVIDENCE_CHARS]!r}"
            )
        info = plistlib.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, ExpatError) as exc:
        raise VolumeIdentityError(f"the volume UUID of {mount!r} cannot be read: {exc}") from None
    uuid = info.get("VolumeUUID") if isinstance(info, dict) else None
    if not isinstance(uuid, str) or not uuid:
        raise VolumeIdentityError(
            f"the volume mounted at {mount!r} has no volume UUID, so no link probe record "
            "can be keyed to it; put the workspace on a volume diskutil names by UUID"
        )
    return mount, uuid


def _kernel() -> str:
    uname = os.uname()
    return f"{uname.sysname} {uname.release} {uname.version}"


def policy_digest(policy: SeatbeltPolicy) -> str:
    """SHA-256 of ``policy``'s text with every option that renders rules turned on: its own
    number of writable roots and read-only paths, the network and terminal rules, and one
    unreadable entry. Paths travel as parameters, so the text records the policy's shape and
    every rule any launch can render, not its paths. The probe records the digest of the policy
    it measured and a launch compares its own, so a change to any rule, or to the roots and
    read-only paths the write set is built with, asks for a new probe."""
    text = SeatbeltPolicy(
        writable_roots=policy.writable_roots,
        read_only=policy.read_only,
        network=True,
        pty=True,
        deny_read=("/unreadable",),
    ).profile()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_path(uuid: str) -> Path:
    return link_record_dir() / f"volume-{uuid}.json"


def _temp_roots() -> list[Path]:
    named = ["/tmp", "/var/tmp", tempfile.gettempdir()]  # noqa: S108
    if os.environ.get("TMPDIR"):
        named.append(os.environ["TMPDIR"])
    return list(dict.fromkeys(Path(os.path.realpath(name)) for name in named))


def _scratch_parent(scratch: str | os.PathLike[str] | None) -> Path:
    """The directory the probe works in, created when absent; refused inside a temp directory,
    which the policy makes writable, so a file placed there would not lie outside the roots."""
    from lionagi._paths import LIONAGI_HOME

    chosen = (
        scratch if scratch is not None else Path(LIONAGI_HOME) / "state" / "seatbelt-link-probe"
    )
    parent = Path(os.path.realpath(os.path.expanduser(chosen)))
    for temp in _temp_roots():
        if parent == temp or temp in parent.parents:
            raise ConfigurationError(
                f"the scratch directory {str(parent)!r} is inside the temp directory "
                f"{str(temp)!r}, which the policy makes writable; give a directory on the "
                "volume to measure, outside /tmp and $TMPDIR, with --scratch"
            )
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def _ln(policy: SeatbeltPolicy, cwd: Path, source: str, name: str) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603 -- sandbox-exec, the policy and /bin/ln on probe paths
        policy.wrap([_LN, source, name]),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    return proc.returncode, proc.stderr.strip()[-_EVIDENCE_CHARS:]


def _links(path: Path) -> int:
    try:
        return path.stat().st_nlink
    except OSError:
        return 0


def _refused_arm(
    policy: SeatbeltPolicy, ws: Path, source: str, name: str, *, target: Path
) -> dict[str, Any]:
    """A link that must be refused: a non-zero exit with the sandbox's refusal text (the
    environment pins ``LC_ALL=C`` so that text is English), no entry at ``name`` (relative to
    the workspace, or absolute), and ``target``, the file the link would name, with the link
    count it had before."""
    before = _links(target)
    rc, err = _ln(policy, ws, source, name)
    made = os.path.lexists(ws / name)
    links = _links(target)
    passed = (
        rc != 0
        and not made
        and before >= 1
        and links == before
        and "Operation not permitted" in err
    )
    return {
        "passed": passed,
        "evidence": (
            f"ln {source} {name}: rc={rc} made={made} target_links={before}->{links} {err!r}"
        ),
    }


def _probe(root: Path) -> tuple[dict[str, dict[str, Any]], str]:
    ws = root / "workspace"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_text("original\n", encoding="utf-8")
    (ws / "inside.txt").write_text("inside\n", encoding="utf-8")
    outside = root / "outside" / "target.txt"
    outside.parent.mkdir()
    outside.write_text("original\n", encoding="utf-8")
    (ws / "outside-alias").symlink_to(outside)
    # The policy the API write set builds for this workspace, with a uv cache of its own as a
    # write-capable API leg gets, so the shared uv cache is not a root here.
    policy = SeatbeltPolicy.for_workspace(ws, per_leg_uv_cache=True)
    for root_path in policy.writable_roots:
        if _within(outside, Path(root_path)):
            raise ConfigurationError(
                f"the probe's outside file {str(outside)!r} lies inside the writable root "
                f"{root_path!r}; give a --scratch directory outside it"
            )
    placed = outside.parent / "placed-link"
    arms = {
        "link_out_of_roots_refused": _refused_arm(
            policy, ws, str(outside), "out-link", target=outside
        ),
        "link_from_read_only_refused": _refused_arm(
            policy, ws, ".git/config", "git-link", target=ws / ".git" / "config"
        ),
        # `ln` follows a symlink source, and a confined command can make the symlink itself.
        "link_through_symlink_refused": _refused_arm(
            policy, ws, "outside-alias", "alias-link", target=outside
        ),
        # A file of the command's own, given a name outside the roots.
        "link_to_outside_name_refused": _refused_arm(
            policy, ws, "inside.txt", str(placed), target=ws / "inside.txt"
        ),
    }
    before = _links(ws / "inside.txt")
    rc, err = _ln(policy, ws, "inside.txt", "inside-link")
    links = _links(ws / "inside.txt")
    arms["link_inside_root_works"] = {
        "passed": rc == 0 and before >= 1 and links == before + 1,
        "evidence": f"ln inside.txt inside-link: rc={rc} st_nlink={before}->{links} {err!r}",
    }
    if outside.read_text(encoding="utf-8") != "original\n":
        arms["link_out_of_roots_refused"]["passed"] = False
        arms["link_out_of_roots_refused"]["evidence"] += " (the outside file changed)"
    return arms, policy_digest(policy)


def probe(
    scratch: str | os.PathLike[str] | None = None,
) -> tuple[str, str, dict[str, dict], str]:
    """Run every arm on the volume holding ``scratch``; its mount point, its volume UUID, arm
    name to ``{"passed": bool, "evidence": str}``, and the digest of the policy measured. The
    working directory is a fresh subdirectory of ``scratch``, removed afterwards."""
    parent = _scratch_parent(scratch)
    mount, uuid = volume_identity(parent)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        if volume_identity(root) != (mount, uuid):
            raise VolumeIdentityError(f"the probe directory {str(root)!r} is not on {mount!r}")
        arms, digest = _probe(root)
        return mount, uuid, arms, digest
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_record(
    mount: str, uuid: str, arms: Mapping[str, Mapping[str, Any]], policy_sha256: str
) -> Path:
    """Write the record ``require_passing_link_record`` reads for the volume, replacing any
    earlier one; ``policy_sha256`` is ``policy_digest`` of the policy the arms ran under."""
    path = _record_path(uuid)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "mount": mount,
        "volume_uuid": uuid,
        "kernel": _kernel(),
        "policy_sha256": policy_sha256,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _record_problem(uuid: str, digest: str) -> str | None:
    path = _record_path(uuid)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "has no link probe record"
    except (OSError, ValueError) as exc:
        return f"has a link probe record that cannot be read ({exc})"
    if not isinstance(record, dict) or record.get("format") != _RECORD_FORMAT:
        return "has a link probe record of another format"
    if record.get("volume_uuid") != uuid:
        return "has a link probe record naming another volume"
    if record.get("kernel") != _kernel():
        return "has a link probe record made under another kernel"
    if record.get("policy_sha256") != digest:
        return "has a link probe record made for another policy text"
    arms = record.get("arms")
    failed = [
        name
        for name in REQUIRED_ARMS
        if not (isinstance(arms, dict) and isinstance(arms.get(name), dict))
        or arms[name].get("passed") is not True
    ]
    if failed:
        return f"has a link probe record whose arms did not all pass ({', '.join(failed)})"
    return None


def require_passing_link_record(policy: SeatbeltPolicy) -> None:
    """Refuse a launch under ``policy`` unless every writable root's volume has a passing link
    record for this kernel and this policy text, and no writable root holds the record
    directory or lies inside it, where the confined process could write a record.

    Raises:
        ConfigurationError: a root's volume cannot be named, has no record, or its record is
            stale or failed; or a root overlaps the record directory.
    """
    records = Path(os.path.realpath(link_record_dir()))
    digest = policy_digest(policy)
    checked: set[str] = set()
    devices: set[int] = set()
    for root in policy.writable_roots:
        root_path = Path(root)
        if _within(root_path, records) or _within(records, root_path):
            raise ConfigurationError(
                f"the writable root {root!r} contains or lies inside the link probe record "
                f"directory {str(records)!r}, where a confined command could write a record; "
                "start the agent from another directory"
            )
        try:
            device: int | None = os.stat(root_path).st_dev
        except OSError:
            device = None
        if device is not None and device in devices:
            continue
        mount, uuid = volume_identity(root_path)
        if device is not None:
            devices.add(device)
        if uuid in checked:
            continue
        checked.add(uuid)
        problem = _record_problem(uuid, digest)
        if problem is not None:
            raise ConfigurationError(
                f"the volume mounted at {mount!r}, which holds the writable root {root!r}, "
                f"{problem}, so whether the sandbox refuses a hard link out of its roots there "
                "is not measured. Run `python -m lionhub.tools.seatbelt_link_probe --scratch "
                f"<a directory on {mount}, outside any workspace>` and launch again"
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the volume holding ``--scratch`` and write its record; 0 only when every arm
    passed."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.tools.seatbelt_link_probe")
    parser.add_argument(
        "--scratch",
        help=(
            "directory on the volume to measure, outside any workspace (default: "
            "<lionagi home>/state/seatbelt-link-probe); never inside /tmp or $TMPDIR"
        ),
    )
    args = parser.parse_args(argv)
    from ._seatbelt import seatbelt_available

    if not seatbelt_available():
        print("this host has no Seatbelt sandbox (macOS /usr/bin/sandbox-exec)", file=sys.stderr)
        return 2
    try:
        mount, uuid, arms, digest = probe(args.scratch)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = write_record(mount, uuid, arms, digest)
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"volume: {mount} ({uuid})")
    print(f"record: {path}")
    return 0 if all(arm["passed"] for arm in arms.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

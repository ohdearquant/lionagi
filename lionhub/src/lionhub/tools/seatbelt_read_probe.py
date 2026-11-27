# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether the Seatbelt policy with read roots refuses a read outside them on a volume.

ADR-0041 Amendment 7 lets a profile name paths an agent's child may not read, and leaves a list of
the paths it may read until some route has been measured denying reads by default. This probe
measures lionagi's own Seatbelt policy with read roots (``SeatbeltPolicy.read_roots``): reads are
denied by default and allowed under the writable roots, the read roots and the system set, and the
directories above those roots can have their metadata read, so the paths below them resolve.

The probe makes a workspace and a file outside it on the volume, and plants one file each under
``/tmp``, ``/var/tmp`` and ``/Users/Shared``, removed afterwards. Under the policy built for that
workspace it runs a shell, the interpreter and a child that reads one path, then writes a record
keyed by the volume and holding the kernel, the read roots and the digest of the policy text. One
arm runs the same read without read roots, and that read must succeed, so a refusal by the other
arms is the read rules' doing. No launch reads the record yet.

Run ``python -m lionhub.tools.seatbelt_read_probe --scratch <directory on the volume>``; with no
``--scratch`` the probe uses ``<lionagi home>/state/seatbelt-read-probe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lionagi._errors import ConfigurationError

from ._seatbelt import SeatbeltPolicy, _within
from .seatbelt_link_probe import VolumeIdentityError, _kernel, _scratch_parent, volume_identity

__all__ = (
    "REQUIRED_ARMS",
    "policy_digest",
    "probe",
    "read_record_dir",
    "write_record",
)

_RECORD_FORMAT = 1
_SH = "/bin/sh"
_TIMEOUT_SECONDS = 30
_EVIDENCE_CHARS = 240

#: Every arm a record must hold as passed for the policy to count as holding on its volume.
REQUIRED_ARMS: tuple[str, ...] = (
    "control_reads_outside_without_read_roots",
    "read_inside_root_works",
    "read_outside_refused",
    "read_through_symlink_refused",
    "stat_outside_refused",
    "list_parent_refused",
    "read_via_tmp_link_refused",
    "read_via_var_link_refused",
    "read_users_shared_refused",
    "shell_runs_in_root",
    "python_starts_with_site",
)

#: Directories outside every root where the probe plants a file to read, each spelled as a
#: command would write it. On macOS ``/tmp`` and ``/var`` are links to ``/private/tmp`` and
#: ``/private/var``, so a policy could match one spelling and miss the other; ``/Users/Shared``
#: lies outside both the home directory and the system set.
_PLANTED: tuple[tuple[str, str], ...] = (
    ("read_via_tmp_link_refused", "/tmp"),  # noqa: S108
    ("read_via_var_link_refused", "/var/tmp"),  # noqa: S108
    ("read_users_shared_refused", "/Users/Shared"),
)

# Runs as `python -I -S -c`: one operation on one path, printed as `<op>=ok:<n>` (bytes read, the
# size, or the number of entries) or `<op>=<errno name>`.
_CHILD = """import errno, os, sys
op, path = sys.argv[1], sys.argv[2]
try:
    if op == "read":
        with open(path, "rb") as f:
            n = len(f.read())
    elif op == "stat":
        n = os.stat(path).st_size
    else:
        n = len(os.listdir(path))
except OSError as e:
    print(op + "=" + str(errno.errorcode.get(e.errno, e.errno)))
else:
    print(op + "=ok:" + str(n))
"""
_RESULT = re.compile(r"^(read|stat|list)=(ok:\d+|E[A-Z0-9]+)$")


def read_record_dir() -> Path:
    """Where read records live: ``LIONAGI_HOME/seatbelt-read-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "seatbelt-read-probes"


def policy_digest(policy: SeatbeltPolicy) -> str:
    """SHA-256 of ``policy``'s text with its read roots (none counts as an empty set) and every
    option that renders rules turned on: the network and terminal rules and one unreadable entry.
    Paths travel as parameters, so the text records how many roots, read roots, parent
    directories and metadata-only paths the measured policy had and every rule it rendered; how a
    launch's own policy is matched to a record is for the launch key that reads it."""
    text = SeatbeltPolicy(
        writable_roots=policy.writable_roots,
        read_only=policy.read_only,
        network=True,
        pty=True,
        deny_read=("/unreadable",),
        read_roots=policy.read_roots if policy.read_roots is not None else (),
        read_metadata=policy.read_metadata,
    ).profile()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_path(uuid: str) -> Path:
    return read_record_dir() / f"volume-{uuid}.json"


def _run(policy: SeatbeltPolicy, cwd: Path, argv: Sequence[str]) -> tuple[int, str, str]:
    """``argv`` under ``policy`` in ``cwd``: its exit code, stdout and stderr, stripped."""
    try:
        proc = subprocess.run(  # noqa: S603 -- sandbox-exec, the policy and fixed probe commands
            policy.wrap(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _evidence(label: str, rc: int, out: str, err: str) -> str:
    return f"{label}: rc={rc} out={out[-_EVIDENCE_CHARS:]!r} err={err[-_EVIDENCE_CHARS:]!r}"


def _child(policy: SeatbeltPolicy, cwd: Path, op: str, path: Path) -> tuple[str | None, str]:
    """What the reading child printed for ``op`` on ``path``: ``ok:<n>``, an errno name, or None
    when its last line is no result for ``op`` (it did not start, or it crashed); and the
    evidence."""
    rc, out, err = _run(policy, cwd, [sys.executable, "-I", "-S", "-c", _CHILD, op, str(path)])
    lines = out.splitlines()
    match = _RESULT.match(lines[-1]) if lines else None
    result = match.group(2) if match is not None and match.group(1) == op else None
    return result, _evidence(f"{op} {path}", rc, out, err)


def _works(policy: SeatbeltPolicy, cwd: Path, op: str, path: Path, n: int) -> dict[str, Any]:
    """An operation that must succeed: the child printed ``ok`` with the expected count."""
    result, evidence = _child(policy, cwd, op, path)
    return {"passed": result == f"ok:{n}", "evidence": evidence}


def _refused(policy: SeatbeltPolicy, cwd: Path, op: str, path: Path) -> dict[str, Any]:
    """An operation that must be refused: the child started and printed ``EPERM``, the error
    Seatbelt gives. A child that did not start, or printed anything else, fails the arm."""
    result, evidence = _child(policy, cwd, op, path)
    return {"passed": result == "EPERM", "evidence": evidence}


def _probe(root: Path) -> tuple[dict[str, dict[str, Any]], str, tuple[str, ...]]:
    ws = root / "workspace"
    ws.mkdir()
    inside = ws / "inside.txt"
    inside.write_text("inside\n", encoding="utf-8")
    outside = root / "outside" / "target.txt"
    outside.parent.mkdir()
    outside.write_text("outside text\n", encoding="utf-8")
    (ws / "outside-alias").symlink_to(outside)
    # The write set's policy for this workspace, with a uv cache of its own as a write-capable
    # API leg gets, and read roots with nothing beyond the system set.
    policy = SeatbeltPolicy.for_workspace(ws, per_leg_uv_cache=True, read_roots=())
    read_roots = policy.read_roots or ()
    for path in (outside, root):
        for readable in (*policy.writable_roots, *read_roots):
            if _within(path, Path(readable)):
                raise ConfigurationError(
                    f"the probe's path {str(path)!r} lies inside the readable root "
                    f"{readable!r}; give a --scratch directory outside it"
                )
    unrestricted = SeatbeltPolicy(
        policy.writable_roots, policy.read_only, child_env=policy.child_env
    )
    arms = {
        "control_reads_outside_without_read_roots": _works(
            unrestricted, ws, "read", outside, len("outside text\n")
        ),
        "read_inside_root_works": _works(policy, ws, "read", inside, len("inside\n")),
        "read_outside_refused": _refused(policy, ws, "read", outside),
        # Seatbelt judges the path a link resolves to, and a confined command can make the link.
        "read_through_symlink_refused": _refused(policy, ws, "read", ws / "outside-alias"),
        "stat_outside_refused": _refused(policy, ws, "stat", outside),
        # The directory above the workspace: its metadata is readable, its entries are not.
        "list_parent_refused": _refused(policy, ws, "list", root),
    }
    for name, directory in _PLANTED:
        try:
            fd, planted = tempfile.mkstemp(prefix="lionagi-read-probe-", dir=directory)
        except OSError as exc:
            arms[name] = {
                "passed": False,
                "evidence": f"no file could be planted in {directory}: {exc}",
            }
            continue
        try:
            os.write(fd, b"outside text\n")
            os.close(fd)
            real = Path(os.path.realpath(planted))
            for readable in (*policy.writable_roots, *read_roots):
                if _within(real, Path(readable)):
                    raise ConfigurationError(
                        f"the probe's file {planted!r} lies inside the readable root "
                        f"{readable!r}, so no refusal can be measured there; set TMPDIR to "
                        "a directory outside /tmp and /var/tmp"
                    )
            arms[name] = _refused(policy, ws, "read", Path(planted))
        finally:
            Path(planted).unlink(missing_ok=True)
    rc, out, err = _run(policy, ws, [_SH, "-c", "/bin/ls"])
    arms["shell_runs_in_root"] = {
        "passed": rc == 0 and "inside.txt" in out.split(),
        "evidence": _evidence("/bin/sh -c /bin/ls", rc, out, err),
    }
    rc, out, err = _run(policy, ws, [sys.executable, "-I", "-c", "import sys; print('python=ok')"])
    arms["python_starts_with_site"] = {
        "passed": rc == 0 and out.splitlines()[-1:] == ["python=ok"],
        "evidence": _evidence(f"{sys.executable} -I -c", rc, out, err),
    }
    if outside.read_text(encoding="utf-8") != "outside text\n":
        raise ConfigurationError(f"the probe's outside file {str(outside)!r} changed")
    return arms, policy_digest(policy), read_roots


def probe(
    scratch: str | os.PathLike[str] | None = None,
) -> tuple[str, str, dict[str, dict], str, tuple[str, ...]]:
    """Run every arm on the volume holding ``scratch``; its mount point, its volume UUID, arm
    name to ``{"passed": bool, "evidence": str}``, the digest of the policy measured and its read
    roots. The working directory is a fresh subdirectory of ``scratch``, removed afterwards."""
    if not sys.executable:
        raise ConfigurationError("no Python interpreter to run the probe's children")
    from lionagi._paths import LIONAGI_HOME

    parent = _scratch_parent(
        scratch if scratch is not None else Path(LIONAGI_HOME) / "state" / "seatbelt-read-probe"
    )
    mount, uuid = volume_identity(parent)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        if volume_identity(root) != (mount, uuid):
            raise VolumeIdentityError(f"the probe directory {str(root)!r} is not on {mount!r}")
        arms, digest, read_roots = _probe(root)
        return mount, uuid, arms, digest, read_roots
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_record(
    mount: str,
    uuid: str,
    arms: Mapping[str, Mapping[str, Any]],
    policy_sha256: str,
    read_roots: Sequence[str],
) -> Path:
    """Write the read record for the volume, replacing any earlier one; ``policy_sha256`` is
    ``policy_digest`` of the policy the arms ran under and ``read_roots`` its read roots."""
    path = _record_path(uuid)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "mount": mount,
        "volume_uuid": uuid,
        "kernel": _kernel(),
        "policy_sha256": policy_sha256,
        "read_roots": list(read_roots),
        "probed_at": datetime.now(UTC).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the volume holding ``--scratch`` and write its record; 0 only when every arm
    passed."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.tools.seatbelt_read_probe")
    parser.add_argument(
        "--scratch",
        help=(
            "directory on the volume to measure, outside any workspace (default: "
            "<lionagi home>/state/seatbelt-read-probe); never inside /tmp or $TMPDIR"
        ),
    )
    args = parser.parse_args(argv)
    from ._seatbelt import seatbelt_available

    if not seatbelt_available():
        print("this host has no Seatbelt sandbox (macOS /usr/bin/sandbox-exec)", file=sys.stderr)
        return 2
    try:
        mount, uuid, arms, digest, read_roots = probe(args.scratch)
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = write_record(mount, uuid, arms, digest, read_roots)
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"read roots: {', '.join(read_roots)}")
    print(f"volume: {mount} ({uuid})")
    print(f"record: {path}")
    return 0 if all(arm["passed"] for arm in arms.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

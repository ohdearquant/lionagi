# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether a codex permission profile that denies reads by default holds, for one codex binary.

ADR-0041 Amendment 7 lets a profile name paths an agent's child may not read, and leaves a list of
the paths it may read until some route has been measured denying reads by default. This probe
measures codex. ``permission_profile_table`` given ``read_roots`` denies ``/``, ``/tmp`` and
``$TMPDIR`` and grants a read entry for each read root; the probe's read roots are one directory
of its own and the system set lionagi's own Seatbelt policy reads from. The probe asks three
things of the binary: that it runs under such a table, that each narrower read entry wins over
the denial of ``/``, and that a shell and the interpreter still start.

It runs the binary twice against the loopback stand-in provider of ``codex_sandbox_probe``, each
time in a scratch working directory with a scratch ``CODEX_HOME``. The first run's table has no
read roots, and its read of a file outside them must succeed, so a refusal in the second run is
the read rules' doing. The second run's table has the read roots. A read counts as refused only
when the command ran, failed, printed ``Operation not permitted`` and printed none of the file.
The probe plants one file each under ``/tmp``, ``/var/tmp`` and ``/Users/Shared``, removed
afterwards, and reads each by the spelling a command would write. The record is keyed as the
sandbox probe's is, by the binary's resolved path, version and bytes, and no launch reads it yet.

Run ``python -m lionhub.providers.openai.codex_read_probe``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lionagi._errors import ConfigurationError
from lionhub.providers.openai import codex_sandbox_probe as sandbox_probe
from lionhub.providers.openai._codex_permissions import (
    binary_sha256,
    codex_binary_version,
    deny_entries,
    permission_profile_table,
    probe_record_path,
)

__all__ = (
    "REQUIRED_ARMS",
    "main",
    "probe",
    "read_record_dir",
    "read_record_path",
    "write_record",
)

_RECORD_FORMAT = 1

#: Every arm a record must hold as passed for the binary to count as denying reads by default.
REQUIRED_ARMS: tuple[str, ...] = (
    "table_runs",
    "control_reads_outside_without_read_roots",
    "read_inside_root_works",
    "read_cwd_works",
    "write_cwd_works",
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
#: ``/private/var``, so a profile could match one spelling and miss the other; ``/Users/Shared``
#: lies outside both the home directory and the system set.
_PLANTED: tuple[tuple[str, str], ...] = (
    ("read_via_tmp_link_refused", "/tmp"),  # noqa: S108
    ("read_via_var_link_refused", "/var/tmp"),  # noqa: S108
    ("read_users_shared_refused", "/Users/Shared"),
)

#: What a sandbox's refusal of a read prints, from the command that was refused.
_REFUSAL = "Operation not permitted"

#: What the interpreter runs to show it starts, its site directory included.
_PYTHON_CODE = "print('python=ok')"


def read_record_dir() -> Path:
    """Where read records live: ``LIONAGI_HOME/codex-read-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "codex-read-probes"


def read_record_path(resolved: str, version: str, sha256: str) -> Path:
    """The read record for one binary, named as its sandbox probe record is named."""
    return read_record_dir() / probe_record_path(resolved, version, sha256).name


def _system_read_roots() -> tuple[str, ...]:
    from lionhub.tools._seatbelt import _system_read_roots as system_read_roots

    return system_read_roots()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _refused(text: str | None, marker: str | None = None) -> bool:
    """The step ran and failed with the sandbox's refusal, and printed no ``marker``."""
    rc = sandbox_probe._rc(text)
    shown = text or ""
    return (
        rc not in (None, 0, 127) and _REFUSAL in shown and (marker is None or marker not in shown)
    )


def _ok(text: str | None, marker: str | None = None) -> bool:
    return sandbox_probe._ok(text, marker)


def _arm(passed: bool, *texts: str | None, note: str = "") -> dict[str, Any]:
    return sandbox_probe._arm(passed, *texts, note=note)


def _probe(
    binary: str, runner: sandbox_probe.Runner, root: Path
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    from lionhub.agent.child_sandbox import resolve_child_sandbox

    nonce = uuid.uuid4().hex[:12]
    marks = {n: f"{n.upper()}-{nonce}" for n in ("inside", "cwd", "outside", "planted")}
    cwd, readroot, outside_dir = root / "cwd", root / "readroot", root / "outside"
    homes = {n: root / f"home-{n}" for n in ("control", "roots")}
    for path in (cwd, readroot, outside_dir, *homes.values()):
        path.mkdir(parents=True)
    inside, cwd_file = readroot / "inside.txt", cwd / "cwd.txt"
    outside, alias = outside_dir / "target.txt", readroot / "outside-alias"
    inside.write_text(marks["inside"] + "\n")
    cwd_file.write_text(marks["cwd"] + "\n")
    outside.write_text(marks["outside"] + "\n")
    alias.symlink_to(outside)

    read_roots = (str(readroot), *_system_read_roots())
    for path in (root, outside_dir):
        for readable in read_roots:
            if _within(path, Path(readable)):
                raise ConfigurationError(
                    f"the probe's path {str(path)!r} lies inside the read root {readable!r}; "
                    "give a --scratch directory outside it"
                )
    policy_deny = list(resolve_child_sandbox(None).deny_read)

    def table(home: Path, roots: Sequence[str] | None) -> dict[str, Any]:
        # A heavy table with the network off, so the only reads it narrows are the read rules'.
        return permission_profile_table(
            deny=deny_entries(policy_deny, home, sandbox_probe._env(home)),
            write=[],
            light=False,
            read_only=False,
            network=False,
            read_roots=roots,
        )

    q = shlex.quote
    control = sandbox_probe._run(
        binary,
        runner,
        commands=[("outside", f"/bin/cat {q(str(outside))}")],
        table=table(homes["control"], None),
        home=homes["control"],
        cwd=cwd,
    )

    arms: dict[str, dict[str, Any]] = {}
    planted: list[tuple[str, Path]] = []
    try:
        for name, directory in _PLANTED:
            try:
                fd, made = tempfile.mkstemp(prefix="lionagi-read-probe-", dir=directory)
            except OSError as exc:
                arms[name] = {
                    "passed": False,
                    "evidence": f"no file could be planted in {directory}: {exc}",
                }
                continue
            os.write(fd, (marks["planted"] + "\n").encode())
            os.close(fd)
            planted.append((name, Path(made)))
            real = Path(os.path.realpath(made))
            for readable in read_roots:
                if _within(real, Path(readable)):
                    raise ConfigurationError(
                        f"the probe's file {made!r} lies inside the read root {readable!r}, so "
                        "no refusal can be measured there"
                    )
        python = sandbox_probe._child_python()
        commands = [
            ("inside", f"/bin/cat {q(str(inside))}"),
            ("cwd_read", f"/bin/cat {q(str(cwd_file))}"),
            ("cwd_write", f"echo w > {q(str(cwd / 'w'))}"),
            ("outside", f"/bin/cat {q(str(outside))}"),
            # The sandbox judges the path a link resolves to, and a confined command can make the
            # link.
            ("symlink", f"/bin/cat {q(str(alias))}"),
            ("stat", f"/bin/ls -d {q(str(outside))}"),
            # The directory above the working directory and the read root.
            ("list_parent", f"/bin/ls {q(str(root))}"),
            *((name, f"/bin/cat {q(str(made))}") for name, made in planted),
            ("shell", "/bin/ls"),
            ("python", f"{q(python)} -I -c {q(_PYTHON_CODE)}"),
        ]
        roots_table = table(homes["roots"], read_roots)
        run = sandbox_probe._run(
            binary,
            runner,
            commands=commands,
            table=roots_table,
            home=homes["roots"],
            cwd=cwd,
        )
    finally:
        for _, made in planted:
            made.unlink(missing_ok=True)

    answered = sum(1 for output in run.values() if output is not None)
    arms["table_runs"] = {
        "passed": answered == len(run),
        "evidence": (
            f"{answered} of {len(run)} steps of the run under the read-roots table came back; "
            "none means codex did not run under the table"
        ),
    }
    arms["control_reads_outside_without_read_roots"] = _arm(
        _ok(control["outside"], marks["outside"]), control["outside"]
    )
    arms["read_inside_root_works"] = _arm(_ok(run["inside"], marks["inside"]), run["inside"])
    arms["read_cwd_works"] = _arm(_ok(run["cwd_read"], marks["cwd"]), run["cwd_read"])
    arms["write_cwd_works"] = _arm(
        _ok(run["cwd_write"]) and (cwd / "w").is_file(), run["cwd_write"]
    )
    arms["read_outside_refused"] = _arm(_refused(run["outside"], marks["outside"]), run["outside"])
    arms["read_through_symlink_refused"] = _arm(
        _refused(run["symlink"], marks["outside"]), run["symlink"]
    )
    arms["stat_outside_refused"] = _arm(_refused(run["stat"]), run["stat"])
    arms["list_parent_refused"] = _arm(
        _refused(run["list_parent"], readroot.name), run["list_parent"]
    )
    for name, _ in planted:
        arms[name] = _arm(_refused(run[name], marks["planted"]), run[name])
    arms["shell_runs_in_root"] = _arm(
        _ok(run["shell"]) and cwd_file.name in (run["shell"] or "").split(), run["shell"]
    )
    arms["python_starts_with_site"] = _arm(_ok(run["python"], "python=ok"), run["python"])
    if outside.read_text() != marks["outside"] + "\n":
        raise ConfigurationError(f"the probe's outside file {str(outside)!r} changed")
    return {name: arms[name] for name in REQUIRED_ARMS}, read_roots, roots_table


def probe(
    binary: str,
    *,
    runner: sandbox_probe.Runner = sandbox_probe.run_codex,
    scratch: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...], dict[str, Any]]:
    """Run every arm against ``binary``: arm name to ``{"passed": bool, "evidence": str}``,
    the read roots measured, and the read-roots table the second run carried.

    ``scratch`` is a directory to make the probe's working directory in; it defaults to
    ``<lionagi home>/state/codex-read-probe`` and is refused inside ``/tmp`` or ``$TMPDIR``. The
    working directory is a fresh subdirectory, removed afterwards.
    """
    from lionagi import _paths

    parent = sandbox_probe._scratch_parent(
        scratch if scratch is not None else Path(_paths.LIONAGI_HOME) / "state" / "codex-read-probe"
    )
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        return _probe(binary, runner, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_record(
    binary: str,
    arms: Mapping[str, Mapping[str, Any]],
    read_roots: Sequence[str],
    table: Mapping[str, Any],
) -> Path:
    """Write the read record for ``binary``, replacing any earlier one; ``read_roots`` and
    ``table`` are what the arms ran under."""
    resolved = os.path.realpath(binary)
    version = codex_binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    sha256 = binary_sha256(resolved)
    if sha256 is None:
        raise RuntimeError(f"the bytes of {binary!r} cannot be read")
    path = read_record_path(resolved, version, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
        "sha256": sha256,
        "read_roots": list(read_roots),
        "table": dict(table),
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the codex binary on PATH and write its read record; 0 only when every arm passed."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.providers.openai.codex_read_probe")
    parser.add_argument("--binary", help="codex binary to probe (default: codex on PATH)")
    parser.add_argument(
        "--scratch",
        help=(
            "directory to make the probe's working directory in (default: "
            "<lionagi home>/state/codex-read-probe); never inside /tmp or $TMPDIR"
        ),
    )
    args = parser.parse_args(argv)
    binary = args.binary or shutil.which("codex")
    if not binary:
        print("codex is not on PATH", file=sys.stderr)
        return 2
    try:
        arms, read_roots, table = probe(
            binary, scratch=Path(args.scratch) if args.scratch else None
        )
    except (sandbox_probe.ScratchRootError, ConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    path = write_record(binary, arms, read_roots, table)
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"read roots: {', '.join(read_roots)}")
    print(f"record: {path}")
    return 0 if all(arm["passed"] for arm in arms.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

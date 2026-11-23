# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Whether Claude Code refuses what lionagi's rendered deny rules name, for one ``claude`` binary.

ADR-0048 Amendment 1 lets a claude_code leg carry the deny and ask rules of a Claude Code settings
file, rendered by :func:`lionhub.hooks.hooks_from.plan_carry` so that each rule names in the leg the
files it named where it was written (H6). The amendment merges the submit wiring only with a record
that shows the child refuse a scripted read or command under each rendered shape, beside a control
with no rule. This probe makes that record.

It writes a project settings file holding one rule of each source shape, renders it through
``plan_carry`` with the leg's working directory apart from the project directory, and runs the
binary twice against the read probe's loopback stand-in for the Messages API
(:mod:`lionhub.providers.anthropic.claude_read_probe`): once with an empty deny list, the control,
and once with the rendered rules. The two runs have the same tools, allowed tools and steps, so a
refusal the control did not see is the rules'. The shapes:

- ``//<dir>/x``, from ``Read(/config/key.txt)``: the file under the project directory and the same
  path under the leg's working directory.
- ``//<dir>/**/<name>``, from ``Read(.env)``: ``.env`` at the top of the leg's working directory and
  two directories down.
- ``~/x``, from ``Read(~/.probe/secret.txt)``, under the run's own ``HOME``.
- an ask rule rendered as deny, from the ask rule ``Read(private/**)``.
- a ``Bash`` rule, ``Bash(touch:*)``.

A Read step counts as refused when its tool result is an error that shows none of the file. The
Bash step counts as refused when its tool result is an error and the file it would make is absent.
Each refusal arm passes only when the control run did the same step. A file no rule names, beside
the anchored one, must still be read in the run with rules, so a release that refuses every read
cannot pass.

The child gets the read probe's flags and environment (``--bare``, no MCP server, ``--tools`` and
``--allowed-tools Bash,Read``, ``--permission-prompts none``, a placeholder login, the proxy
variables pointed at a listener that refuses everything). Claude Code's sandbox stays off, so the
arms measure the permission rules alone.

The record is written to ``LIONAGI_HOME/claude-deny-probes``, keyed by the binary's resolved path,
version and bytes, with the source rules and the rendered ones. No launch reads it yet.

Run ``python -m lionhub.providers.anthropic.claude_deny_probe``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lionagi._errors import ConfigurationError
from lionhub.hooks.hooks_from import HooksFromError, plan_carry
from lionhub.providers.anthropic import claude_read_probe as read_probe

__all__ = (
    "REQUIRED_ARMS",
    "SOURCE_RULES",
    "deny_record_dir",
    "deny_record_path",
    "main",
    "probe",
    "write_record",
)

_RECORD_FORMAT = 1

#: Runs one claude command line, ``(argv, env, cwd)``, and returns its exit status, -1 when it
#: outlived the timeout. Injected so tests need no binary.
Runner = Callable[[list[str], dict[str, str], Path], int]

#: The ``permissions`` of the project settings file the probe renders: one rule of each source
#: shape the rendered shapes come from.
SOURCE_RULES: dict[str, tuple[str, ...]] = {
    "deny": (
        "Read(/config/key.txt)",
        "Read(.env)",
        "Read(~/.probe/secret.txt)",
        "Bash(touch:*)",
    ),
    "ask": ("Read(private/**)",),
}

#: Every arm a record must hold as passed for the rendered shapes to count as refused.
REQUIRED_ARMS: tuple[str, ...] = (
    "rules_rendered",
    "runs_under_settings",
    "anchored_project_file_refused",
    "anchored_leg_file_refused",
    "any_depth_name_top_refused",
    "any_depth_name_nested_refused",
    "home_rule_refused",
    "ask_rule_as_deny_refused",
    "bash_rule_refused",
    "uncovered_file_read_works",
    "no_other_outbound",
)

#: Read steps: the step's name, the arm it decides, and its file under the probe's root.
_READS: tuple[tuple[str, str, str], ...] = (
    ("anchored_project", "anchored_project_file_refused", "project/config/key.txt"),
    ("anchored_leg", "anchored_leg_file_refused", "leg/config/key.txt"),
    ("env_top", "any_depth_name_top_refused", "leg/.env"),
    ("env_nested", "any_depth_name_nested_refused", "leg/a/b/.env"),
    ("private", "ask_rule_as_deny_refused", "leg/private/p.txt"),
    ("uncovered", "uncovered_file_read_works", "leg/config/other.txt"),
)

#: The file the ``~/`` rule names, under each run's ``HOME``.
_HOME_FILE = ".probe/secret.txt"


def _absolute(directory: Path) -> str:
    return "//" + str(directory).strip("/")


def _expected_rules(project: Path, leg: Path) -> list[str]:
    """The rule text ADR-0048 H6 gives for :data:`SOURCE_RULES` with these two directories."""
    p, g = _absolute(project), _absolute(leg)
    return [
        f"Read({p}/config/key.txt)",
        f"Read({g}/config/key.txt)",
        f"Read({p}/**/.env)",
        f"Read({g}/**/.env)",
        "Read(~/.probe/secret.txt)",
        "Bash(touch:*)",
        f"Read({p}/**/private/**)",
        f"Read({g}/**/private/**)",
    ]


def _settings(rules: Sequence[str]) -> dict[str, Any]:
    """The ``--settings`` the child gets: the deny rules and nothing else."""
    return {"permissions": {"deny": list(rules)}}


def _run(
    binary: str,
    runner: Runner,
    *,
    steps: Sequence[tuple[str, Mapping[str, Any]]],
    rules: Sequence[str],
    home: Path,
    config: Path,
    cwd: Path,
) -> tuple[dict[str, tuple[str, bool] | None], dict[str, Any]]:
    """One claude run: step name to the tool result the child sent back (None when it never
    did), and what the run was given and what reached the two listeners."""
    stand_in = read_probe.MessagesStandIn([step for _, step in steps])
    proxy = read_probe.ProxyRecorder()
    settings = _settings(rules)
    argv = read_probe._argv(binary, settings)
    with read_probe._serve(stand_in) as base_url, read_probe._serve(proxy) as proxy_url:
        env = read_probe._env(base_url, proxy_url, home, config)
        status = runner(argv, env, cwd)
    results = {name: stand_in.results.get(f"toolu_probe_{i}") for i, (name, _) in enumerate(steps)}
    run = {
        "argv": argv,
        "env_names": sorted(env),
        "exit_status": status,
        "requests": list(stand_in.seen),
        "proxied": list(proxy.seen),
        "settings": settings,
    }
    return results, run


def _control_note(control_ok: bool) -> str:
    if control_ok:
        return "the control run did the same step with the same tools and permissions"
    return "the control run did not do this step, so a refusal here is not the rules'"


def _probe(
    binary: str, runner: Runner, root: Path
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    nonce = uuid.uuid4().hex[:12]
    project, leg = root / "project", root / "leg"
    homes = {n: root / f"home-{n}" for n in ("control", "rules")}
    configs = {n: root / f"config-{n}" for n in ("control", "rules")}
    for path in (project / ".claude", leg, *configs.values()):
        path.mkdir(parents=True)
    marks: dict[str, str] = {}
    for name, _, relative in _READS:
        marks[name] = f"{name.upper()}-{nonce}"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marks[name] + "\n")
    marks["home"] = f"HOME-{nonce}"
    for home in homes.values():
        secret = home / _HOME_FILE
        secret.parent.mkdir(parents=True)
        secret.write_text(marks["home"] + "\n")

    source = project / ".claude" / "settings.json"
    document = {"permissions": {kind: list(rules) for kind, rules in SOURCE_RULES.items()}}
    source.write_text(json.dumps(document, indent=2) + "\n")
    try:
        plan = plan_carry(
            json.loads(source.read_text()),
            fmt="claude",
            carry=["deny"],
            source_path=str(source),
            leg_cwd=str(leg),
            home=str(homes["rules"]),
        )
    except HooksFromError as exc:
        raise ConfigurationError(f"the probe's own rules did not render: {exc}") from None
    rules = list(plan.deny_rules)

    q = shlex.quote
    touched = {n: leg / f"touched-{n}" for n in homes}

    def steps(run: str) -> list[tuple[str, dict[str, Any]]]:
        return [
            *((name, read_probe._read(root / relative)) for name, _, relative in _READS),
            ("home", read_probe._read(homes[run] / _HOME_FILE)),
            ("bash", read_probe._bash(f"touch {q(str(touched[run]))}")),
        ]

    control, control_run = _run(
        binary,
        runner,
        steps=steps("control"),
        rules=[],
        home=homes["control"],
        config=configs["control"],
        cwd=leg,
    )
    ruled, rules_run = _run(
        binary,
        runner,
        steps=steps("rules"),
        rules=rules,
        home=homes["rules"],
        config=configs["rules"],
        cwd=leg,
    )

    arms: dict[str, dict[str, Any]] = {}
    arms["rules_rendered"] = {
        "passed": sorted(rules) == sorted(_expected_rules(project, leg))
        and plan.ask_rules_as_deny == 1,
        "evidence": (
            f"{len(rules)} rules rendered, {plan.ask_rules_as_deny} of them from ask rules: "
            + ", ".join(rules)
        ),
    }
    answered = sum(1 for result in ruled.values() if result is not None)
    arms["runs_under_settings"] = {
        "passed": answered == len(ruled),
        "evidence": (
            f"{answered} of {len(ruled)} steps of the run with the rules came back, exit status "
            f"{rules_run['exit_status']}; none means claude did not run under the settings"
        ),
    }
    for name, arm, _ in _READS:
        control_ok = read_probe._read_ok(control[name], marks[name])
        if name == "uncovered":
            passed = control_ok and read_probe._read_ok(ruled[name], marks[name])
            arms[arm] = read_probe._arm(passed, control[name], ruled[name])
            continue
        passed = control_ok and read_probe._read_refused(ruled[name], marks[name])
        arms[arm] = read_probe._arm(
            passed, control[name], ruled[name], note=_control_note(control_ok)
        )
    control_ok = read_probe._read_ok(control["home"], marks["home"])
    arms["home_rule_refused"] = read_probe._arm(
        control_ok and read_probe._read_refused(ruled["home"], marks["home"]),
        control["home"],
        ruled["home"],
        note=_control_note(control_ok),
    )
    control_ok = read_probe._bash_ok(control["bash"]) and touched["control"].is_file()
    ran = touched["rules"].exists()
    bash = ruled["bash"]
    arms["bash_rule_refused"] = read_probe._arm(
        control_ok and bash is not None and bash[1] and not ran,
        control["bash"],
        bash,
        note=_control_note(control_ok)
        + (
            "; the command ran: its file exists"
            if ran
            else "; the file the command makes is absent"
        ),
    )
    proxied = [*control_run["proxied"], *rules_run["proxied"]]
    arms["no_other_outbound"] = {
        "passed": not proxied,
        "evidence": (
            f"{len(proxied)} requests reached the listener the proxy variables name"
            + (f": {', '.join(proxied)[: read_probe._EVIDENCE_CHARS]}" if proxied else "")
            + "; this covers only traffic that honours the proxy variables"
        ),
    }
    rendering = {
        "source": document["permissions"],
        "rendered": rules,
        "ask_rules_as_deny": plan.ask_rules_as_deny,
        "project_dir": str(project),
        "leg_cwd": str(leg),
    }
    runs = {"control": control_run, "rules": rules_run}
    return {name: arms[name] for name in REQUIRED_ARMS}, rendering, runs


def _scratch_parent(scratch: Path | str | None) -> Path:
    """The directory the probe makes its root in, created when absent: a caller-given path, else
    ``<lionagi home>/state/claude-deny-probe``."""
    from lionagi import _paths

    chosen = (
        scratch
        if scratch is not None
        else Path(_paths.LIONAGI_HOME) / "state" / "claude-deny-probe"
    )
    parent = Path(os.path.realpath(os.path.expanduser(chosen)))
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def probe(
    binary: str,
    *,
    runner: Runner | None = None,
    scratch: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Run every arm against ``binary``: arm name to ``{"passed": bool, "evidence": str}``, the
    source and rendered rules, and what each of the two runs was given and saw.

    ``runner`` defaults to the read probe's ``run_claude``. ``scratch`` is a directory to make the
    probe's root in; the root is a fresh subdirectory, removed afterwards. Its real path is used, so
    the rendered rules name the files by the spelling Claude Code resolves.
    """
    parent = _scratch_parent(scratch)
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="p-", dir=parent)))
    try:
        return _probe(binary, runner or read_probe.run_claude, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def deny_record_dir() -> Path:
    """Where deny records live: ``LIONAGI_HOME/claude-deny-probes``."""
    from lionagi._paths import LIONAGI_HOME

    return Path(LIONAGI_HOME) / "claude-deny-probes"


def deny_record_path(resolved: str, version: str, sha256: str) -> Path:
    """The record for one binary, named by its resolved path, version and the SHA-256 of its
    bytes: a binary replaced in place under the same path and version has no record."""
    digest = hashlib.sha256(f"{resolved}\0{version}\0{sha256}".encode()).hexdigest()[:24]
    return deny_record_dir() / f"claude-{version}-{digest}.json"


def write_record(
    binary: str,
    arms: Mapping[str, Mapping[str, Any]],
    rendering: Mapping[str, Any],
    runs: Mapping[str, Any],
) -> Path:
    """Write the deny record for ``binary``, replacing any earlier one."""
    resolved = os.path.realpath(binary)
    version = read_probe._binary_version(binary)
    if version is None:
        raise RuntimeError(f"the version of {binary!r} cannot be read")
    sha256 = read_probe._sha256(resolved)
    if sha256 is None:
        raise RuntimeError(f"the bytes of {binary!r} cannot be read")
    path = deny_record_path(resolved, version, sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "format": _RECORD_FORMAT,
        "binary": resolved,
        "version": version,
        "sha256": sha256,
        "rules": dict(rendering),
        "runs": {name: dict(run) for name, run in runs.items()},
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "arms": {name: dict(arm) for name, arm in arms.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Probe the claude binary on PATH and write its deny record; 0 only when every arm passed."""
    parser = argparse.ArgumentParser(prog="python -m lionhub.providers.anthropic.claude_deny_probe")
    parser.add_argument("--binary", help="claude binary to probe (default: claude on PATH)")
    parser.add_argument(
        "--scratch",
        help=(
            "directory to make the probe's root in (default: "
            "<lionagi home>/state/claude-deny-probe)"
        ),
    )
    args = parser.parse_args(argv)
    binary = args.binary or shutil.which("claude")
    if not binary:
        print("claude is not on PATH", file=sys.stderr)
        return 2
    try:
        arms, rendering, runs = probe(binary, scratch=Path(args.scratch) if args.scratch else None)
        path = write_record(binary, arms, rendering, runs)
    except (ConfigurationError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for name, arm in arms.items():
        print(f"{'pass' if arm['passed'] else 'FAIL'}  {name}  {arm['evidence']}")
    print(f"record: {path}")
    return 0 if all(arm["passed"] for arm in arms.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

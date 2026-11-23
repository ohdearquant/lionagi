# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Capture what ``codex exec --json`` writes for a few fixed prompts, as fixtures.

A codex run whose prompt a hook stops before the agent starts ends with an empty console, and a
run that starts and writes nothing looks the same from outside. Telling the two apart needs
what codex itself streams in each case, so this module records it::

    python -m lionhub.providers.openai.codex_exec_capture --out <new directory>

Each case runs ``codex exec --json`` once with a prompt fixed in this file, in an empty
working directory under ``--out``, with the read-only sandbox and no MCP server, and keeps the
standard output, the standard error and the exit status. The full command line is printed
before each run. Nothing is written outside ``--out`` by this module; the codex child writes
its own session files under the codex home it is given.

Cases:

- ``no_output``: a run that starts, is not blocked, and is asked to write nothing.
- ``unknown_model``: a run naming a model the account does not have, so the provider answers
  with an error. It is not a content refusal. Whether that error comes after the thread starts
  is what the capture records (``error_after_thread_started`` in the manifest); only when it
  does is the run an example of an error after start.
- ``hook_blocked`` (only with ``--hook-case``): a run in a scratch codex home whose one
  ``UserPromptSubmit`` hook refuses every prompt. The hook's trust record is computed by this
  package's own hash, so a wrong hash shows as a run that was not blocked.

The first two run under the operator's codex home, passed to the child by path and never
opened here, with ``--ignore-user-config`` so the user config loads no MCP server and runs no
hook. The hook case runs under a scratch home this module writes: ``hooks.json`` and a
``config.toml`` holding only that hook's trust record, so the user config it loads declares no
MCP server. No sign-in file is read, copied or written: the scratch home has none, so that case
runs only where the environment the child inherits already signs codex in. Codex may install
files into a fresh home and start processes there that outlive the run; this module stops
nothing, and the manifest lists the entries the run added to the scratch home.

The manifest keeps, per case, the command line, the exit status and the ``type`` of each line
of the JSON stream, never the text of an event.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lionhub.providers._cli_subprocess import _redact_secrets_for_log
from lionhub.providers.openai._codex_hook_trust import (
    TRUSTED,
    hook_hash,
    hook_identity,
    hook_trust_report,
)
from lionhub.providers.openai._codex_permissions import codex_binary_version
from lionhub.providers.openai._codex_project_layers import (
    _codex_home,
    project_layers,
    system_mcp_server_names,
)

__all__ = (
    "CASES",
    "Case",
    "CaptureRefusedError",
    "capture",
    "case_argv",
    "main",
    "stream_shape",
)

#: Runs one command line: ``(argv, env, cwd)`` to ``(exit status, stdout, stderr)``. Injected
#: so tests need no codex binary.
Runner = Callable[[list[str], dict[str, str], Path], tuple[int, str, str]]

_RUN_TIMEOUT_SECONDS = 300
#: A model name no account has, so the provider answers the started turn with an error.
UNAVAILABLE_MODEL = "lionagi-capture-no-such-model"
#: What the blocking hook writes to stderr before it exits 2.
HOOK_REASON = "lionagi capture: this prompt was blocked by a test hook"
#: Variable names whose values are removed from the captured output.
_SECRET_NAME_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass(frozen=True)
class Case:
    """One capture: its prompt, the codex home it runs under, and its extra arguments."""

    name: str
    prompt: str
    scratch_home: bool
    extra_args: tuple[str, ...] = ()


CASES: tuple[Case, ...] = (
    Case(
        name="no_output",
        prompt=(
            "Do not write any reply text and do not call any tool. End your turn immediately "
            "with an empty message."
        ),
        scratch_home=False,
    ),
    Case(
        name="unknown_model",
        prompt="Reply with the single word: ok.",
        scratch_home=False,
        extra_args=("-m", UNAVAILABLE_MODEL),
    ),
    Case(
        name="hook_blocked",
        prompt="Reply with the single word: ok.",
        scratch_home=True,
    ),
)


class CaptureRefusedError(Exception):
    """A condition under which the capture would not show what it claims; nothing ran."""


def case_argv(binary: str, case: Case, cwd: Path) -> list[str]:
    """The ``codex exec`` command line for ``case`` run in ``cwd``.

    The read-only sandbox, the working directory and JSON output on every case.
    ``--ignore-user-config`` on the cases under the operator's home, so its config loads no MCP
    server and runs no hook; not on the hook case, whose trust record lives in the scratch
    home's own config. The prompt follows ``--`` so it is never read as an option.
    """
    argv = [binary, "exec", "--json"]
    if not case.scratch_home:
        argv.append("--ignore-user-config")
    argv += ["--sandbox", "read-only", "--skip-git-repo-check", "-C", str(cwd)]
    argv += [*case.extra_args, "--", case.prompt]
    return argv


def _run(argv: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    done = subprocess.run(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=_RUN_TIMEOUT_SECONDS,
        check=False,
    )
    return done.returncode, done.stdout, done.stderr


def _prepare_out(out: Path) -> Path:
    """``out`` created, refused when it exists and is not an empty directory."""
    if out.exists() or out.is_symlink():
        if not out.is_dir() or out.is_symlink() or any(out.iterdir()):
            raise CaptureRefusedError(f"--out {str(out)!r} exists and is not an empty directory")
    else:
        out.mkdir(parents=True)
    return Path(os.path.realpath(out))


def _refuse_mcp_sources(cwd: Path, env: Mapping[str, str]) -> None:
    """Refuse when a project layer above ``cwd`` or the system config could load an MCP
    server or other settings into a run in ``cwd``."""
    try:
        layers = [str(path) for path, _text in project_layers(cwd, env)]
    except Exception as exc:
        raise CaptureRefusedError(f"the codex project configs above the runs: {exc}") from exc
    if layers:
        raise CaptureRefusedError(
            f"a codex project config sits above {str(cwd)!r} ({', '.join(layers)}); choose an "
            "--out outside it so no project layer reaches the runs"
        )
    try:
        servers = system_mcp_server_names(set())
    except Exception as exc:
        raise CaptureRefusedError(f"the codex system config cannot be read: {exc}") from exc
    if servers:
        raise CaptureRefusedError(
            f"the codex system config declares MCP servers {sorted(servers)!r}, which every run "
            "would load"
        )


def _write_hook_home(home: Path) -> str:
    """Write the scratch home's blocking hook and its trust record; return the entry name."""
    home.mkdir()
    script = home / "block-prompt.sh"
    script.write_text(f"#!/bin/sh\necho {shlex.quote(HOOK_REASON)} >&2\nexit 2\n")
    script.chmod(0o755)
    handler = {"type": "command", "command": str(script)}
    (home / "hooks.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [handler]}]}}, indent=2) + "\n"
    )
    identity = hook_identity("UserPromptSubmit", None, handler)
    if identity is None:
        raise CaptureRefusedError("the blocking hook has a type codex skips")
    # The entry name starts with the home as codex resolves it; the trust report names it.
    declared = hook_trust_report(Path(os.path.realpath(home))).hooks
    if len(declared) != 1:
        raise CaptureRefusedError(f"the scratch home declares {len(declared)} hooks, not 1")
    name = declared[0].hook.name
    (home / "config.toml").write_text(
        f"[hooks.state.{json.dumps(name)}]\n"
        f"enabled = true\n"
        f"trusted_hash = {json.dumps(hook_hash(identity))}\n"
    )
    report = hook_trust_report(Path(os.path.realpath(home)))
    statuses = {trust.hook.name: trust.status for trust in report.hooks}
    if report.problems or statuses != {name: TRUSTED}:
        raise CaptureRefusedError(
            f"the scratch home's hook does not read as trusted: {statuses!r} {report.problems!r}"
        )
    return name


_ERROR_TYPES = frozenset({"error", "turn.failed"})


def stream_shape(stdout: str) -> dict[str, object]:
    """The ``type`` of each line of a ``codex exec --json`` stream, and whether the first error
    event came after ``thread.started``: ``None`` when there is no error event.

    A line that is not a JSON object with a text ``type`` is recorded as ``"<other>"``.
    """
    types: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        kind = event.get("type") if isinstance(event, dict) else None
        types.append(kind if isinstance(kind, str) else "<other>")
    started = types.index("thread.started") if "thread.started" in types else None
    failed = next((i for i, kind in enumerate(types) if kind in _ERROR_TYPES), None)
    after: bool | None = None
    if failed is not None:
        after = started is not None and started < failed
    return {
        "event_types": types,
        "thread_started": started is not None,
        "error_after_thread_started": after,
    }


def _entries(home: Path | None) -> set[str]:
    if home is None or not home.is_dir():
        return set()
    return {entry.name for entry in home.iterdir()}


def _secret_values(env: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if value and any(part in name.upper() for part in _SECRET_NAME_PARTS)
    }


def capture(
    out: Path,
    *,
    binary: str = "codex",
    hook_case: bool = False,
    env: Mapping[str, str] | None = None,
    runner: Runner = _run,
    say: Callable[[str], None] = print,
) -> dict[str, object]:
    """Run the cases into ``out`` and return the manifest also written there.

    Every refusal comes before the first run, so a refused capture runs nothing.
    """
    base_env = dict(os.environ if env is None else env)
    try:
        operator_home = _codex_home(base_env)
    except Exception as exc:
        raise CaptureRefusedError(f"the operator's codex home: {exc}") from exc
    out = _prepare_out(Path(out))
    cases = [case for case in CASES if hook_case or not case.scratch_home]
    plans = []
    for case in cases:
        cwd = out / case.name / "cwd"
        cwd.mkdir(parents=True)
        child_env = dict(base_env)
        if case.scratch_home:
            home = out / case.name / "codex-home"
            _write_hook_home(home)
            child_env["CODEX_HOME"] = str(home)
        else:
            child_env["CODEX_HOME"] = str(operator_home)
        _refuse_mcp_sources(cwd, child_env)
        plans.append((case, cwd, child_env))
    secrets = _secret_values(base_env)
    manifest: dict[str, object] = {
        "codex_version": codex_binary_version(binary),
        "binary": binary,
        "cases": {},
    }
    for case, cwd, child_env in plans:
        argv = case_argv(binary, case, cwd)
        say(f"[{case.name}] CODEX_HOME={shlex.quote(child_env['CODEX_HOME'])} " + shlex.join(argv))
        scratch = Path(child_env["CODEX_HOME"]) if case.scratch_home else None
        before = _entries(scratch)
        try:
            status, stdout, stderr = runner(argv, child_env, cwd)
        except subprocess.TimeoutExpired:
            status, stdout, stderr = -1, "", f"timed out after {_RUN_TIMEOUT_SECONDS} s"
        except OSError as exc:
            status, stdout, stderr = -1, "", f"could not start: {exc}"
        where = out / case.name
        (where / "stdout.jsonl").write_text(_redact_secrets_for_log(stdout, secrets))
        (where / "stderr.txt").write_text(_redact_secrets_for_log(stderr, secrets))
        (where / "exit_status.txt").write_text(f"{status}\n")
        record: dict[str, object] = {
            "argv": argv,
            "codex_home": "scratch" if case.scratch_home else "operator",
            "exit_status": status,
            "stderr_bytes": len(stderr.encode()),
            **stream_shape(stdout),
        }
        if scratch is not None:
            record["home_entries_added"] = sorted(_entries(scratch) - before)
        manifest["cases"][case.name] = record
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lionhub.providers.openai.codex_exec_capture",
        description="Capture codex exec --json output for fixed prompts into a new directory.",
    )
    parser.add_argument("--out", required=True, type=Path, help="a new or empty directory")
    parser.add_argument("--binary", default="codex", help="the codex binary (default: codex)")
    parser.add_argument(
        "--hook-case",
        action="store_true",
        help="also run the hook-blocked case in a scratch codex home",
    )
    args = parser.parse_args(argv)
    try:
        manifest = capture(args.out, binary=args.binary, hook_case=args.hook_case)
    except CaptureRefusedError as refusal:
        print(f"codex_exec_capture: {refusal}", file=sys.stderr)
        return 2
    print(json.dumps(manifest["cases"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

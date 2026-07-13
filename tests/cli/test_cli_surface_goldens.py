# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CLI contract goldens, part 2: full-registry surface + in-process error paths.

Complements ``tests/cli/test_cli_contracts.py`` (which pins ``li agent`` /
``li schedule`` / ``li schedule create`` / ``li monitor`` flag sets and exit
codes via out-of-process ``subprocess`` calls to the real entrypoint). This
module extends that coverage in three ways rather than duplicating it:

1. It walks the *whole* top-level command registry and the ``li o flow`` /
   ``li o fanout`` and remaining ``li schedule`` subcommand surfaces (list,
   get, limits, enable, disable, trigger, delete, runs) that the existing
   file does not pin — a command silently added to (or removed from) the
   registry, or a flag silently added to/removed from a spawn-forwarding
   subcommand, now fails a golden until the change is deliberate.
2. Every case here is invoked **in-process** — calling ``lionagi.cli.main.main``
   directly and either catching the ``SystemExit`` argparse raises or reading
   its plain integer return value — instead of spawning a subprocess. This
   is materially faster and avoids any shared-state assumptions across
   ``pytest-xdist`` workers that an out-of-process test could accidentally
   pick up (it can't: nothing here touches the filesystem outside ``tmp_path``
   or the network).
3. It pins, end-to-end through the real CLI entrypoint, the specific
   regression class this whole test area exists to guard: a spawn-forwarding
   surface (``li agent``, ``li o flow``, ``li o fanout``) silently accepting
   a bad input — concretely, a ``--cwd`` that does not exist — instead of
   failing loudly before anything is spawned or persisted. A prior version of
   ``li agent --cwd <typo'd path>`` let the underlying provider silently
   create the directory and report a clean, completed-ok run; the fix makes
   every one of these three surfaces validate ``--cwd`` before allocating a
   run record or spawning a provider process. The tests below monkeypatch
   the run-allocation function each surface calls to raise if reached, then
   assert the *validation* error is what actually propagates — proving
   allocation was never reached, not merely that the process exits non-zero.

A note on one substitution: the source generic guidance for this test area
mentions pinning an "invalid --effort value" exit code. ``--effort`` is
deliberately free text with no argparse ``choices=`` — it is lower-cased and
passed through unvalidated so a newly supported provider effort tier is never
silently rejected by a stale allowlist (see
``lionagi/service/providers.py::normalize_effort``). There is therefore no
cheap, hermetic "invalid --effort" error to pin without mocking a live
provider spawn. ``--trigger-type`` and ``--action-kind`` on
``li schedule create`` are the CLI's actual argparse-``choices=``-validated
flags and serve the same "invalid enum value" contract this module pins
instead.

Flake rule: every assertion here is deterministic — pure argparse
introspection, or an in-process call whose only external dependency is a
nonexistent path the test itself constructs under ``tmp_path``. A flaking
case is a bug in the test, not grounds to skip it in place.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import lionagi.cli.main as cli_main
from lionagi._errors import ConfigurationError

# ---------------------------------------------------------------------------
# Shared introspection + invocation helpers
# ---------------------------------------------------------------------------


def _command_parser(command: str, *subs: str) -> argparse.ArgumentParser:
    """Build the real parser for a command (and optional nested subcommand)
    the same way `li` does, and return the subcommand's parser object.

    Mirrors the helper in test_cli_contracts.py: introspecting
    ``option_strings``/``_actions`` off the live parser is immune to
    argparse's rendered-help formatting changing across Python versions.
    """
    spec = cli_main._COMMAND_BY_NAME[command]
    parser, _ = cli_main._build_parser(spec)
    for name in (command, *subs):
        sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        parser = sub_action.choices[name]
    return parser


def _flag_set(command: str, *subs: str) -> list[str]:
    parser = _command_parser(command, *subs)
    return sorted({s for action in parser._actions for s in action.option_strings})


def _positional_dests(command: str, *subs: str) -> list[str]:
    """Dests of positional (non-flag, non-subparsers) actions, in order."""
    parser = _command_parser(command, *subs)
    return [
        a.dest
        for a in parser._actions
        if not a.option_strings
        and a.dest != "help"
        and not isinstance(a, argparse._SubParsersAction)
    ]


def _top_level_subcommand_set() -> list[str]:
    parser, _ = cli_main._build_parser(None)
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return sorted(sub_action.choices.keys())


def _run_main(argv: list[str]) -> int:
    """Invoke the real `li` entrypoint in-process.

    Mirrors exactly what the installed console script sees at
    ``sys.exit(main())``: a ``SystemExit`` raised inside argparse (unknown
    flag, invalid choice, missing required positional, unknown subcommand)
    becomes its integer code; a plain ``return`` from ``main()`` is used
    as-is. Anything else — e.g. the unhandled ``ConfigurationError`` the
    --cwd fail-fast path raises — propagates out of this helper exactly as
    it would out of an uncaught exception in a real process; callers that
    expect that shape wrap the call in ``pytest.raises`` directly.
    """
    try:
        return cli_main.main(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1


# ---------------------------------------------------------------------------
# Goldens — sorted flag/positional sets, pinned from the actual parser tree
# ---------------------------------------------------------------------------

TOP_LEVEL_COMMANDS = [
    "agent",
    "casts",
    "dispatch",
    "doctor",
    "engine",
    "invoke",
    "kill",
    "mirror",
    "mon",
    "monitor",
    "o",
    "orchestrate",
    "schedule",
    "state",
    "stats",
    "studio",
    "team",
]

ORCHESTRATE_FANOUT_FLAGS = [
    "--agent",
    "--bypass",
    "--cwd",
    "--effort",
    "--fast",
    "--help",
    "--invocation",
    "--max-concurrent",
    "--num-workers",
    "--output",
    "--pack",
    "--project",
    "--resume-on-timeout",
    "--save",
    "--synthesis-prompt",
    "--team-mode",
    "--theme",
    "--timeout",
    "--verbose",
    "--with-synthesis",
    "--workers",
    "--yolo",
    "-a",
    "-h",
    "-n",
    "-v",
]
ORCHESTRATE_FANOUT_POSITIONALS = ["query"]

ORCHESTRATE_FLOW_FLAGS = [
    "--agent",
    "--allow-degraded-context",
    "--background",
    "--bare",
    "--bypass",
    "--cwd",
    "--dry-run",
    "--effort",
    "--fast",
    "--file",
    "--help",
    "--invocation",
    "--max-agents",
    "--max-concurrent",
    "--max-ops",
    "--notify",
    "--output",
    "--pack",
    "--playbook",
    "--project",
    "--reactive",
    "--resume",
    "--resume-on-timeout",
    "--save",
    "--show-graph",
    "--team-attach",
    "--team-mode",
    "--theme",
    "--timeout",
    "--verbose",
    "--with-synthesis",
    "--workers",
    "--yolo",
    "-a",
    "-f",
    "-h",
    "-p",
    "-v",
]
ORCHESTRATE_FLOW_POSITIONALS = ["query"]

# `li schedule` subcommands not already pinned by test_cli_contracts.py
# (which only pins `create`). Every one of these has just -h/--help for
# flags; they differ only in whether they take a positional `id`.
SCHEDULE_SIMPLE_SUBCOMMAND_POSITIONALS = {
    "list": [],
    "limits": [],
    "get": ["id"],
    "enable": ["id"],
    "disable": ["id"],
    "trigger": ["id"],
    "delete": ["id"],
    "runs": ["id"],
}


class TestTopLevelCommandRegistryGolden:
    def test_top_level_command_set_is_pinned(self):
        """A command (or alias) silently added to or removed from
        `_COMMAND_REGISTRY` must fail this golden until the change to the
        set of things `li` accepts as a first argument is deliberate."""
        assert _top_level_subcommand_set() == sorted(TOP_LEVEL_COMMANDS)


class TestOrchestrateFlagGoldens:
    def test_orchestrate_top_has_only_help(self):
        # `li o` / `li orchestrate` itself carries no flags of its own —
        # everything lives on the `fanout`/`flow` subcommands.
        assert _flag_set("orchestrate") == ["--help", "-h"]

    def test_orchestrate_subcommand_set(self):
        # `ctl` (status/pause/resume/msg) is a separate, narrower
        # control-plane surface addressed by run id, not a spawn-forwarding
        # surface — out of scope for the flag/exit-code/error-shape goldens
        # below, but its presence in the subcommand set is still pinned so a
        # new orchestrate subcommand can't slip in unnoticed.
        parser = _command_parser("orchestrate")
        sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        assert sorted(sub_action.choices.keys()) == ["ctl", "fanout", "flow"]

    def test_fanout_flag_and_positional_set(self):
        assert _flag_set("orchestrate", "fanout") == ORCHESTRATE_FANOUT_FLAGS
        assert _positional_dests("orchestrate", "fanout") == ORCHESTRATE_FANOUT_POSITIONALS

    def test_flow_flag_and_positional_set(self):
        assert _flag_set("orchestrate", "flow") == ORCHESTRATE_FLOW_FLAGS
        assert _positional_dests("orchestrate", "flow") == ORCHESTRATE_FLOW_POSITIONALS


class TestScheduleSubcommandShapeGoldens:
    @pytest.mark.parametrize("name", sorted(SCHEDULE_SIMPLE_SUBCOMMAND_POSITIONALS))
    def test_simple_subcommand_shape(self, name):
        assert _flag_set("schedule", name) == ["--help", "-h"]
        assert _positional_dests("schedule", name) == SCHEDULE_SIMPLE_SUBCOMMAND_POSITIONALS[name]


# ---------------------------------------------------------------------------
# In-process exit-code / error-shape contracts (no subprocess)
# ---------------------------------------------------------------------------


class TestInProcessContractErrors:
    def test_unknown_top_level_command_is_nonzero_and_named(self, capsys):
        rc = _run_main(["bogus-top-level-command"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "bogus-top-level-command" in captured.err
        assert captured.out == ""

    def test_flow_missing_model_or_agent_is_nonzero(self, capsys):
        rc = _run_main(["o", "flow"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "model or --agent is required" in captured.err
        assert captured.out == ""

    def test_fanout_missing_model_or_agent_is_nonzero(self, capsys):
        rc = _run_main(["o", "fanout"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "model or --agent is required" in captured.err

    def test_flow_unknown_flag_is_nonzero_and_named(self, capsys):
        rc = _run_main(["o", "flow", "--this-flag-does-not-exist"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "--this-flag-does-not-exist" in captured.err

    def test_fanout_unknown_flag_is_nonzero_and_named(self, capsys):
        rc = _run_main(["o", "fanout", "--this-flag-does-not-exist"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "--this-flag-does-not-exist" in captured.err

    def test_schedule_create_invalid_trigger_type_is_nonzero_and_named(self, capsys):
        rc = _run_main(["schedule", "create", "a-name", "--trigger-type", "bogus"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "bogus" in captured.err
        assert "--trigger-type" in captured.err

    def test_schedule_create_invalid_action_kind_is_nonzero_and_named(self, capsys):
        rc = _run_main(["schedule", "create", "a-name", "--action-kind", "bogus"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "bogus" in captured.err
        assert "--action-kind" in captured.err


# ---------------------------------------------------------------------------
# The regression class itself: nonexistent --cwd must fail fast, end-to-end
# ---------------------------------------------------------------------------


class TestNonexistentCwdFailFastEndToEnd:
    """`li agent` / `li o flow` / `li o fanout` --cwd <nonexistent> must raise
    ConfigurationError, naming the path and the flag, before the run
    allocator each surface uses is ever called — i.e. before any run row
    could be persisted and before any provider process could be spawned.
    Exercised through the real `li` argv-to-dispatch path (`main()`), not by
    calling the internal validator directly (that is already unit-tested in
    tests/cli/test_agent_cwd_failfast.py and
    tests/cli/orchestrate/test_orchestration_cwd_failfast.py) — this pins the
    contract as an external caller of `li` actually observes it.
    """

    def test_agent_cwd_failfast(self, monkeypatch, tmp_path, capsys):
        import lionagi.cli.agent as agent_mod

        def _boom():
            raise AssertionError(
                "allocate_run must not be reached — cwd validation must fire first"
            )

        monkeypatch.setattr(agent_mod, "allocate_run", _boom)
        bad_cwd = str(tmp_path / "nonexistent-workspace")

        with pytest.raises(ConfigurationError) as exc_info:
            _run_main(["agent", "--cwd", bad_cwd, "claude", "hi"])

        assert bad_cwd in str(exc_info.value)
        assert "--cwd" in str(exc_info.value)
        # `li agent` additionally logs a clean one-line diagnostic to stderr
        # before re-raising (as-observed current behavior — see
        # lionagi/cli/agent.py's `except BaseException` handler in run_agent).
        captured = capsys.readouterr()
        assert bad_cwd in captured.err
        assert captured.err.startswith("error: ")
        assert captured.out == ""

    def test_flow_cwd_failfast(self, monkeypatch, tmp_path, capsys):
        import lionagi.cli.orchestrate._orchestration as orch_mod

        def _boom(*a, **kw):
            raise AssertionError(
                "allocate_run must not be reached — cwd validation must fire first"
            )

        monkeypatch.setattr(orch_mod, "allocate_run", _boom)
        bad_cwd = str(tmp_path / "nonexistent-workspace")

        with pytest.raises(ConfigurationError) as exc_info:
            _run_main(["o", "flow", "--cwd", bad_cwd, "claude", "hi"])

        assert bad_cwd in str(exc_info.value)
        assert "--cwd" in str(exc_info.value)
        # Contract surprise, pinned as-observed (unlike `li agent`, this path
        # does not call log_error before re-raising — `_run_orch_command`'s
        # generic BaseException branch only logs for its known
        # extra_handlers, not for a bare re-raise): nothing reaches stdout or
        # stderr from CLI-owned code before the exception propagates.
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_fanout_cwd_failfast(self, monkeypatch, tmp_path):
        import lionagi.cli.orchestrate._orchestration as orch_mod

        def _boom(*a, **kw):
            raise AssertionError(
                "allocate_run must not be reached — cwd validation must fire first"
            )

        monkeypatch.setattr(orch_mod, "allocate_run", _boom)
        bad_cwd = str(tmp_path / "nonexistent-workspace")

        with pytest.raises(ConfigurationError) as exc_info:
            _run_main(["o", "fanout", "--cwd", bad_cwd, "claude", "hi"])

        assert bad_cwd in str(exc_info.value)
        assert "--cwd" in str(exc_info.value)

    def test_flow_json_output_mode_emits_no_partial_json_on_cwd_failure(self, tmp_path, capsys):
        """--output json must never leave partial/invalid JSON on stdout: an
        early config-time failure (before any DAG execution or JSON
        serialization begins) must leave stdout untouched, same as the
        text-output case above — nothing is printed until a real result
        exists to format."""
        bad_cwd = str(tmp_path / "nonexistent-workspace")

        with pytest.raises(ConfigurationError):
            _run_main(["o", "flow", "--output", "json", "--cwd", bad_cwd, "claude", "hi"])

        captured = capsys.readouterr()
        assert captured.out == ""


# ---------------------------------------------------------------------------
# Console-script entrypoint smoke test (the one deliberate subprocess case)
# ---------------------------------------------------------------------------


def _find_li_console_script() -> str | None:
    """Locate the installed `li` console script the same way a real caller
    would run it — NOT `python -m lionagi.cli`, which only proves the module
    imports, not that `[project.scripts] li = ...` is actually wired up in
    this environment (an editable reinstall has silently left a stale/broken
    console script before)."""
    candidate = Path(sys.executable).parent / "li"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("li")


class TestConsoleScriptEntryPointSmoke:
    def test_li_console_script_resolves_and_runs(self):
        li_path = _find_li_console_script()
        if li_path is None:
            pytest.skip("`li` console script not found on PATH or next to sys.executable")
        result = subprocess.run(
            [li_path, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip(), "expected a non-empty version string"

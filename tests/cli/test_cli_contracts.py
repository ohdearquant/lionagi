# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CLI contract goldens for `li agent`, `li schedule`, and `li monitor`.

These three commands are the CLI surfaces that spawn or observe other
processes (subagents, scheduled fires, running entities). The regression
class this module guards against: a spawn-forwarding surface silently
accepting invalid input (bad flag, missing required argument, unreachable
backend) instead of failing loudly with a clear diagnostic and a nonzero
exit code, and CLI flag/exit-code drift going unnoticed because nothing
pins the actual `--help` surface or the current, empirically observed
error shape.

Everything here is invoked out-of-process via `sys.executable -m
lionagi.cli.main ...` (the real `li` entrypoint per `[project.scripts]` in
pyproject.toml is `lionagi.cli.main:main`), so these tests see exactly what
an external caller of the installed `li` binary sees: real exit codes, real
stdout/stderr, no patched internals.

Flag-set goldens compare *sorted flag lists* extracted from `--help` output,
not full help text — full text (wrapped descriptions, epilogs, examples)
churns on every wording tweak; the flag set is the actual contract external
callers and scheduled actions depend on.

Anything that requires a live Studio daemon (`li studio`) with real
scheduled/session data is skipped with a reason rather than mocked — mocking
the daemon would just be testing the mock. Where an error path can be
triggered deterministically without a daemon (unreachable Studio URL,
absent state.db) by pointing the command at an empty/unreachable target via
env vars, that is exercised directly: it's cheap, hermetic, and real.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile

import pytest

import lionagi.cli.main as cli_main

_CLI = [sys.executable, "-m", "lionagi.cli.main"]


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_CLI, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _command_parser(command: str, *subs: str) -> argparse.ArgumentParser:
    """Build the real parser for a command (and optional nested subcommand)
    the same way `li` does, and return the subcommand's parser object.

    The flag-set goldens introspect `option_strings` off the parser rather
    than scraping rendered `--help` text: argparse's help formatting changes
    across Python versions (3.13 collapsed `-a AGENT, --agent AGENT` into
    `-a, --agent AGENT`), so text-scraping pins the renderer, not the
    contract. Private argparse attributes are used deliberately — they are
    the stable introspection surface for this.
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


def _subcommand_set(command: str) -> list[str]:
    parser = _command_parser(command)
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return sorted(sub_action.choices.keys())


# --- goldens: sorted flag sets, pinned from the actual parser definitions ---

AGENT_HELP_FLAGS = [
    "--agent",
    "--bypass",
    "--context-budget",
    "--context-from",
    "--continue-last",
    "--cwd",
    "--effort",
    "--fast",
    "--form",
    "--help",
    "--invocation",
    "--list-profiles",
    "--notify",
    "--preset",
    "--project",
    "--prompt",
    "--prompt-file",
    "--resume",
    "--resume-on-timeout",
    "--theme",
    "--timeout",
    "--verbose",
    "--yolo",
    "-a",
    "-c",
    "-h",
    "-r",
    "-v",
]

SCHEDULE_HELP_FLAGS = ["--help", "-h"]

SCHEDULE_SUBCOMMANDS = [
    "list",
    "get",
    "limits",
    "create",
    "enable",
    "disable",
    "trigger",
    "delete",
    "runs",
]

SCHEDULE_CREATE_HELP_FLAGS = [
    "--action-command",
    "--action-command-args",
    "--action-kind",
    "--agent",
    "--cron",
    "--cwd",
    "--description",
    "--flow-yaml",
    "--github-filter",
    "--github-repo",
    "--help",
    "--interval",
    "--max-cost-usd",
    "--max-runs",
    "--max-tokens",
    "--model",
    "--on-fail",
    "--on-success",
    "--once",
    "--playbook",
    "--poll-interval",
    "--project",
    "--prompt",
    "--threshold-config",
    "--trigger-type",
    "-h",
]

MONITOR_HELP_FLAGS = [
    "--follow",
    "--help",
    "--interval",
    "--max-wait",
    "--no-chain",
    "--project",
    "--refresh",
    "--run",
    "--since",
    "--type",
    "--watch",
    "-h",
    "-p",
    "-t",
    "-w",
]


class TestHelpFlagGoldens:
    def test_agent_help_flag_set(self):
        result = _run(["agent", "--help"])
        assert result.returncode == 0
        assert _flag_set("agent") == AGENT_HELP_FLAGS

    def test_schedule_help_flag_set_and_subcommands(self):
        result = _run(["schedule", "--help"])
        assert result.returncode == 0
        assert _flag_set("schedule") == SCHEDULE_HELP_FLAGS
        assert _subcommand_set("schedule") == sorted(SCHEDULE_SUBCOMMANDS)

    def test_schedule_create_help_flag_set(self):
        result = _run(["schedule", "create", "--help"])
        assert result.returncode == 0
        assert _flag_set("schedule", "create") == SCHEDULE_CREATE_HELP_FLAGS

    def test_monitor_help_flag_set(self):
        result = _run(["monitor", "--help"])
        assert result.returncode == 0
        assert _flag_set("monitor") == MONITOR_HELP_FLAGS


class TestExitCodesForContractErrors:
    def test_agent_unknown_flag_is_nonzero(self):
        result = _run(["agent", "--this-flag-does-not-exist"])
        assert result.returncode != 0
        assert "--this-flag-does-not-exist" in result.stderr

    def test_agent_missing_prompt_is_nonzero_and_named(self):
        # `agent`'s positional [[MODEL] PROMPT ...] is argparse-optional, so
        # argparse itself accepts zero positionals; the "a prompt is
        # required" check is enforced by run_agent, not the parser — pinning
        # this closes the gap argparse's own contract leaves open.
        result = _run(["agent"])
        assert result.returncode != 0
        assert "prompt" in result.stderr

    def test_schedule_missing_subcommand_is_nonzero_and_names_it(self):
        result = _run(["schedule"])
        assert result.returncode != 0
        assert "schedule_action" in result.stderr

    def test_schedule_create_missing_name_is_nonzero_and_names_it(self):
        result = _run(["schedule", "create"])
        assert result.returncode != 0
        assert "name" in result.stderr

    def test_schedule_get_missing_id_is_nonzero_and_names_it(self):
        result = _run(["schedule", "get"])
        assert result.returncode != 0
        assert "id" in result.stderr

    def test_monitor_invalid_type_choice_is_nonzero_and_names_it(self):
        result = _run(["monitor", "--type", "bogus-entity-kind"])
        assert result.returncode != 0
        assert "bogus-entity-kind" in result.stderr

    def test_monitor_unknown_flag_is_nonzero(self):
        # Contract surprise (pin as-observed): an unrecognized flag after
        # `monitor` is NOT reported as `li monitor: error: ...` — argparse's
        # subparser leaves it unconsumed, and it bubbles up to the top-level
        # `li` parser's own "unrecognized arguments" check instead. Still
        # nonzero and still names the offending flag, which is the contract
        # a caller actually needs.
        result = _run(["monitor", "--this-flag-does-not-exist"])
        assert result.returncode != 0
        assert "--this-flag-does-not-exist" in result.stderr


class TestErrorShapeWithoutADaemon:
    """Error shapes triggerable cheaply, with no Studio daemon and no API
    key, by pointing the command at an empty/unreachable target instead of
    relying on whatever daemon state happens to exist on the host (a real
    Studio daemon with real schedules may well be running on a dev machine
    — pointing at a closed port / empty state dir keeps this hermetic).
    """

    def test_schedule_runs_unreachable_studio_reports_diagnostic(self):
        # Hold a bound-but-never-listening socket for the duration of the
        # subprocess: the OS reserves the port for us (no other process can
        # take it), and with no listen queue every connect attempt gets a
        # deterministic ECONNREFUSED — unlike a hardcoded low port, which
        # firewalls can silently drop (hang) rather than refuse.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            env = os.environ.copy()
            env["LIONAGI_STUDIO_URL"] = f"http://127.0.0.1:{port}"
            result = _run(["schedule", "runs", "nonexistent-schedule-id"], env=env)
        assert result.returncode == 1
        assert "Cannot reach Studio" in result.stderr
        assert f"127.0.0.1:{port}" in result.stderr

    def test_monitor_detail_unknown_entity_reports_diagnostic(self):
        with tempfile.TemporaryDirectory() as empty_home:
            env = os.environ.copy()
            # An empty LIONAGI_HOME has no state.db, so the lookup takes the
            # "state.db not found" branch deterministically rather than
            # depending on whatever is recorded in the real one.
            env["LIONAGI_HOME"] = empty_home
            result = _run(["monitor", "nonexistent-entity-zzz123"], env=env)
            assert result.returncode == 0  # current behavior: prints, exits 0
            assert "nonexistent-entity-zzz123" in result.stdout
            assert "not found" in result.stdout


class TestRequiresLiveDaemonSkipped:
    """Cases whose success path genuinely needs a running `li studio`
    daemon with real data are skipped with a reason, not mocked — mocking
    the daemon's HTTP responses would only validate the mock.
    """

    @pytest.mark.skip(
        reason=(
            "li schedule list success path requires a live `li studio` "
            "daemon; mocking its HTTP responses would test the mock, not "
            "the CLI contract. The unreachable-daemon error path is covered "
            "by TestErrorShapeWithoutADaemon instead."
        )
    )
    def test_schedule_list_against_live_daemon(self):
        raise AssertionError("skipped, see reason")

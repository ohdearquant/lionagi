# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The CLI must report a broken environment as its own kind of failure.

When a dependency is missing, every command dies during import and the CLI
previously exited 1 with a traceback. A run that started and failed also exits
1, so a caller reading the exit status could not tell the two apart, and a
wrapper that expects an artifact would report the absent artifact as the
command's own result. That misattribution is the thing these tests pin: the
exit code must be distinct, and the last line of stderr must say the command
never ran.
"""

from __future__ import annotations

import importlib

import pytest

from lionagi.cli._util import EXIT_CODE_BY_STATUS, EXIT_CODE_ENVIRONMENT_ERROR

# `from lionagi.cli import main` would bind the re-exported main() function
# rather than this module, because the package attribute shadows the submodule
# of the same name, and monkeypatching an attribute on a function silently
# patches nothing a caller reads.
cli_main = importlib.import_module("lionagi.cli.main")


def test_environment_code_collides_with_no_run_status() -> None:
    """The whole point is distinguishability, so a collision defeats it.

    Asserted against the status map rather than a second literal: adding a
    status that happens to use 78 would otherwise silently re-merge the two
    cases this separation exists to keep apart.
    """
    assert EXIT_CODE_ENVIRONMENT_ERROR not in set(EXIT_CODE_BY_STATUS.values())


def test_missing_dependency_exits_with_the_environment_code(monkeypatch, caplog):
    def _boom(argv=None):
        raise ModuleNotFoundError("No module named 'sniffio'", name="sniffio")

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR

    # The summary goes through the CLI error logger, so it is captured here
    # rather than in the stderr stream that carries the traceback.
    summary = caplog.text
    assert "sniffio" in summary
    assert "not a failed run" in summary


def test_the_traceback_survives(monkeypatch, capsys):
    """The summary names the module; only the traceback names the import chain.

    Which module is missing rarely identifies the cause on its own. What does
    is the chain of module-level imports that reached it, so the summary must
    add to the traceback rather than replace it.
    """

    def _boom(argv=None):
        raise ModuleNotFoundError("No module named 'sniffio'", name="sniffio")

    monkeypatch.setattr(cli_main, "_run", _boom)
    cli_main.main([])

    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "ModuleNotFoundError" in err


def test_a_nameless_module_error_still_reports(monkeypatch, caplog):
    """``exc.name`` is optional, and a crash while reporting a crash is worse
    than a vague message."""

    def _boom(argv=None):
        raise ModuleNotFoundError("something went wrong")

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert "a required module" in caplog.text


@pytest.mark.parametrize("code", [0, 1, 2, 124, 130])
def test_ordinary_exit_codes_pass_through(monkeypatch, code):
    """The guard must not become a catch-all that rewrites real outcomes."""

    monkeypatch.setattr(cli_main, "_run", lambda argv=None: code)
    assert cli_main.main([]) == code


def test_other_exceptions_are_not_swallowed(monkeypatch):
    """Only a missing module means the environment cannot start.

    Catching more than that would convert genuine failures into environment
    reports, which is the same misattribution in the opposite direction.
    """

    def _boom(argv=None):
        raise RuntimeError("a real failure")

    monkeypatch.setattr(cli_main, "_run", _boom)

    with pytest.raises(RuntimeError, match="a real failure"):
        cli_main.main([])

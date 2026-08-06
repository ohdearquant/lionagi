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
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lionagi.cli._util import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_ENVIRONMENT_ERROR,
    begin_invocation,
    clear_run_allocation,
    end_invocation,
    mark_run_allocated,
    run_was_allocated,
)

# `from lionagi.cli import main` is not a handle on this module. What it yields
# depends on import order: the package's __getattr__ pins the re-exported
# main() function, while importing the submodule binds the module onto the same
# package attribute. Monkeypatching an attribute on whichever of the two is
# bound may silently patch nothing a caller reads, so the module is taken by
# name.
cli_main = importlib.import_module("lionagi.cli.main")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_environment_code_collides_with_no_run_status() -> None:
    """The whole point is distinguishability, so a collision defeats it.

    Asserted against the status map rather than a second literal: adding a
    status that happens to use 78 would otherwise silently re-merge the two
    cases this separation exists to keep apart.
    """
    assert EXIT_CODE_ENVIRONMENT_ERROR not in set(EXIT_CODE_BY_STATUS.values())


def test_missing_dependency_exits_with_the_environment_code(monkeypatch, reported):
    def _boom(argv=None):
        raise ModuleNotFoundError("No module named 'sniffio'", name="sniffio")

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR

    summary = "\n".join(reported)
    assert "sniffio" in summary
    # The claim a caller acts on: no run exists, so there is nothing to go and
    # read and the environment is the whole story.
    assert "No run was started" in summary


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


def test_a_nameless_module_error_still_reports(monkeypatch, reported):
    """``exc.name`` is optional, and a crash while reporting a crash is worse
    than a vague message."""

    def _boom(argv=None):
        raise ModuleNotFoundError("something went wrong")

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert "a required module" in "\n".join(reported)


@pytest.mark.parametrize("code", [0, 1, 2, 124, 130])
def test_ordinary_exit_codes_pass_through(monkeypatch, code):
    """The guard must not become a catch-all that rewrites real outcomes."""

    monkeypatch.setattr(cli_main, "_run", lambda argv=None: code)
    assert cli_main.main([]) == code


@pytest.fixture
def reported(monkeypatch) -> list[str]:
    """Collect what the CLI reports, without depending on logging configuration.

    `configure_cli_logging` turns propagation off on the CLI logger, so whether
    `caplog` sees a message depends on whether some earlier test in the session
    already configured logging. Asserting through it made these tests pass or
    fail on execution order rather than on behaviour. The message text is what
    these tests are about; that it genuinely reaches stderr, last, is pinned by
    the subprocess test below, which configures nothing and reads the real
    stream.
    """
    messages: list[str] = []
    monkeypatch.setattr(cli_main, "log_error", messages.append)
    return messages


@pytest.fixture(autouse=True)
def _no_run_allocated():
    """Keep the process-level allocation marker from leaking between tests.

    Some tests here set it deliberately, and any other test in the session that
    allocates a run sets it too. Left set, it would silently turn the assertions
    below into their opposite.
    """
    clear_run_allocation()
    yield
    clear_run_allocation()


def test_the_loader_boundary_does_not_absorb_a_missing_dependency(monkeypatch, reported):
    """A missing dependency found while loading a command is still the environment.

    Command modules are imported lazily when one is selected, and that import
    sits behind a broad `except Exception` which reports a command-scoped error
    and returns 1. A dependency missing from the environment would be caught
    there and reported as an ordinary failure, which is exactly the collision
    the distinct code exists to remove, so it has to reach the entry point
    instead.
    """

    def _boom(selected):
        raise ModuleNotFoundError("No module named 'somedep'", name="somedep")

    monkeypatch.setattr(cli_main, "build_cli_parser", _boom)

    assert cli_main.main(["doctor"]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert "somedep" in "\n".join(reported)


def test_a_missing_command_module_reports_the_environment_end_to_end():
    """The same path again, with nothing patched.

    The test above pins the boundary but supplies the error itself. Here the
    import genuinely fails: a meta-path finder refuses one command module, the
    CLI is asked for that command, and the real lazy loader raises. This is the
    configuration the feature exists for, so it is worth paying a subprocess
    for.

    A caller that keeps only the tail of stderr has to receive the diagnosis, so
    the report is asserted to be the last thing written.
    """
    script = textwrap.dedent(
        """
        import sys

        BLOCKED = "lionagi.cli.doctor"

        class _RefuseOne:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == BLOCKED:
                    raise ModuleNotFoundError(
                        f"No module named {fullname!r}", name=fullname
                    )
                return None

        sys.modules.pop(BLOCKED, None)
        sys.meta_path.insert(0, _RefuseOne())

        from lionagi.cli.main import main

        sys.exit(main(["doctor"]))
        """
    )
    # Run from the source tree, so the subprocess imports the code under test.
    # An installed lionagi may sit in the same interpreter and would otherwise
    # answer instead, which makes the test report on a build nobody changed.
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )

    assert proc.returncode == EXIT_CODE_ENVIRONMENT_ERROR, proc.stderr
    last_line = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "lionagi.cli.doctor" in last_line
    assert "No run was started" in last_line


def test_a_missing_import_after_a_run_was_allocated_is_not_an_environment_fault(monkeypatch):
    """Once a run exists on disk, the same error means something different.

    A run id, a run directory and a manifest are durable state a caller can go
    and read. Reporting that as an unusable environment would claim nothing was
    executed while the evidence says otherwise, which is the misattribution this
    feature exists to prevent, running the other way. So it propagates and is
    reported the way any other failure during a run is.
    """

    def _boom(argv=None):
        mark_run_allocated()
        raise ModuleNotFoundError("No module named 'some_provider'", name="some_provider")

    monkeypatch.setattr(cli_main, "_run", _boom)

    with pytest.raises(ModuleNotFoundError):
        cli_main.main([])


def test_a_previous_invocations_run_does_not_suppress_the_report(monkeypatch, reported):
    """The marker answers a question about *this* invocation.

    A process that runs the entry point more than once would otherwise carry the
    first invocation's allocation forward, and every later broken environment
    would be misreported as a failed run.
    """
    mark_run_allocated()

    def _boom(argv=None):
        raise ModuleNotFoundError("No module named 'sniffio'", name="sniffio")

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert "sniffio" in "\n".join(reported)


def test_an_overlapping_invocation_does_not_erase_an_allocation():
    """The reset is the dangerous operation, so it only runs when nothing is in flight.

    The two ways overlapping invocations can go wrong are not equally bad.
    Seeing another invocation's allocation only re-raises, which is what
    happened before any of this existed. Losing one asserts that nothing ran
    while a run directory sits on disk, which is the false claim the exit code
    exists to prevent, so that direction has to be impossible.
    """
    begin_invocation()
    mark_run_allocated()

    begin_invocation()  # a second invocation starts while the first is running
    assert run_was_allocated(), "the second entry erased the first one's allocation"

    end_invocation()
    end_invocation()


def test_a_finished_invocation_lets_the_next_one_reset():
    """Guarding the reset must not disable it, or the marker latches on forever.

    Once nothing is in flight the fact is stale, and a later broken environment
    in the same process has to be reported as one.
    """
    begin_invocation()
    mark_run_allocated()
    end_invocation()

    begin_invocation()
    assert not run_was_allocated()
    end_invocation()


def test_the_marker_survives_allocation_on_another_thread(monkeypatch, reported):
    """Allocation does not happen on the thread that returns the exit code.

    `run_async` drives each command's async body on its own thread with its own
    event loop, so a thread-local or a ContextVar would be invisible here and
    would report that nothing ran while a run existed. This pins the property
    that makes the process-wide marker the correct choice rather than the lazy
    one.
    """
    import threading

    def _boom(argv=None):
        worker = threading.Thread(target=mark_run_allocated)
        worker.start()
        worker.join()
        raise ModuleNotFoundError("No module named 'some_provider'", name="some_provider")

    monkeypatch.setattr(cli_main, "_run", _boom)

    with pytest.raises(ModuleNotFoundError):
        cli_main.main([])
    assert reported == []


@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [
        (ModuleNotFoundError("No module named 'fastmcp'", name="fastmcp"), 78),
        (ImportError("cannot import name 'FastMCP'"), 1),
    ],
)
def test_li_mcp_separates_a_missing_extra_from_a_broken_one(raised, expected_code):
    """`li mcp` needs an optional extra, and the two ways that fails differ.

    An extra that is not installed means the server never started and nothing
    ran. An extra that is installed but cannot be imported is a defect in what is
    present rather than a missing piece of it. Reporting both with the ordinary
    failure code leaves a caller unable to tell "install this" from "this
    installation is broken".

    The dependency blocked here is `fastmcp`, the package the extra actually
    supplies, and the command is driven through `main()`. Blocking `lionagi.mcp`
    instead would prove nothing: that package is deliberately dependency-free and
    imports fine without the extra, so the real failure happens one level deeper
    and would have been missed by a test that never reached it.
    """
    script = textwrap.dedent(
        f"""
        import sys

        BLOCKED = "fastmcp"
        RAISED = {raised!r}

        class _RefuseOne:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == BLOCKED or fullname.startswith(BLOCKED + "."):
                    raise RAISED
                return None

        for name in [m for m in list(sys.modules)
                     if m == BLOCKED or m.startswith(BLOCKED + ".")]:
            del sys.modules[name]
        sys.meta_path.insert(0, _RefuseOne())

        from lionagi.cli.main import main

        sys.exit(main(["mcp"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )

    assert proc.returncode == expected_code, proc.stderr


@pytest.mark.parametrize(
    ("raised", "expected_type", "expected_name"),
    [
        (
            "ModuleNotFoundError(\"No module named 'fastmcp'\", name='fastmcp')",
            "ModuleNotFoundError",
            "fastmcp",
        ),
        ("ImportError(\"cannot import name 'FastMCP'\")", "ImportError", None),
    ],
)
def test_serve_reports_a_missing_extra_as_a_missing_module(raised, expected_type, expected_name):
    """`serve()` keeps the distinction in the exception type it raises.

    `li mcp` does not depend on this: it imports the server module itself, so it
    sees the original error before `serve()` is ever called. The caller that does
    depend on it is `python -m lionagi.mcp`, which calls `serve()` directly and
    has nothing else to key off. Without this test the type is pinned only where
    it happens to be redundant, and flattening it to a plain ImportError would
    leave every test passing while that entry point lost the distinction.

    `name` is asserted too, because it is what a caller reports to a human and it
    is dropped by the obvious wrong implementation.
    """
    script = textwrap.dedent(
        f"""
        import sys

        BLOCKED = "fastmcp"

        class _RefuseOne:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == BLOCKED or fullname.startswith(BLOCKED + "."):
                    raise {raised}
                return None

        for name in [m for m in list(sys.modules)
                     if m == BLOCKED or m.startswith(BLOCKED + ".")]:
            del sys.modules[name]
        sys.meta_path.insert(0, _RefuseOne())

        from lionagi.mcp import serve

        try:
            serve()
        except BaseException as exc:
            print(type(exc).__name__)
            print(getattr(exc, "name", None))
            sys.exit(0)
        sys.exit(1)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    seen_type, seen_name = proc.stdout.split()[:2]
    assert seen_type == expected_type, proc.stdout
    assert seen_name == str(expected_name), proc.stdout


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

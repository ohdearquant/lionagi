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

from lionagi._feature_errors import MissingFeatureError
from lionhub.orchestration.job import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_ENVIRONMENT_ERROR,
    begin_invocation,
    clear_run_allocation,
    end_invocation,
    mark_run_allocated,
    run_was_allocated,
)
from tests._job_test_support import _no_run_allocated  # noqa: F401

# `from lionhub.serve.cli import main` is not a handle on this module. What it yields
# depends on import order: the package's __getattr__ pins the re-exported
# main() function, while importing the submodule binds the module onto the same
# package attribute. Monkeypatching an attribute on whichever of the two is
# bound may silently patch nothing a caller reads, so the module is taken by
# name.
cli_main = importlib.import_module("lionhub.serve.cli.main")

REPO_ROOT = Path(__file__).resolve().parents[5]
# Both import roots of this checkout, so a child process runs this tree's lionagi and
# lionhub rather than whichever checkout the installed distribution points at.
SOURCE_PATH = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "lionhub" / "src")])


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


_HUB_HINT = (
    "the bundled provider hub is missing from this installation; "
    "reinstall the matching lionagi release to restore it"
)

# What the provider registry raises when a selected provider cannot activate because a
# declared dependency, or the bundled provider hub itself, is not installed: the typed
# error is an ImportError but not a ModuleNotFoundError.
MISSING_FEATURES = {
    "optional-dependency": (
        lambda: MissingFeatureError(
            feature="providers",
            missing_distribution="ollama",
            install_hint="pip install lionagi[ollama]",
            provider="ollama",
            endpoint="chat/completions",
            reason="missing_dependency",
            import_root="ollama",
        ),
        ("ollama", "pip install lionagi[ollama]"),
    ),
    "provider-hub": (
        lambda: MissingFeatureError(
            feature="providers",
            missing_distribution="lionagi",
            install_hint=_HUB_HINT,
            import_root="lionhub",
        ),
        ("lionhub", "reinstall the matching lionagi release"),
    ),
    "no-import-root": (
        lambda: MissingFeatureError(
            feature="providers",
            missing_distribution="some-distribution",
            install_hint="pip install some-distribution",
        ),
        ("some-distribution", "pip install some-distribution"),
    ),
}


@pytest.mark.parametrize(
    ("make_error", "expected"), MISSING_FEATURES.values(), ids=MISSING_FEATURES.keys()
)
def test_a_missing_feature_exits_with_the_environment_code(
    monkeypatch, reported, make_error, expected
):
    """A typed missing-feature error before any run exists is the environment too.

    The registry raises it where a bare `ModuleNotFoundError` used to escape, and it is an
    `ImportError` but not a `ModuleNotFoundError`, so a handler for the narrower type alone
    lets it out as a traceback with the ordinary failure code.
    """

    def _boom(argv=None):
        raise make_error()

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR

    summary = "\n".join(reported)
    for text in expected:
        assert text in summary
    assert "No run was started" in summary


_NOT_STARTED = (
    "No run was started, so this is an unusable environment rather than a failed run. "
    "Install the missing dependency, then re-run."
)

# What the report says for each kind of error: the name of what is missing, and the guidance
# that follows it. A module error carries none. A declared dependency's hint is a short install
# command, and the provider hub's is a whole sentence, so the guidance has to read correctly
# after either one.
REPORTED_REQUIREMENTS = {
    "missing-module": (
        lambda: ModuleNotFoundError("No module named 'sniffio'", name="sniffio"),
        "sniffio",
        "",
    ),
    "install-command-hint": (
        MISSING_FEATURES["optional-dependency"][0],
        "ollama",
        " Guidance: pip install lionagi[ollama]",
    ),
    "sentence-hint": (
        MISSING_FEATURES["provider-hub"][0],
        "lionhub",
        f" Guidance: {_HUB_HINT}",
    ),
    "distribution-name-only": (
        MISSING_FEATURES["no-import-root"][0],
        "some-distribution",
        " Guidance: pip install some-distribution",
    ),
}


@pytest.mark.parametrize(
    ("make_error", "missing", "guidance"),
    REPORTED_REQUIREMENTS.values(),
    ids=REPORTED_REQUIREMENTS.keys(),
)
def test_the_startup_report_names_what_is_missing_then_gives_the_guidance(
    monkeypatch, reported, make_error, missing, guidance
):
    """The sentence names what is missing; the hint follows it as its own sentence rather than
    sitting in parentheses inside the first one."""

    def _boom(argv=None):
        raise make_error()

    monkeypatch.setattr(cli_main, "_run", _boom)

    assert cli_main.main([]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert reported == [
        f"cannot start: {missing} is not installed in this environment. {_NOT_STARTED}{guidance}"
    ]


@pytest.mark.parametrize(
    ("make_error", "missing", "guidance"),
    REPORTED_REQUIREMENTS.values(),
    ids=REPORTED_REQUIREMENTS.keys(),
)
def test_the_command_loader_report_names_what_is_missing_then_gives_the_guidance(
    monkeypatch, reported, make_error, missing, guidance
):
    def _boom(selected):
        raise make_error()

    monkeypatch.setattr(cli_main, "build_cli_parser", _boom)

    assert cli_main.main(["doctor"]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert reported == [
        f"command 'doctor' cannot run: {missing} is not installed in this environment. "
        f"{_NOT_STARTED}{guidance}"
    ]


@pytest.mark.parametrize("make_error", [make for make, _ in MISSING_FEATURES.values()])
def test_a_missing_feature_after_a_run_was_allocated_is_not_an_environment_fault(
    monkeypatch, reported, make_error
):
    """The allocation rule holds for the typed error as it does for a missing module."""

    def _boom(argv=None):
        mark_run_allocated()
        raise make_error()

    monkeypatch.setattr(cli_main, "_run", _boom)

    with pytest.raises(MissingFeatureError):
        cli_main.main([])
    assert reported == []


def test_the_loader_boundary_does_not_absorb_a_missing_feature(monkeypatch, reported):
    """A command module whose import raises the typed error is reported as the environment,
    not as a command that "failed to load"."""

    def _boom(selected):
        raise MISSING_FEATURES["optional-dependency"][0]()

    monkeypatch.setattr(cli_main, "build_cli_parser", _boom)

    assert cli_main.main(["doctor"]) == EXIT_CODE_ENVIRONMENT_ERROR
    assert "ollama" in "\n".join(reported)


def test_li_agent_exits_78_when_the_ollama_package_is_absent(tmp_path):
    """`li agent` selects its provider before it allocates a run, so a provider whose declared
    package is not installed is an unusable environment: exit 78, the package and the install
    hint named last, and nothing on disk.

    Driven through `main()` with the real registry. The ollama package is absent from the
    child's import system, and the nested-agent refusal, which looks at the process's
    ancestors, is answered the way the agent-depth tests answer it so the run does not depend
    on what started the test. The home directory is a fresh one, so a run that was allocated
    would be visible in it.
    """
    script = textwrap.dedent(
        """
        import sys

        # A None entry makes ``import ollama`` fail and ``find_spec("ollama")`` answer None,
        # which is what an interpreter without the package does.
        sys.modules["ollama"] = None

        from lionagi.service import _subagent_admission as admission

        admission._INHERITED_AGENT_DEPTH = 0
        admission._ancestor_agent_depth = lambda: (0, None)

        from lionhub.serve.cli.main import main

        sys.exit(main(["agent", "ollama/a-model", "say hello"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": SOURCE_PATH,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        },
        timeout=120,
    )

    assert proc.returncode == EXIT_CODE_ENVIRONMENT_ERROR, proc.stderr
    last_line = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "ollama" in last_line
    assert "pip install lionagi[ollama]" in last_line
    assert "No run was started" in last_line
    assert not (tmp_path / ".lionagi" / "runs").exists()


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

        BLOCKED = "lionhub.serve.cli.doctor"

        class _RefuseOne:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == BLOCKED:
                    raise ModuleNotFoundError(
                        f"No module named {fullname!r}", name=fullname
                    )
                return None

        sys.modules.pop(BLOCKED, None)
        sys.meta_path.insert(0, _RefuseOne())

        from lionhub.serve.cli.main import main

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
        env={**os.environ, "PYTHONPATH": SOURCE_PATH},
        timeout=120,
    )

    assert proc.returncode == EXIT_CODE_ENVIRONMENT_ERROR, proc.stderr
    last_line = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "lionhub.serve.cli.doctor" in last_line
    assert "No run was started" in last_line


def test_a_missing_cli_catalog_reports_the_environment_in_a_fresh_process():
    """A missing catalog must reach the same startup error boundary as a command."""
    script = textwrap.dedent(
        """
        import sys

        BLOCKED = "lionhub.serve.cli.catalog"

        class _RefuseCatalog:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == BLOCKED:
                    raise ModuleNotFoundError(
                        f"No module named {fullname!r}", name=fullname
                    )
                return None

        sys.meta_path.insert(0, _RefuseCatalog())
        from lionhub.serve.cli.main import main

        sys.exit(main(["doctor"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": SOURCE_PATH},
        timeout=120,
    )

    assert proc.returncode == EXIT_CODE_ENVIRONMENT_ERROR, proc.stderr
    last_line = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "lionhub.serve.cli.catalog" in last_line
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
    supplies, and the command is driven through `main()`. Blocking `lionhub.serve.mcp`
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

        from lionhub.serve.cli.main import main

        sys.exit(main(["mcp"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": SOURCE_PATH},
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
    depend on it is `python -m lionhub.serve.mcp`, which calls `serve()` directly and
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

        from lionhub.serve.mcp import serve

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
        env={**os.environ, "PYTHONPATH": SOURCE_PATH},
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

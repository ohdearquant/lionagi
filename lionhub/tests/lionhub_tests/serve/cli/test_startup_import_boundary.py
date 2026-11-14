# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Missing startup imports must remain distinguishable from failed commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[5]

_CHILD_SETUP = """
import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
scratch = Path(sys.argv[2]).resolve()
assert not [name for name in sys.modules if name.partition('.')[0] in ('lionagi', 'lionhub')]
assert Path.home().resolve() == scratch / 'home'
assert Path(os.environ['LIONAGI_HOME']).resolve() == scratch / 'lionagi'
assert Path.cwd().resolve() == scratch / 'work'
sys.path[:0] = [str(root), str(root / 'lionhub' / 'src')]

external_attempts = []

def refuse_external_action(event, args):
    if event in {
        'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen',
        'os.system', 'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.exec',
    }:
        external_attempts.append(event)
        raise AssertionError(f'unexpected startup action: {event}')

sys.addaudithook(refuse_external_action)

def assert_checkout_origins():
    roots = (root / 'lionagi', root / 'lionhub' / 'src' / 'lionhub')
    for name, module in tuple(sys.modules.items()):
        if module is None or name.partition('.')[0] not in ('lionagi', 'lionhub'):
            continue
        source = vars(module).get('__file__')
        if source is not None:
            assert any(Path(source).resolve().is_relative_to(path) for path in roots), (name, source)
        for package_path in vars(module).get('__path__', ()):
            assert any(Path(package_path).resolve().is_relative_to(path) for path in roots), (
                name, package_path
            )

def import_main():
    # Failed initialization leaves no module whose __file__ can be checked.
    spec = importlib.util.find_spec('lionhub.serve.cli.main')
    expected = root / 'lionhub' / 'src' / 'lionhub' / 'serve' / 'cli' / 'main.py'
    assert spec is not None and Path(spec.origin).resolve() == expected
    module = importlib.import_module('lionhub.serve.cli.main')
    assert Path(module.__file__).resolve() == expected
    assert_checkout_origins()
    return module

def assert_no_startup_effects():
    assert not external_attempts, external_attempts
    assert 'lionhub.serve.cli.doctor' not in sys.modules
    assert 'lionhub.serve.cli.machine' not in sys.modules
    util = sys.modules.get('lionhub.orchestration.job')
    if util is not None:
        assert not util.run_was_allocated()
        assert util._invocations_in_flight == 0
    for name in (
        'lionagi.cli.progress', 'lionagi.cli.hint', 'lionagi.cli.warn',
        'lionagi.cli.error', 'claude-cli', 'codex-cli', 'pi-cli',
    ):
        assert not logging.getLogger(name).handlers, name
    assert_checkout_origins()
"""


def _run_fresh(
    tmp_path: Path, body: str, *, close_stderr: bool = False
) -> subprocess.CompletedProcess[str]:
    directories = {
        "HOME": "home",
        "USERPROFILE": "home",
        "LIONAGI_HOME": "lionagi",
        "TMPDIR": "tmp",
        "TMP": "tmp",
        "TEMP": "tmp",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data",
    }
    # Keep interpreter/platform essentials, not account or provider configuration.
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE"}
    }
    for name, relative in directories.items():
        path = tmp_path / relative
        path.mkdir(exist_ok=True)
        environment[name] = str(path)
    work = tmp_path / "work"
    work.mkdir()
    command = [
        sys.executable,
        "-I",
        "-c",
        _CHILD_SETUP + textwrap.dedent(body),
        str(_ROOT),
        str(tmp_path),
    ]
    if close_stderr:
        # The interpreter starts with descriptor 2 closed, so sys.stderr is None.
        command = ["/bin/sh", "-c", 'exec "$@" 2>&-', "sh", *command]
    process = subprocess.run(  # noqa: S603 -- this interpreter and fixed test programs
        command,
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert not list(tmp_path.rglob("state.db*"))
    assert not list(tmp_path.rglob("run.json"))
    assert not (tmp_path / "lionagi" / "runs").exists()
    assert not (tmp_path / "home" / ".lionagi" / "runs").exists()
    return process


def _assert_environment_refusal(
    process: subprocess.CompletedProcess[str], missing: str, *, machine: bool = False
) -> None:
    assert process.returncode == 78, process.stderr
    lines = [line for line in process.stderr.splitlines() if line.strip()]
    assert lines, process.stderr
    assert missing in lines[-1]
    assert "No run was started" in lines[-1]
    assert "Traceback (most recent call last):" in process.stderr
    assert process.stderr.index("Traceback (most recent call last):") < process.stderr.rindex(
        "No run was started"
    )
    if machine:
        stdout_lines = process.stdout.splitlines()
        assert len(stdout_lines) == 1, process.stdout
        assert json.loads(stdout_lines[0]) == {
            "ok": False,
            "contract_version": 1,
            "data": None,
            "error": {
                "kind": "unavailable",
                "message": lines[-1].removeprefix("error: "),
                "detail": {"exit_code": 78},
            },
        }
    else:
        assert process.stdout == ""


@pytest.mark.parametrize(
    ("argv", "machine"),
    [
        pytest.param(["doctor"], False, id="human"),
        pytest.param(["doctor", "--machine"], True, id="machine"),
        pytest.param(["doctor", "--", "--machine"], False, id="after-sentinel"),
    ],
)
def test_missing_hub_keeps_the_pre_run_environment_exit(
    tmp_path: Path, argv: list[str], machine: bool
) -> None:
    """The CLI's entry module is in the hub, so the rest of the hub is what can be missing."""
    process = _run_fresh(
        tmp_path,
        f"""
        refused = []
        entry = {{'lionhub', 'lionhub.serve', 'lionhub.serve.cli', 'lionhub.serve.cli.main'}}
        helper = 'lionhub.serve.cli._machine_envelope'

        class RefuseHub:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition('.')[0] == 'lionhub' and fullname not in entry | {{helper}}:
                    refused.append(fullname)
                    raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
                return None

        sys.meta_path.insert(0, RefuseHub())
        cli_main = import_main()
        before = set(sys.modules)
        assert helper not in before
        code = cli_main.main({argv!r})
        assert refused == ['lionhub.serve.cli.catalog']
        added = sys.modules.keys() - before
        unexpected = {{
            name for name in added
            if name != helper and name.partition('.')[0] not in sys.stdlib_module_names
        }}
        assert not unexpected, unexpected
        assert (helper in sys.modules) == {machine!r}
        assert_no_startup_effects()
        sys.exit(code)
        """,
    )
    _assert_environment_refusal(process, "lionhub", machine=machine)


@pytest.mark.skipif(sys.platform == "win32", reason="closes descriptor 2 through a POSIX shell")
@pytest.mark.parametrize("machine", [False, True], ids=["human", "machine"])
def test_environment_refusal_with_stderr_closed_keeps_stdout_clean(
    tmp_path: Path, machine: bool
) -> None:
    """The traceback must not fall back to stdout when there is no stderr."""
    argv = ["doctor", "--machine"] if machine else ["doctor"]
    process = _run_fresh(
        tmp_path,
        f"""
        assert sys.stderr is None
        entry = {{'lionhub', 'lionhub.serve', 'lionhub.serve.cli', 'lionhub.serve.cli.main'}}
        helper = 'lionhub.serve.cli._machine_envelope'

        class RefuseHub:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.partition('.')[0] == 'lionhub' and fullname not in entry | {{helper}}:
                    raise ModuleNotFoundError(f"No module named {{fullname!r}}", name=fullname)
                return None

        sys.meta_path.insert(0, RefuseHub())
        cli_main = import_main()
        code = cli_main.main({argv!r})
        assert_no_startup_effects()
        sys.exit(code)
        """,
        close_stderr=True,
    )
    assert process.returncode == 78
    assert process.stderr == ""
    if not machine:
        assert process.stdout == ""
        return
    stdout_lines = process.stdout.splitlines()
    assert len(stdout_lines) == 1, process.stdout
    envelope = json.loads(stdout_lines[0])
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "unavailable"
    assert envelope["error"]["detail"] == {"exit_code": 78}
    assert "No run was started" in envelope["error"]["message"]


def test_catalog_missing_dependency_keeps_its_original_name(tmp_path: Path) -> None:
    process = _run_fresh(
        tmp_path,
        """
        refused = []

        class RefuseCatalogDependency:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionagi._auto':
                    assert 'lionhub.serve.cli.catalog' in sys.modules
                    refused.append(fullname)
                    raise ModuleNotFoundError(
                        "No module named 'lionagi._auto'", name='lionagi._auto'
                    )
                return None

        sys.meta_path.insert(0, RefuseCatalogDependency())
        cli_main = import_main()
        code = cli_main.main(['doctor'])
        assert refused == ['lionagi._auto']
        assert_no_startup_effects()
        sys.exit(code)
        """,
    )
    _assert_environment_refusal(process, "lionagi._auto")
    assert "lionhub/serve/cli/catalog.py" in process.stderr.replace("\\", "/")


@pytest.mark.parametrize("error_type", ["ImportError", "RuntimeError"])
def test_catalog_broken_import_is_not_reported_as_a_missing_environment(
    tmp_path: Path, error_type: str
) -> None:
    marker = "catalog import failed deliberately"
    process = _run_fresh(
        tmp_path,
        f"""
        refused = []

        class BrokenCatalog:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.serve.cli.catalog':
                    refused.append(fullname)
                    raise {error_type}({marker!r})
                return None

        sys.meta_path.insert(0, BrokenCatalog())
        try:
            import_main()
        except {error_type}:
            assert refused == ['lionhub.serve.cli.catalog']
            assert_no_startup_effects()
            raise
        else:
            raise AssertionError('broken catalog unexpectedly imported')
        """,
    )
    assert process.returncode == 1, process.stderr
    assert process.stdout == ""
    assert process.stderr.splitlines()[-1] == f"{error_type}: {marker}"
    assert "No run was started" not in process.stderr
    assert "cannot start:" not in process.stderr


@pytest.mark.parametrize("error_kind", ["module", "feature"])
@pytest.mark.parametrize("machine", [False, True], ids=["human", "machine"])
def test_configuration_import_environment_fault_is_reported_before_a_run(
    tmp_path: Path, error_kind: str, machine: bool
) -> None:
    argv = ["doctor", "--machine"] if machine else ["doctor"]
    process = _run_fresh(
        tmp_path,
        f"""
        refused = []

        class RefuseConfiguration:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.orchestration.console':
                    refused.append(fullname)
                    if {error_kind!r} == 'feature':
                        from lionagi._feature_errors import MissingFeatureError
                        raise MissingFeatureError(
                            feature='startup-console',
                            missing_distribution='startup-console-dependency',
                            install_hint='install startup-console-dependency',
                            import_root='startup_console_dependency',
                        )
                    raise ModuleNotFoundError(
                        "No module named 'startup_console_dependency'",
                        name='startup_console_dependency',
                    )
                return None

        sys.meta_path.insert(0, RefuseConfiguration())
        cli_main = import_main()
        from lionagi._log_channels import log_error
        assert cli_main.log_error is log_error
        code = cli_main.main({argv!r})
        assert refused == ['lionhub.orchestration.console']
        assert_no_startup_effects()
        sys.exit(code)
        """,
    )
    _assert_environment_refusal(process, "startup_console_dependency", machine=machine)
    if error_kind == "feature":
        last_line = [line for line in process.stderr.splitlines() if line.strip()][-1]
        assert "install startup-console-dependency" in last_line


def test_configuration_import_error_is_not_a_missing_environment(tmp_path: Path) -> None:
    process = _run_fresh(
        tmp_path,
        """
        refused = []

        class RefuseConfiguration:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.orchestration.console':
                    refused.append(fullname)
                    raise ImportError('startup-console-import-marker')
                return None

        sys.meta_path.insert(0, RefuseConfiguration())
        try:
            cli_main = import_main()
            cli_main.main(['doctor', '--machine'])
        except ImportError:
            assert refused == ['lionhub.orchestration.console']
            assert_no_startup_effects()
            raise
        else:
            raise AssertionError('broken logging configuration unexpectedly imported')
        """,
    )
    assert process.returncode == 1, process.stderr
    assert process.stdout == ""
    assert process.stderr.splitlines()[-1] == "ImportError: startup-console-import-marker"
    assert "No run was started" not in process.stderr
    assert "cannot start:" not in process.stderr


def test_saved_catalog_fault_precedes_configuration_import(tmp_path: Path) -> None:
    process = _run_fresh(
        tmp_path,
        """
        refused = []

        class RefuseStartup:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.serve.cli.catalog':
                    refused.append('catalog')
                    raise ModuleNotFoundError(
                        "No module named 'startup_catalog_dependency'",
                        name='startup_catalog_dependency',
                    )
                if fullname == 'lionhub.orchestration.console':
                    refused.append('configuration')
                    raise AssertionError('configuration imported after a saved catalog fault')
                return None

        sys.meta_path.insert(0, RefuseStartup())
        cli_main = import_main()
        code = cli_main.main(['doctor'])
        assert refused == ['catalog']
        assert 'lionhub.orchestration.console' not in sys.modules
        assert_no_startup_effects()
        sys.exit(code)
        """,
    )
    _assert_environment_refusal(process, "startup_catalog_dependency")


def test_configuration_precedes_real_machine_output_and_keeps_the_core_emitter(
    tmp_path: Path,
) -> None:
    process = _run_fresh(
        tmp_path,
        """
        cli_main = import_main()
        from lionagi._log_channels import log_error
        assert cli_main.log_error is log_error
        assert 'lionhub.orchestration.console' not in sys.modules
        console_file = str(root / 'lionhub/src/lionhub/orchestration/console.py')
        machine_file = str(root / 'lionhub/src/lionhub/serve/cli/machine.py')
        events = []

        def observe(frame, event, arg):
            filename = frame.f_code.co_filename
            name = frame.f_code.co_name
            if filename == console_file and name == 'configure_cli_logging' and event == 'return':
                assert logging.getLogger('lionagi.cli.error').handlers
                events.append('configured')
            elif filename == machine_file and name == 'dispatch_machine' and event == 'call':
                assert events == ['configured']
                assert logging.getLogger('lionagi.cli.error').handlers
                events.append('dispatch')
            elif filename == machine_file and name == 'emit' and event == 'call':
                assert events == ['configured', 'dispatch']
                assert logging.getLogger('lionagi.cli.error').handlers
                events.append('emit')

        sys.setprofile(observe)
        try:
            code = cli_main.main(['--machine', '__startup_unknown_command__'])
        finally:
            sys.setprofile(None)
        assert code == 0
        assert 'lionhub.orchestration.console' in sys.modules
        assert 'lionagi.cli._logging' not in sys.modules
        assert events == ['configured', 'dispatch', 'emit']
        assert not external_attempts
        from lionhub.orchestration import job as _util
        assert not _util.run_was_allocated()
        assert _util._invocations_in_flight == 0
        assert_checkout_origins()
        sys.exit(code)
        """,
    )
    assert process.returncode == 0, process.stderr
    lines = process.stdout.splitlines()
    assert len(lines) == 1, process.stdout
    envelope = json.loads(lines[0])
    assert envelope["ok"] is False
    assert envelope["error"]["kind"] == "invalid_input"


@pytest.mark.parametrize("argv", [["doctor"], ["doctor", "--machine"]], ids=["human", "machine"])
def test_missing_job_dependency_keeps_the_pre_run_environment_exit(tmp_path, argv):
    process = _run_fresh(
        tmp_path,
        f"""
        refused = []

        class RefuseJobDependency:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.orchestration.job':
                    refused.append(fullname)
                    raise ModuleNotFoundError(
                        "No module named 'startup_job_dependency'", name='startup_job_dependency'
                    )
                return None

        sys.meta_path.insert(0, RefuseJobDependency())
        cli_main = import_main()
        code = cli_main.main({argv!r})
        assert refused == ['lionhub.orchestration.job']
        assert 'lionhub.orchestration.job' not in sys.modules
        assert_no_startup_effects()
        sys.exit(code)
        """,
    )
    _assert_environment_refusal(process, "startup_job_dependency", machine="--machine" in argv)


@pytest.mark.parametrize("error_type", ["ImportError", "RuntimeError"])
def test_broken_job_import_is_not_reported_as_a_missing_environment(tmp_path, error_type):
    marker = "job import failed deliberately"
    process = _run_fresh(
        tmp_path,
        f"""
        refused = []

        class BrokenJob:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.orchestration.job':
                    refused.append(fullname)
                    raise {error_type}({marker!r})
                return None

        sys.meta_path.insert(0, BrokenJob())
        try:
            import_main()
        except {error_type}:
            assert refused == ['lionhub.orchestration.job']
            assert_no_startup_effects()
            raise
        else:
            raise AssertionError('broken job unexpectedly imported')
        """,
    )
    assert process.returncode == 1, process.stderr
    assert process.stdout == ""
    assert process.stderr.splitlines()[-1] == f"{error_type}: {marker}"
    assert "No run was started" not in process.stderr
    assert "cannot start:" not in process.stderr


def test_saved_catalog_fault_precedes_job_import(tmp_path):
    process = _run_fresh(
        tmp_path,
        """
        refused = []

        class RefuseStartup:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'lionhub.serve.cli.catalog':
                    refused.append('catalog')
                    raise ModuleNotFoundError(
                        "No module named 'startup_catalog_dependency'",
                        name='startup_catalog_dependency',
                    )
                if fullname == 'lionhub.orchestration.job':
                    refused.append('job')
                    raise AssertionError('job imported after a saved catalog fault')
                return None

        sys.meta_path.insert(0, RefuseStartup())
        cli_main = import_main()
        code = cli_main.main(['doctor'])
        assert refused == ['catalog']
        assert 'lionhub.orchestration.job' not in sys.modules
        assert_no_startup_effects()
        sys.exit(code)
        """,
    )
    _assert_environment_refusal(process, "startup_catalog_dependency")


def test_startup_environment_exit_matches_the_canonical_job_constant(tmp_path):
    process = _run_fresh(
        tmp_path,
        """
        cli_main = import_main()
        from lionhub.orchestration import job
        assert cli_main._STARTUP_ENVIRONMENT_EXIT == job.EXIT_CODE_ENVIRONMENT_ERROR
        assert_no_startup_effects()
        """,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout == ""

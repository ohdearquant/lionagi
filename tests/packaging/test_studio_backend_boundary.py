# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Local schedule validation and export with the Studio application's imports refused.

ADR-0138 names this test as the proof that they need no backend. It has two arms.

- Source arm, the default: the checkout's own packages, with the 1.x compatibility
  modules still present.
- Installed arm: when ``LIONAGI_STUDIO_WHEEL`` names a built wheel and
  ``LIONAGI_STUDIO_WHEEL_PYTHON`` the interpreter of a new environment that wheel was
  installed into with the ``studio`` extra, the same assertions run with that
  interpreter, from a directory outside the checkout, with no checkout path on
  ``sys.path``. The arm also requires ``lionagi`` and ``lionhub`` to load from that
  environment and the installed package files to be the ones in that wheel. CI's
  ``studio-wheel`` job runs it.

The 1.x wheel bundles the application, so both arms refuse its imports with a
meta-path blocker rather than rely on its absence. The 2.0 arm, with the application
physically absent, remains a release check.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WHEEL_ENV = "LIONAGI_STUDIO_WHEEL"
WHEEL_PYTHON_ENV = "LIONAGI_STUDIO_WHEEL_PYTHON"
# The top-level packages the wheel ships (pyproject.toml, tool.hatch.build.targets.wheel).
WHEEL_PACKAGES = ("lionagi", "lionhub", "lionagi_studio")


def _installed_arm() -> tuple[Path, Path] | None:
    """The wheel and interpreter of the installed arm, or None for the source arm."""
    wheel = os.environ.get(WHEEL_ENV)
    python = os.environ.get(WHEEL_PYTHON_ENV)
    if not wheel and not python:
        return None
    assert wheel and python, f"the installed arm needs both {WHEEL_ENV} and {WHEEL_PYTHON_ENV}"
    return Path(wheel), Path(python)


def _wheel_record(wheel: Path) -> dict[str, str]:
    """Each package file the wheel's RECORD lists, with its digest."""
    with zipfile.ZipFile(wheel) as archive:
        (record,) = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
        lines = archive.read(record).decode().splitlines()
    digests = {}
    for row in csv.reader(lines):
        path, digest = row[0], row[1]
        if digest and path.split("/", 1)[0] in WHEEL_PACKAGES:
            digests[path] = digest.split("=", 1)[1]
    return digests


def test_fresh_wheel_schedule_validate_and_export_without_backend() -> None:
    installed = _installed_arm()
    if installed is None:
        mode = "source"
        interpreter = sys.executable
        scratch = ROOT / ".leg-scratch"
        scratch.mkdir(exist_ok=True)
        tmp_path = Path(tempfile.mkdtemp(prefix="studio-backend-boundary-", dir=scratch))
    else:
        mode = "installed"
        wheel, wheel_python = installed
        assert wheel.is_file(), wheel
        interpreter = str(wheel_python)
        tmp_path = Path(tempfile.mkdtemp(prefix="studio-backend-boundary-"))
        # Run outside the checkout, so nothing under it is importable from the cwd.
        assert ROOT.resolve() not in tmp_path.resolve().parents, tmp_path
    try:
        home = tmp_path / "home"
        state = tmp_path / "state"
        home.mkdir()
        state.mkdir()
        agents = home / ".lionagi" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "---\nmodel: anthropic/claude-sonnet-5\n---\nReview the latest changes.\n"
        )
        schedule_set = tmp_path / "schedules.yaml"
        schedule_set.write_text(
            textwrap.dedent(
                """\
                apiVersion: lionagi.io/v1alpha1
                kind: ScheduleSet
                metadata:
                  name: backend-boundary
                  project: boundary
                schedules:
                  review:
                    trigger:
                      every: 1h
                    target:
                      kind: agent
                      profile: reviewer
                      prompt: review the latest changes
                    execution:
                      cwd: .
                """
            )
        )
        script = r'''
    import contextlib, importlib.abc, io, json, os, sys, tempfile
    from pathlib import Path
    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[3]
    if mode == "source":
        sys.path[:0] = [str(root), str(root / "lionhub" / "src")]
    else:
        # The installed arm reads nothing from the checkout: no entry on the path is in it.
        for entry in sys.path:
            where = Path(entry or ".").resolve()
            assert where != root and root not in where.parents, (entry, sys.path)
    blocked = (
        "apps.studio",
        "lionagi_studio",
        "lionagi.studio.app",
        "lionagi.studio.routes",
        "lionhub.orchestration.scheduler.routes",
    )
    refused = []
    class RefuseApplication(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                refused.append(fullname)
                raise ImportError("blocked application import: " + fullname)
    sys.meta_path.insert(0, RefuseApplication())
    from lionhub.serve.cli.main import main
    manifest = sys.argv[2]
    def run(argv):
        if "--machine" in argv:
            saved_fd = os.dup(1)
            with tempfile.TemporaryFile() as output:
                os.dup2(output.fileno(), 1)
                try:
                    code = main(argv)
                    sys.stdout.flush()
                finally:
                    os.dup2(saved_fd, 1)
                    os.close(saved_fd)
                output.seek(0)
                return output.read().decode(), ""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        assert code == 0, (argv, code, out.getvalue(), err.getvalue())
        return out.getvalue(), err.getvalue()
    human_validate, _ = run(["schedule", "validate", manifest])
    assert "VALID" in human_validate, human_validate
    machine_validate, _ = run(["--machine", "schedule", "validate", manifest])
    assert json.loads(machine_validate)["data"]["valid"] is True, machine_validate
    expected_export_yaml = """apiVersion: lionagi.io/v1alpha1
    kind: ScheduleSet
    metadata:
      name: export
      project: export
      scope: null
    schedules: {}
    """
    # The report goes to a file: stderr also carries the store's one-time warning on a
    # runtime linked to an SQLite without the WAL-reset fix.
    report_file = Path.cwd() / "export-report.txt"
    human_export, _ = run(["schedule", "export", "--report", str(report_file)])
    assert human_export == expected_export_yaml, repr(human_export)
    human_export_report = report_file.read_text()
    assert human_export_report == "0 ready, 0 blocked\n", repr(human_export_report)
    machine_export, _ = run(["--machine", "schedule", "export"])
    assert json.loads(machine_export) == {
        "ok": True,
        "contract_version": 1,
        "data": {
            "documents": [{"project": "export", "yaml": expected_export_yaml}],
            "report": [],
            "blocked_count": 0,
            "ready_count": 0,
        },
        "error": None,
    }, machine_export
    assert refused == [], refused
    try:
        import apps.studio
    except ImportError:
        pass
    else:
        raise AssertionError("the application import control was not refused")
    try:
        import lionagi_studio
    except ImportError:
        pass
    else:
        raise AssertionError("the application package control was not refused")
    assert refused == ["apps.studio", "lionagi_studio"]
    if mode == "installed":
        from importlib import metadata
        prefix = Path(sys.prefix).resolve()
        for name in ("lionagi", "lionhub"):
            origin = Path(sys.modules[name].__file__).resolve()
            assert prefix in origin.parents, (name, origin, prefix)
            assert root not in origin.parents, (name, origin)
        packages = ("lionagi", "lionhub", "lionagi_studio")
        digests = {
            str(path): path.hash.value
            for path in metadata.distribution("lionagi").files or ()
            if path.hash is not None and str(path).split("/", 1)[0] in packages
        }
        print("RECORD " + json.dumps(digests, sort_keys=True))
    print("PASS: human and machine validate/export succeed; application import control refused")
    '''
        script = textwrap.dedent(script)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["LIONAGI_HOME"] = str(state)
        env.pop("LIONAGI_STATE_DB_URL", None)
        if installed is not None:
            env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [interpreter, "-c", script, str(ROOT), str(schedule_set), mode],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "PASS: human and machine validate/export succeed" in result.stdout, result.stdout
        if installed is not None:
            # The installed package files are the ones in the wheel this run was handed,
            # not an earlier build or a release of the same version.
            (line,) = [row for row in result.stdout.splitlines() if row.startswith("RECORD ")]
            installed_digests = json.loads(line[len("RECORD ") :])
            assert installed_digests, "the installed distribution lists no package files"
            assert installed_digests == _wheel_record(installed[0])
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Exercise the 1.x checkout with Studio application imports denied.

The built wheel outside the checkout and the 2.0 physical-absence arm remain
release checks; this test covers the source tree with the 1.x compatibility
modules still present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_fresh_wheel_schedule_validate_and_export_without_backend() -> None:
    scratch = ROOT / ".leg-scratch"
    scratch.mkdir(exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="studio-backend-boundary-", dir=scratch))
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
    root = Path(sys.argv[1])
    sys.path[:0] = [str(root), str(root / "lionhub" / "src")]
    blocked = (
        "apps.studio",
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
    assert refused == ["apps.studio"]
    print("PASS: human and machine validate/export succeed; application import control refused")
    '''
        script = textwrap.dedent(script)
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["LIONAGI_HOME"] = str(state)
        env.pop("LIONAGI_STATE_DB_URL", None)
        result = subprocess.run(
            [sys.executable, "-c", script, str(ROOT), str(schedule_set)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "PASS: human and machine validate/export succeed" in result.stdout, result.stdout
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The CI wrapper selects configured roots without changing the caller's directory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("lane", ["test-python", "test-python-cov"])
def test_default_ci_targets_from_nonroot_keep_junit_and_rss_paths(tmp_path, lane):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "trace.jsonl"
    uv = fake_bin / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, runpy, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['run', 'python'] and args[2].endswith('/test_roots.py'):\n"
        "    runpy.run_path(args[2], run_name='__main__')\n"
        "else:\n"
        "    with open(os.environ['TRACE_FILE'], 'a') as log:\n"
        "        log.write(json.dumps({'args': args, 'cwd': os.getcwd(), "
        "'project': os.environ.get('UV_PROJECT'), 'rss': os.environ.get('PYTEST_RSS_LOG')})+'\\n')\n"
        "    for arg in args:\n"
        "        if arg.startswith('--junitxml='):\n"
        "            Path(arg.split('=', 1)[1]).write_text('<testsuites/>')\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        TRACE_FILE=str(trace),
        PYTEST_JUNIT_XML="report.xml",
        PYTEST_RSS_LOG="relative-rss",
    )
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/ci.sh"), lane],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert all(call["cwd"] == str(tmp_path) for call in calls)
    assert all(call["project"] == str(ROOT) for call in calls)
    assert all(call["rss"] == "relative-rss" for call in calls)
    from scripts.test_roots import configured_test_roots

    expected = [str(ROOT / root) for root in configured_test_roots(ROOT)]
    assert calls[0]["args"][-len(expected) :] == expected
    assert calls[1]["args"][-len(expected) :] == expected
    assert (tmp_path / "report.xml").is_file()


def test_explicit_ci_target_keeps_caller_relative_path_and_environment(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    trace = tmp_path / "trace.jsonl"
    uv = fake_bin / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['TRACE_FILE'], 'a') as log:\n"
        "    log.write(json.dumps({'args': args, 'cwd': os.getcwd(), "
        "'project': os.environ.get('UV_PROJECT')})+'\\n')\n"
        "for arg in args:\n"
        "    if arg.startswith('--junitxml='):\n"
        "        Path(arg.split('=', 1)[1]).write_text('<testsuites/>')\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        TRACE_FILE=str(trace),
        PYTEST_JUNIT_XML="explicit.xml",
        UV_PROJECT="caller-project",
    )
    target = "relative/test_a.py::test_one"
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/ci.sh"), "test-python", target],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    calls = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2
    assert all(call["cwd"] == str(tmp_path) for call in calls)
    assert all(call["project"] == "caller-project" for call in calls)
    assert all(call["args"][-1] == target for call in calls)
    assert (tmp_path / "explicit.xml").is_file()

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A DataLogger writes nothing unless it is given a directory.

Used as a library, lionagi keeps its logs in memory. A service that wants log
files passes its directory, through the logger config or the LOG_* settings.
"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import weakref
from pathlib import Path

import pytest

from lionagi.protocols.generic.log import DataLogger, DataLoggerConfig, Log


def _events(logger: DataLogger) -> list:
    return [entry.content["event"] for entry in logger.logs]


def _records(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in directory.rglob("*.json")
        for line in path.read_text().splitlines()
        if line
    ]


def test_default_config_names_no_directory_and_no_exit_save():
    config = DataLoggerConfig()
    assert config.persist_dir is None
    assert config.auto_save_on_exit is False


def test_without_a_directory_capacity_keeps_the_newest_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = DataLogger(capacity=3)
    for event in range(5):
        logger.log(Log(content={"event": event}))
    assert _events(logger) == [2, 3, 4]
    assert list(tmp_path.iterdir()) == []


def test_dump_without_a_directory_raises_and_keeps_the_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = DataLogger()
    logger.log(Log(content={"event": "kept"}))
    with pytest.raises(ValueError, match="persist_path"):
        logger.dump()
    assert _events(logger) == ["kept"]
    assert list(tmp_path.iterdir()) == []

    target = tmp_path / "explicit.json"
    logger.dump(persist_path=target)
    assert [record["content"]["event"] for record in _records(tmp_path)] == ["kept"]


@pytest.mark.asyncio
async def test_adump_without_a_directory_raises_and_keeps_the_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = DataLogger()
    logger.log(Log(content={"event": "kept"}))
    with pytest.raises(ValueError, match="persist_path"):
        await logger.adump()
    assert _events(logger) == ["kept"]
    assert list(tmp_path.iterdir()) == []


def test_exit_save_requested_without_a_directory_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logger = DataLogger(auto_save_on_exit=True)
    logger.log(Log(content={"event": "pending"}))
    logger.save_at_exit()
    assert _events(logger) == ["pending"]
    ref = weakref.ref(logger)
    del logger
    gc.collect()
    assert ref() is None
    assert list(tmp_path.iterdir()) == []


def test_a_default_branch_logger_has_no_directory():
    from lionagi.session.branch import Branch

    config = Branch()._log_manager._config
    assert config.persist_dir is None
    assert config.auto_save_on_exit is False


_LIBRARY_RUN = """
import asyncio

from lionagi.protocols.generic.log import Log
from lionagi.service.hooks.hooked_event import global_hook_logger
from lionagi.session.branch import Branch


async def main():
    async with Branch() as branch:
        for event in range(120):
            branch._log_manager.log(Log(content={"event": event}))
    kept = Branch()
    for event in range(120):
        kept._log_manager.log(Log(content={"event": event}))
    for event in range(250):
        global_hook_logger.log(Log(content={"hook": event}))
    print(len(kept._log_manager.logs), len(global_hook_logger.logs))
    return kept


kept = asyncio.run(main())
"""


def _run_library_child(tmp_path: Path, extra_env: dict[str, str]) -> tuple[Path, str]:
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    checkout = Path(__file__).resolve().parents[3]
    environment = {k: v for k, v in os.environ.items() if not k.startswith("LOG_")}
    environment["PYTHONPATH"] = os.pathsep.join([str(checkout), environment.get("PYTHONPATH", "")])
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HOME"] = str(home)
    environment.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", _LIBRARY_RUN],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return work, result.stdout.strip()


def test_library_use_leaves_no_files_in_the_working_directory(tmp_path):
    # Past capacity, at the end of `async with Branch()`, and at interpreter
    # exit, with the default settings nothing is written.
    work, counts = _run_library_child(tmp_path, {})
    assert sorted(path.relative_to(work) for path in work.rglob("*")) == []
    assert counts == "50 100"


def test_a_configured_log_directory_still_receives_the_logs(tmp_path):
    # Control: the same run, given a directory through the settings, writes
    # every entry of both branches: two dumps at capacity and one at the end
    # of each. The hook logger was given no directory, so it writes nothing.
    logs = tmp_path / "logs"
    work, counts = _run_library_child(
        tmp_path,
        {"LOG_PERSIST_DIR": str(logs), "LOG_AUTO_SAVE_ON_EXIT": "true"},
    )
    assert counts == "20 100"
    events = sorted(record["content"]["event"] for record in _records(logs))
    assert events == sorted(list(range(120)) * 2)
    assert not (logs / "hooks").exists()
    assert sorted(path.relative_to(work) for path in work.rglob("*")) == []

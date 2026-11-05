# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DataLogger lifetime and automatic persistence behavior."""

from __future__ import annotations

import atexit
import builtins
import gc
import json
import logging
import os
import subprocess
import sys
import weakref
from pathlib import Path

import pytest

from lionagi.protocols.generic.log import DataLogger, DataLoggerConfig, Log
from lionagi.protocols.generic.pile import Pile


def _records(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in directory.glob("*.json")
        for line in path.read_text().splitlines()
        if line
    ]


def _log(logger: DataLogger, event: str) -> None:
    logger.log(Log(content={"event": event}))


def _release_legacy_atexit(ref) -> None:
    # Mutation/baseline cleanup: the previous implementation registered this
    # bound method. Unregistering after the assertion avoids a test-created
    # handler trying to write after pytest removes its temporary directory.
    logger = ref()
    if logger is not None:
        atexit.unregister(logger.save_at_exit)


def test_abandoned_logger_and_pile_collect_and_pending_record_is_saved(tmp_path):
    logger = DataLogger(persist_dir=tmp_path)
    _log(logger, "pending")
    logger_ref, pile_ref = weakref.ref(logger), weakref.ref(logger.logs)
    del logger
    try:
        gc.collect()
        assert logger_ref() is None
        assert pile_ref() is None
        assert [item["content"]["event"] for item in _records(tmp_path)] == ["pending"]
    finally:
        _release_legacy_atexit(logger_ref)


def test_bound_exit_method_does_not_keep_logger_alive(tmp_path):
    # This independent lifetime assertion must turn red if bound-method atexit
    # registration is restored, even when there are no pending records.
    logger = DataLogger(persist_dir=tmp_path)
    ref = weakref.ref(logger)
    del logger
    try:
        gc.collect()
        assert ref() is None
    finally:
        _release_legacy_atexit(ref)


def _run_child(tmp_path: Path, auto_save: bool, release: bool) -> None:
    checkout = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join([str(checkout), environment.get("PYTHONPATH", "")])
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HOME"] = str(tmp_path)
    program = """
import gc
import sys
from pathlib import Path
from lionagi.protocols.generic.log import DataLogger, Log

logger = DataLogger(persist_dir=Path(sys.argv[1]), auto_save_on_exit=sys.argv[2] == '1')
logger.log(Log(content={'event': 'child'}))
if sys.argv[3] == '1':
    del logger
    gc.collect()
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(int(auto_save)), str(int(release))],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_live_logger_is_saved_at_normal_interpreter_exit(tmp_path):
    _run_child(tmp_path, auto_save=True, release=False)
    assert [item["content"]["event"] for item in _records(tmp_path)] == ["child"]


@pytest.mark.parametrize("release", [False, True], ids=["live-at-exit", "collected"])
def test_auto_save_disabled_does_not_write_on_collection_or_exit(tmp_path, release):
    _run_child(tmp_path, auto_save=False, release=release)
    assert _records(tmp_path) == []


def test_automatic_failure_warns_once_with_count_and_path_and_keeps_live_records(tmp_path, caplog):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    logger = DataLogger(persist_dir=blocked)
    _log(logger, "one")
    _log(logger, "two")
    caplog.set_level(logging.WARNING, logger="lionagi.protocols.generic.log")
    try:
        logger.save_at_exit()
        warnings = [
            record
            for record in caplog.records
            if record.name == "lionagi.protocols.generic.log" and record.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "2" in message
        assert str(blocked) in message
        assert len(logger.logs) == 2
        assert [entry.content["event"] for entry in logger.logs] == ["one", "two"]
    finally:
        logger.logs.clear()
        atexit.unregister(logger.save_at_exit)


@pytest.mark.parametrize("failure", ["short-write", "close"])
def test_automatic_partial_write_or_close_failure_retains_snapshot(
    tmp_path, monkeypatch, caplog, failure
):
    logger = DataLogger(persist_dir=tmp_path)
    _log(logger, "pending")
    real_open = builtins.open

    class FailingFile:
        def __enter__(self):
            return self

        def write(self, text):
            return len(text) - 1 if failure == "short-write" else len(text)

        def __exit__(self, *_args):
            if failure == "close":
                raise OSError("close failure")

    def guarded_open(path, *args, **kwargs):
        if Path(path).parent == tmp_path and Path(path).suffix == ".json":
            return FailingFile()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    caplog.set_level(logging.WARNING, logger="lionagi.protocols.generic.log")
    try:
        logger.save_at_exit()
        assert [entry.content["event"] for entry in logger.logs] == ["pending"]
        warnings = [r for r in caplog.records if r.name == "lionagi.protocols.generic.log"]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
        logger.logs.clear()


def test_entry_added_during_automatic_write_is_not_cleared(tmp_path, monkeypatch):
    logger = DataLogger(persist_dir=tmp_path)
    _log(logger, "before")
    real_open = builtins.open
    writes = []

    class AddingFile:
        def __enter__(self):
            return self

        def write(self, text):
            writes.append(text)
            _log(logger, "during")
            return len(text)

        def __exit__(self, *_args):
            return False

    def guarded_open(path, *args, **kwargs):
        if Path(path).parent == tmp_path and Path(path).suffix == ".json":
            return AddingFile()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        logger.save_at_exit()
        assert [json.loads(line)["content"]["event"] for line in writes[0].splitlines()] == [
            "before"
        ]
        assert [entry.content["event"] for entry in logger.logs] == ["during"]
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
        logger.logs.clear()


def test_reassigned_logs_are_the_pending_records_saved_on_collection(tmp_path):
    logger = DataLogger(persist_dir=tmp_path)
    _log(logger, "old")
    logger.logs = Pile(collections=[Log(content={"event": "replacement"})], item_type=Log)
    logger_ref, pile_ref = weakref.ref(logger), weakref.ref(logger.logs)
    del logger
    try:
        gc.collect()
        assert logger_ref() is None
        assert pile_ref() is None
        assert [item["content"]["event"] for item in _records(tmp_path)] == ["replacement"]
    finally:
        _release_legacy_atexit(logger_ref)


def test_reassigned_config_controls_the_automatic_save_destination(tmp_path):
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir()
    new_path.mkdir()
    logger = DataLogger(persist_dir=old_path)
    _log(logger, "new-destination")
    logger._config = DataLoggerConfig(persist_dir=new_path)
    ref = weakref.ref(logger)
    del logger
    try:
        gc.collect()
        assert ref() is None
        assert _records(old_path) == []
        assert [item["content"]["event"] for item in _records(new_path)] == ["new-destination"]
    finally:
        _release_legacy_atexit(ref)


def test_automatic_serialization_failure_retains_record_without_leaking_payload(tmp_path, caplog):
    secret = "private-payload-must-not-appear-in-log"

    class Unserializable:
        def __repr__(self):
            return secret

    value = Unserializable()
    logger = DataLogger(persist_dir=tmp_path)
    logger.log(Log(content={"private": value}))
    caplog.set_level(logging.WARNING, logger="lionagi.protocols.generic.log")
    try:
        logger.save_at_exit()
        warnings = [
            record
            for record in caplog.records
            if record.name == "lionagi.protocols.generic.log" and record.levelno >= logging.WARNING
        ]
        assert len(warnings) == 1
        assert warnings[0].levelno == logging.WARNING
        assert "1" in warnings[0].getMessage()
        assert str(tmp_path) in warnings[0].getMessage()
        assert secret not in caplog.text
        assert len(logger.logs) == 1
        assert next(iter(logger.logs)).content["private"] is value
        assert _records(tmp_path) == []
    finally:
        logger.logs.clear()


@pytest.mark.parametrize("clear", [True, False])
def test_explicit_dump_preserves_clear_behavior_for_later_automatic_save(tmp_path, clear):
    logger = DataLogger(persist_dir=tmp_path)
    _log(logger, "record")
    logger.dump(clear=clear)
    assert len(logger.logs) == (0 if clear else 1)
    ref = weakref.ref(logger)
    del logger
    try:
        gc.collect()
        assert ref() is None
        events = [item["content"]["event"] for item in _records(tmp_path)]
        assert events == ["record"] * (1 if clear else 2)
    finally:
        _release_legacy_atexit(ref)


def test_explicit_dump_still_works_with_automatic_save_disabled(tmp_path):
    logger = DataLogger(persist_dir=tmp_path, auto_save_on_exit=False)
    _log(logger, "explicit")
    logger.dump()
    assert not logger.logs
    assert [item["content"]["event"] for item in _records(tmp_path)] == ["explicit"]

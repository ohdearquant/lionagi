# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint errors preserve their independent exception contracts."""

from __future__ import annotations

import json

import pytest

from lionagi._checkpoint_v3_reader import CheckpointRecoveryError
from lionagi._errors import LionError
from lionagi.cli.orchestrate._checkpoint import (
    FlowCheckpointDecodeError,
    FlowCheckpointRecoveryError,
    FlowResumeError,
    FlowV3WriterUnavailableError,
    load_checkpoint,
)


def test_writer_unavailable_preserves_lion_and_recovery_contracts_with_clean_report():
    report = {"source_version": 3, "disposition": "clean", "problem": None}
    error = FlowV3WriterUnavailableError(report)
    assert isinstance(error, FlowResumeError)
    assert isinstance(error, LionError)
    assert isinstance(error, CheckpointRecoveryError)
    assert error.reason == "v3_writer_unavailable"
    assert error.checkpoint_recovery is report
    assert error.valid_prefix is None
    assert error.message == str(error)
    assert error.args == (str(error),)
    assert error.details == {}
    assert error.status_code == type(error).default_status_code
    assert error.to_dict() == {
        "error": "FlowV3WriterUnavailableError",
        "message": str(error),
        "status_code": error.default_status_code,
    }


def test_malformed_checkpoint_preserves_json_and_lion_contracts_without_document(tmp_path):
    secret = "private-prompt-sentinel"
    source = '{"prompt":"' + secret + '",\n"version":}'
    path = tmp_path / "checkpoint.json"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(json.JSONDecodeError) as original:
        json.loads(source)
    with pytest.raises(FlowCheckpointDecodeError) as caught:
        load_checkpoint(path)
    error = caught.value
    assert isinstance(error, FlowCheckpointRecoveryError)
    assert isinstance(error, FlowResumeError)
    assert isinstance(error, LionError)
    assert isinstance(error, json.JSONDecodeError)
    assert (error.pos, error.lineno, error.colno) == (
        original.value.pos,
        original.value.lineno,
        original.value.colno,
    )
    assert error.doc == ""
    assert error.checkpoint_recovery["disposition"] == "refused"
    assert error.checkpoint_recovery["problem"]["code"] == "invalid_record"
    assert error.message == str(error)
    assert error.args == (str(error),)
    assert error.details == {}
    assert error.status_code == type(error).default_status_code
    assert error.to_dict()["message"] == str(error)
    assert secret not in str(error) + repr(error) + repr(error.args)
    assert secret not in json.dumps(error.checkpoint_recovery)
    assert secret not in repr(error.to_dict(include_cause=True))

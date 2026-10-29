# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Operator resume preserves only safe checkpoint recovery diagnostics."""

from __future__ import annotations

import json

import pytest

from lionagi.studio.operator.resume_run import _redacted_resume_result

SECRET = "private-prompt-and-response-sentinel"


def _report(disposition):
    problem = None
    if disposition == "refused":
        problem = {
            "code": "invalid_sequence",
            "line": 2,
            "byte_offset": 42,
            "expected_seq": 2,
            "actual_seq": 4,
            "raw_context": SECRET,
        }
    return {
        "report_version": 1,
        "source_version": 3,
        "generation": "0" * 32,
        "base_sequence": 0,
        "last_applied_sequence": 1,
        "applied_records": 1,
        "valid_prefix_bytes": 42,
        "ignored": {"torn_final_record": 0, "stale_generation_record": 0, "raw": SECRET},
        "problem": problem,
        "disposition": disposition,
        "raw_prompt": SECRET,
    }


@pytest.mark.parametrize("failed", [False, True], ids=["successful-launch", "refusal"])
def test_operator_result_keeps_safe_report_without_raw_values(failed):
    report = _report("refused" if failed else "recovered_prefix")
    result = {"checkpoint_recovery": report}
    if failed:
        result.update(error="invalid_checkpoint", message="invalid_sequence")
    else:
        result.update(invocation_id="invocation", checkpoint_run_id="checkpoint")
    response = _redacted_resume_result({"status": "succeeded", "result": result}, "run")
    assert response["resumed"] is not failed
    assert response["checkpoint_recovery"]["disposition"] == report["disposition"]
    assert response["checkpoint_recovery"]["generation"] == "0" * 32
    assert SECRET not in json.dumps(response)
    if failed:
        assert response["checkpoint_recovery"]["problem"]["code"] == "invalid_sequence"
    else:
        assert response["checkpointRunId"] == "checkpoint"


@pytest.mark.parametrize("field", ["generation", "disposition", "problem"])
def test_operator_omits_malformed_report_instead_of_exposing_payload(field):
    report = _report("refused")
    report[field] = SECRET if field != "problem" else {"code": SECRET}
    result = {"error": "invalid_checkpoint", "message": "invalid", "checkpoint_recovery": report}
    response = _redacted_resume_result({"status": "succeeded", "result": result}, "run")
    assert "checkpoint_recovery" not in response
    assert SECRET not in json.dumps(response)


@pytest.mark.parametrize("code", [[SECRET], {"secret": SECRET}], ids=["list", "object"])
@pytest.mark.parametrize("failed", [False, True], ids=["successful-launch", "refusal"])
def test_operator_omits_report_with_unhashable_problem_code(code, failed):
    report = _report("refused")
    report["problem"]["code"] = code
    result = {"checkpoint_recovery": report}
    if failed:
        result.update(error="invalid_checkpoint", message="invalid")
    else:
        result.update(invocation_id="invocation", checkpoint_run_id="checkpoint")

    response = _redacted_resume_result({"status": "succeeded", "result": result}, "run")

    assert response["resumed"] is not failed
    assert "checkpoint_recovery" not in response
    assert SECRET not in json.dumps(response)


def test_operator_preserves_refusal_with_unknown_captured_base_version():
    from lionagi._checkpoint_v3_reader import CheckpointRecoveryError, _base
    from lionagi._checkpoint_v3_reader import _report as reader_report

    report = reader_report()
    # The v3 dispatch snapshot can be replaced by this unsupported base before capture.
    with pytest.raises(CheckpointRecoveryError) as caught:
        _base(b'{"version":4}', report)
    result = {
        "error": "unsupported_checkpoint_version",
        "message": "unsupported version",
        "checkpoint_recovery": caught.value.checkpoint_recovery,
    }
    response = _redacted_resume_result({"status": "succeeded", "result": result}, "run")
    assert response["checkpoint_recovery"]["source_version"] == 4
    assert response["checkpoint_recovery"]["problem"]["code"] == "unsupported_version"


@pytest.mark.parametrize("source", [True, 4.0, -1, 2**63, SECRET])
def test_operator_rejects_unsafe_source_versions(source):
    report = _report("refused")
    report["source_version"] = source
    result = {"error": "invalid_checkpoint", "message": "invalid", "checkpoint_recovery": report}
    response = _redacted_resume_result({"status": "succeeded", "result": result}, "run")
    assert "checkpoint_recovery" not in response
    assert SECRET not in json.dumps(response)

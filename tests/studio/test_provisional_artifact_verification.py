# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for live artifact progress before a verdict is recorded."""

from __future__ import annotations

import json

import lionagi.studio.services.artifact_verification as verification_mod
from lionagi.studio.services.artifact_verification import provisional_artifact_verification

CONTRACT = {
    "expected": [
        {"id": "verdicts", "path": "VERDICTS.md", "required": True},
        {"id": "slices", "path": "SLICES.md", "required": True},
    ]
}


def test_written_artifacts_are_seen_before_the_run_finishes(tmp_path):
    (tmp_path / "scribe").mkdir()
    (tmp_path / "scribe" / "VERDICTS.md").write_text("rows")

    result = provisional_artifact_verification(CONTRACT, str(tmp_path))

    assert result is not None
    assert result["provisional"] is True
    assert [p["id"] for p in result["produced"]] == ["verdicts"]
    assert [e["id"] for e in result["missing_required"]] == ["slices"]


def test_the_answer_is_always_marked_provisional(tmp_path):
    """Without the flag a caller cannot tell this apart from the recorded
    verdict, and a mid-run reading would be read as a judgement."""
    (tmp_path / "VERDICTS.md").write_text("a")
    (tmp_path / "SLICES.md").write_text("b")

    result = provisional_artifact_verification(CONTRACT, str(tmp_path))

    assert result["status"] == "passed"
    assert result["provisional"] is True


def test_a_contract_stored_as_json_text_is_read(tmp_path):
    """The column round-trips as text on one backend and as a dict on another."""
    (tmp_path / "VERDICTS.md").write_text("a")

    result = provisional_artifact_verification(json.dumps(CONTRACT), str(tmp_path))

    assert [p["id"] for p in result["produced"]] == ["verdicts"]


def test_no_contract_produces_no_reading(tmp_path):
    assert provisional_artifact_verification(None, str(tmp_path)) is None
    assert provisional_artifact_verification({}, str(tmp_path)) is None


def test_no_artifacts_path_produces_no_reading():
    assert provisional_artifact_verification(CONTRACT, None) is None
    assert provisional_artifact_verification(CONTRACT, "") is None


def test_unparseable_contract_text_produces_no_reading(tmp_path):
    assert provisional_artifact_verification("{not json", str(tmp_path)) is None


def test_a_contract_of_the_wrong_shape_produces_no_reading(tmp_path):
    """A contract the run itself will reject is not this endpoint's to report;
    inventing a status here would put a verdict on the panel that nothing else
    agrees with."""
    assert provisional_artifact_verification({"expected": "not a list"}, str(tmp_path)) is None
    assert provisional_artifact_verification(json.dumps([1, 2, 3]), str(tmp_path)) is None


def test_a_hostile_declared_path_produces_no_reading(tmp_path):
    assert (
        provisional_artifact_verification(
            {"expected": [{"id": "x", "path": "../escape.md"}]}, str(tmp_path)
        )
        is None
    )


def test_an_unreadable_artifacts_root_produces_no_reading(tmp_path, monkeypatch):
    def raise_oserror(*_args, **_kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(verification_mod, "verify_artifact_contract", raise_oserror)

    assert provisional_artifact_verification(CONTRACT, str(tmp_path)) is None


def test_a_missing_artifacts_root_still_answers(tmp_path):
    """A run whose root does not exist yet has produced nothing, which is a
    reading rather than an error."""
    result = provisional_artifact_verification(CONTRACT, str(tmp_path / "not-created-yet"))

    assert result is not None
    assert result["produced"] == []
    assert result["provisional"] is True

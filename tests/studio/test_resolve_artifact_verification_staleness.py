# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A stored artifact-verification verdict is a completion-time snapshot;
`resolve_artifact_verification` labels it with disk currency rather than
presenting it as an unqualified current-state answer."""

from __future__ import annotations

import os

from lionagi.state.artifact_verifier import verify_artifact_contract
from lionagi.studio.services.artifact_verification import resolve_artifact_verification

CONTRACT = {"expected": [{"id": "report", "path": "report.md", "required": True}]}


def _recorded_verdict(tmp_path):
    (tmp_path / "report.md").write_text("content")
    return verify_artifact_contract(CONTRACT, artifacts_root=str(tmp_path))


def test_a_fresh_recorded_verdict_is_marked_checked_with_no_staleness_markers(tmp_path):
    stored = _recorded_verdict(tmp_path)

    resolved = resolve_artifact_verification(
        stored, status="completed", contract=CONTRACT, artifacts_path=str(tmp_path)
    )

    assert resolved["checked_at"] == stored["checked_at"]
    assert resolved["staleness_check"] == "checked"
    assert resolved["changed_since_verification"] == []
    assert resolved["absent_since_verification"] == []


def test_a_file_changed_after_the_recorded_verdict_is_labeled(tmp_path):
    stored = _recorded_verdict(tmp_path)
    artifact = tmp_path / "report.md"
    later = stored["checked_at"] + 100
    os.utime(artifact, (later, later))

    resolved = resolve_artifact_verification(
        stored, status="completed", contract=CONTRACT, artifacts_path=str(tmp_path)
    )

    assert resolved["changed_since_verification"] == ["report"]
    # The recorded status itself is untouched — this labels currency, it
    # never re-judges pass/fail.
    assert resolved["status"] == stored["status"]


def test_a_file_removed_after_the_recorded_verdict_is_labeled_absent(tmp_path):
    stored = _recorded_verdict(tmp_path)
    (tmp_path / "report.md").unlink()

    resolved = resolve_artifact_verification(
        stored, status="completed", contract=CONTRACT, artifacts_path=str(tmp_path)
    )

    assert resolved["absent_since_verification"] == ["report"]


def test_no_artifacts_path_skips_the_disk_check_and_reports_unknown(tmp_path):
    """The paginated list surface passes artifacts_path=None deliberately to
    avoid a filesystem walk per row; staleness labeling must respect that —
    and must say the check was skipped rather than imply a clean result."""
    stored = _recorded_verdict(tmp_path)
    (tmp_path / "report.md").unlink()

    resolved = resolve_artifact_verification(
        stored, status="completed", contract=CONTRACT, artifacts_path=None
    )

    assert resolved["staleness_check"] == "unknown"
    assert resolved["status"] == stored["status"]
    assert "absent_since_verification" not in resolved
    # The stored dict itself is never mutated.
    assert "staleness_check" not in stored


def test_a_legacy_verdict_missing_checked_at_resolves_unknown_not_fresh(tmp_path):
    # A payload recorded before `checked_at`/`produced` existed on the
    # verdict cannot be disk-checked at all; it must read as unknown, never
    # be indistinguishable from a verdict that was checked and came back
    # clean.
    stored = {"status": "passed", "produced": [{"id": "report", "path": "report.md", "size": 5}]}

    resolved = resolve_artifact_verification(
        stored, status="completed", contract=CONTRACT, artifacts_path=str(tmp_path)
    )

    assert resolved["staleness_check"] == "unknown"
    assert resolved["status"] == "passed"


def test_not_recorded_status_is_returned_unlabeled(tmp_path):
    stored = {"status": "not_recorded"}

    resolved = resolve_artifact_verification(
        stored, status="completed", contract=CONTRACT, artifacts_path=str(tmp_path)
    )

    assert resolved == {"status": "not_recorded"}

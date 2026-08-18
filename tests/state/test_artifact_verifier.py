"""Tests for lionagi/state/artifact_verifier.py (ADR-0064)."""

from __future__ import annotations

import os
import tempfile

import pytest

from lionagi.state.artifact_verifier import (
    ArtifactPathError,
    _safe_join,
    missing_artifact_evidence,
    missing_artifact_summary,
    resolve_artifact_contract,
    stale_artifact_markers,
    validate_artifact_contract,
    verify_artifact_contract,
)

# _safe_join


class TestSafeJoin:
    def test_simple_relative(self, tmp_path):
        result = _safe_join(str(tmp_path), "report.md")
        assert result == os.path.realpath(os.path.join(str(tmp_path), "report.md"))

    def test_subdir_relative(self, tmp_path):
        result = _safe_join(str(tmp_path), "subdir/file.txt")
        assert result.startswith(os.path.realpath(str(tmp_path)))

    def test_dotdot_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="segments not allowed"):
            _safe_join(str(tmp_path), "../escape.txt")

    def test_glob_question_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="glob characters"):
            _safe_join(str(tmp_path), "file?.md")

    def test_glob_bracket_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="glob characters"):
            _safe_join(str(tmp_path), "file[0].md")

    def test_empty_rel_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError):
            _safe_join(str(tmp_path), "")


# validate_artifact_contract


class TestValidateArtifactContract:
    def test_none_is_valid(self):
        validate_artifact_contract(None)

    def test_missing_expected_list(self):
        with pytest.raises(ArtifactPathError, match="expected: list"):
            validate_artifact_contract({"expected": "not-a-list"})

    def test_not_dict(self):
        with pytest.raises(ArtifactPathError, match="must be a dict"):
            validate_artifact_contract("invalid")  # type: ignore

    def test_duplicate_id(self):
        with pytest.raises(ArtifactPathError, match="duplicate id"):
            validate_artifact_contract(
                {
                    "expected": [
                        {"id": "report", "path": "report.md"},
                        {"id": "report", "path": "other.md"},
                    ]
                }
            )

    def test_invalid_id_with_space(self):
        with pytest.raises(ArtifactPathError, match="alphanumeric"):
            validate_artifact_contract({"expected": [{"id": "bad id", "path": "x.md"}]})

    def test_required_must_be_bool(self):
        with pytest.raises(ArtifactPathError, match="required must be a bool"):
            validate_artifact_contract(
                {"expected": [{"id": "x", "path": "x.md", "required": "yes"}]}
            )

    def test_empty_expected_list_is_valid(self):
        validate_artifact_contract({"expected": []})


# resolve_artifact_contract


class TestResolveArtifactContract:
    def test_agent_defaults_only(self):
        result = resolve_artifact_contract(
            playbook_artifacts=None,
            agent_defaults={"expected": [{"id": "report", "path": "report.md"}]},
        )
        assert result is not None
        assert len(result["expected"]) == 1
        assert result["expected"][0]["source"] == "agent_profile"

    def test_playbook_overrides_agent_same_id(self):
        result = resolve_artifact_contract(
            playbook_artifacts={"expected": [{"id": "report", "path": "playbook_report.md"}]},
            agent_defaults={"expected": [{"id": "report", "path": "agent_report.md"}]},
        )
        assert result is not None
        assert len(result["expected"]) == 1
        assert result["expected"][0]["path"] == "playbook_report.md"
        assert result["expected"][0]["source"] == "playbook"

    def test_playbook_and_agent_different_ids_merged(self):
        result = resolve_artifact_contract(
            playbook_artifacts={"expected": [{"id": "brief", "path": "brief.md"}]},
            agent_defaults={"expected": [{"id": "log", "path": "log.txt"}]},
        )
        assert result is not None
        assert len(result["expected"]) == 2

    def test_required_defaults_to_true(self):
        result = resolve_artifact_contract(
            playbook_artifacts=None,
            agent_defaults={"expected": [{"id": "x", "path": "x.md"}]},
        )
        assert result is not None
        assert result["expected"][0]["required"] is True


# verify_artifact_contract


class TestVerifyArtifactContract:
    def test_missing_root_dir_fails(self):
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root="/nonexistent_root_abc")
        assert result is not None
        assert result["status"] == "failed"
        assert len(result["missing_required"]) == 1
        assert result["produced"] == []

    def test_required_present_passes(self, tmp_path):
        (tmp_path / "report.md").write_text("content")
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
        assert result is not None
        assert result["status"] == "passed"
        assert len(result["produced"]) == 1
        assert result["produced"][0]["size"] > 0

    def test_zero_byte_required_fails(self, tmp_path):
        (tmp_path / "empty.md").write_text("")
        contract = {"expected": [{"id": "empty", "path": "empty.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
        assert result is not None
        assert result["status"] == "failed"
        assert len(result["missing_required"]) == 1

    def test_optional_missing_gives_warning(self, tmp_path):
        (tmp_path / "required.md").write_text("content")
        contract = {
            "expected": [
                {"id": "required", "path": "required.md", "required": True},
                {"id": "optional", "path": "optional.md", "required": False},
            ]
        }
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
        assert result is not None
        assert result["status"] == "warning"
        assert len(result["missing_optional"]) == 1
        assert len(result["produced"]) == 1

    def test_all_missing_required_fails_splits(self, tmp_path):
        contract = {
            "expected": [
                {"id": "req", "path": "req.md", "required": True},
                {"id": "opt", "path": "opt.md", "required": False},
            ]
        }
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
        assert result is not None
        assert result["status"] == "failed"
        assert len(result["missing_required"]) == 1
        assert len(result["missing_optional"]) == 1

    def test_all_present_passes(self, tmp_path):
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        contract = {
            "expected": [
                {"id": "a", "path": "a.md"},
                {"id": "b", "path": "b.md"},
            ]
        }
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
        assert result is not None
        assert result["status"] == "passed"
        assert len(result["produced"]) == 2


# missing_artifact_summary / evidence


def test_missing_artifact_summary_single():
    missing = [{"id": "report", "path": "report.md"}]
    summary = missing_artifact_summary(missing)
    assert "report" in summary
    assert "report.md" in summary


def test_missing_artifact_summary_plural():
    missing = [{"id": "a", "path": "a.md"}, {"id": "b", "path": "b.md"}]
    summary = missing_artifact_summary(missing)
    assert "2" in summary


def test_missing_artifact_evidence():
    missing = [{"id": "report", "path": "report.md"}]
    evidence = missing_artifact_evidence(missing)
    assert evidence == [{"kind": "expected_artifact", "id": "report", "label": "report.md"}]


# canonical names required by ADR-0064 test plan


def test_resolve_contract_both_none():
    assert resolve_artifact_contract(playbook_artifacts=None, agent_defaults=None) is None


def test_resolve_contract_playbook_only():
    result = resolve_artifact_contract(
        playbook_artifacts={"expected": [{"id": "brief", "path": "brief.md"}]},
        agent_defaults=None,
    )
    assert result is not None
    assert len(result["expected"]) == 1
    assert result["expected"][0]["source"] == "playbook"


def test_resolve_contract_agent_only():
    result = resolve_artifact_contract(
        playbook_artifacts=None,
        agent_defaults={"expected": [{"id": "report", "path": "report.md"}]},
    )
    assert result is not None
    assert result["expected"][0]["source"] == "agent_profile"


def test_resolve_contract_merge_union():
    result = resolve_artifact_contract(
        playbook_artifacts={"expected": [{"id": "brief", "path": "brief.md"}]},
        agent_defaults={"expected": [{"id": "log", "path": "log.txt"}]},
    )
    assert result is not None
    ids = {e["id"] for e in result["expected"]}
    assert ids == {"brief", "log"}


def test_resolve_contract_merge_override():
    result = resolve_artifact_contract(
        playbook_artifacts={"expected": [{"id": "report", "path": "playbook.md"}]},
        agent_defaults={"expected": [{"id": "report", "path": "agent.md"}]},
    )
    assert result is not None
    assert len(result["expected"]) == 1
    assert result["expected"][0]["path"] == "playbook.md"
    assert result["expected"][0]["source"] == "playbook"


def test_validate_contract_valid():
    validate_artifact_contract({"expected": [{"id": "report", "path": "report.md"}]})


def test_validate_contract_duplicate_id():
    with pytest.raises(ArtifactPathError, match="duplicate id"):
        validate_artifact_contract(
            {
                "expected": [
                    {"id": "report", "path": "a.md"},
                    {"id": "report", "path": "b.md"},
                ]
            }
        )


def test_validate_contract_bad_id_chars():
    with pytest.raises(ArtifactPathError, match="alphanumeric"):
        validate_artifact_contract({"expected": [{"id": "bad id!", "path": "x.md"}]})


def test_validate_contract_absolute_path():
    with pytest.raises(ArtifactPathError, match="absolute path not allowed"):
        validate_artifact_contract({"expected": [{"id": "x", "path": "/etc/passwd"}]})


def test_validate_contract_dotdot_path():
    with pytest.raises(ArtifactPathError, match="segments not allowed"):
        validate_artifact_contract({"expected": [{"id": "x", "path": "../escape.md"}]})


def test_validate_contract_glob_path():
    with pytest.raises(ArtifactPathError, match="glob characters"):
        validate_artifact_contract({"expected": [{"id": "x", "path": "*.md"}]})


def test_verify_no_contract():
    assert verify_artifact_contract(None, artifacts_root="/tmp") is None


def test_verify_no_artifacts_dir(tmp_path):
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path / "nonexistent"))
    assert result is not None
    assert result["status"] == "failed"
    assert len(result["missing_required"]) == 1


def test_verify_all_present(tmp_path):
    (tmp_path / "a.md").write_text("content a")
    (tmp_path / "b.md").write_text("content b")
    contract = {"expected": [{"id": "a", "path": "a.md"}, {"id": "b", "path": "b.md"}]}
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
    assert result is not None
    assert result["status"] == "passed"
    assert len(result["produced"]) == 2


def test_verify_required_missing(tmp_path):
    # Dir exists but required artifact is not in it.
    (tmp_path / "other.md").write_text("unrelated")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
    assert result is not None
    assert result["status"] == "failed"
    assert any(e["id"] == "report" for e in result["missing_required"])


def test_verify_optional_missing(tmp_path):
    (tmp_path / "required.md").write_text("content")
    contract = {
        "expected": [
            {"id": "required", "path": "required.md", "required": True},
            {"id": "optional", "path": "optional.md", "required": False},
        ]
    }
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
    assert result is not None
    assert result["status"] == "warning"
    assert len(result["missing_optional"]) == 1


def test_verify_empty_file(tmp_path):
    (tmp_path / "empty.md").write_text("")
    contract = {"expected": [{"id": "empty", "path": "empty.md", "required": True}]}
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
    assert result is not None
    assert result["status"] == "failed"


def test_verify_optional_only_missing_dir(tmp_path):
    contract = {
        "expected": [
            {"id": "notes", "path": "notes.md", "required": False},
            {"id": "log", "path": "log.txt", "required": False},
        ]
    }
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path / "nonexistent"))
    assert result is not None
    assert result["status"] == "warning"
    assert len(result["missing_optional"]) == 2
    assert result["missing_required"] == []


def test_verify_mixed_required_optional_missing_dir(tmp_path):
    contract = {
        "expected": [
            {"id": "req", "path": "req.md", "required": True},
            {"id": "opt", "path": "opt.md", "required": False},
        ]
    }
    result = verify_artifact_contract(contract, artifacts_root=str(tmp_path / "nonexistent"))
    assert result is not None
    assert result["status"] == "failed"
    assert len(result["missing_required"]) == 1
    assert len(result["missing_optional"]) == 1


def test_safe_join_normal(tmp_path):
    result = _safe_join(str(tmp_path), "subdir/report.md")
    assert result.startswith(os.path.realpath(str(tmp_path)))
    assert result.endswith("report.md")


def test_safe_join_absolute_rejects(tmp_path):
    with pytest.raises(ArtifactPathError, match="absolute path not allowed"):
        _safe_join(str(tmp_path), "/etc/passwd")


def test_safe_join_dotdot_rejects(tmp_path):
    with pytest.raises(ArtifactPathError, match="segments not allowed"):
        _safe_join(str(tmp_path), "../escape.md")


def test_safe_join_glob_rejects(tmp_path):
    with pytest.raises(ArtifactPathError, match="glob characters"):
        _safe_join(str(tmp_path), "*.md")


# A bare filename resolves to whichever worker produced it
#
# In a multi-agent run each worker writes into its own subdirectory of the
# artifacts root, and which worker produces a given artifact is decided when the
# plan is cast. A playbook contract therefore cannot name that subdirectory in
# advance, so a bare filename used to be impossible to satisfy: every declared
# artifact verified as MISSING even when the file was sitting one level down,
# which in turn rewrote a completed run to failed.


def _contract(*entries):
    return {"expected": list(entries)}


def test_bare_filename_found_in_a_worker_subdirectory(tmp_path):
    (tmp_path / "scribe").mkdir()
    (tmp_path / "scribe" / "VERDICTS.md").write_text("rows")

    result = verify_artifact_contract(
        _contract({"id": "verdicts", "path": "VERDICTS.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    assert result["status"] == "passed"
    assert result["missing_required"] == []
    assert result["produced"][0]["id"] == "verdicts"


def test_produced_reports_where_the_file_actually_is(tmp_path):
    # _context_from reads produced["path"] relative to the root, so reporting
    # the declared name rather than the resolved one would hand it a path that
    # does not exist.
    (tmp_path / "planner").mkdir()
    (tmp_path / "planner" / "SLICES.md").write_text("slices")

    result = verify_artifact_contract(
        _contract({"id": "slices", "path": "SLICES.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    entry = result["produced"][0]
    assert entry["path"] == os.path.join("planner", "SLICES.md")
    assert (tmp_path / entry["path"]).read_text() == "slices"


def test_a_file_at_the_root_still_wins_over_a_subdirectory_copy(tmp_path):
    (tmp_path / "REPORT.md").write_text("root copy")
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "REPORT.md").write_text("subdir copy")

    result = verify_artifact_contract(
        _contract({"id": "r", "path": "REPORT.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    assert result["produced"][0]["path"] == "REPORT.md"


def test_same_filename_in_two_subdirectories_resolves_deterministically(tmp_path):
    for name in ("zeta", "alpha", "mid"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "OUT.md").write_text(name)

    seen = {
        verify_artifact_contract(
            _contract({"id": "o", "path": "OUT.md", "required": True}),
            artifacts_root=str(tmp_path),
        )["produced"][0]["path"]
        for _ in range(5)
    }
    assert seen == {os.path.join("alpha", "OUT.md")}


def test_a_directory_qualified_path_is_matched_exactly_and_not_searched_elsewhere(tmp_path):
    # role_default entries name their own directory. That precision must be
    # honoured: a contract asking for critic/review.md is not satisfied by some
    # other worker's review.md.
    (tmp_path / "writer").mkdir()
    (tmp_path / "writer" / "review.md").write_text("wrong author")

    result = verify_artifact_contract(
        _contract({"id": "cr", "path": "critic/review.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    assert result["status"] == "failed"
    assert [e["id"] for e in result["missing_required"]] == ["cr"]


def test_a_nested_bare_filename_is_not_found_two_levels_down(tmp_path):
    # The search is one level deep on purpose: workers write into their own
    # directory, not into arbitrary trees, and an unbounded walk would let an
    # incidental file elsewhere satisfy a contract.
    deep = tmp_path / "worker" / "nested"
    deep.mkdir(parents=True)
    (deep / "DEEP.md").write_text("too far")

    result = verify_artifact_contract(
        _contract({"id": "d", "path": "DEEP.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    assert result["status"] == "failed"


def test_an_empty_file_in_a_subdirectory_is_still_missing(tmp_path):
    # A worker that created the file and wrote nothing has not produced it.
    (tmp_path / "scribe").mkdir()
    (tmp_path / "scribe" / "EMPTY.md").write_text("")

    result = verify_artifact_contract(
        _contract({"id": "e", "path": "EMPTY.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    assert result["status"] == "failed"


def test_a_genuinely_absent_artifact_is_still_missing(tmp_path):
    (tmp_path / "scribe").mkdir()
    (tmp_path / "scribe" / "SOMETHING_ELSE.md").write_text("x")

    result = verify_artifact_contract(
        _contract({"id": "gone", "path": "NOT_THERE.md", "required": True}),
        artifacts_root=str(tmp_path),
    )
    assert result["status"] == "failed"
    assert [e["id"] for e in result["missing_required"]] == ["gone"]


def test_the_reported_run_shape_now_passes_end_to_end(tmp_path):
    # The exact contract and layout that produced a false failure: four
    # playbook-declared bare filenames, each written by a different worker,
    # plus one role_default entry that already named its own directory.
    layout = {
        "scribe": "VERDICTS.md",
        "planner": "SLICES.md",
        "writer": "STALE.md",
        "advisor": "PARKED.md",
        "critic": "review.md",
    }
    for agent, filename in layout.items():
        (tmp_path / agent).mkdir()
        (tmp_path / agent / filename).write_text("content")

    result = verify_artifact_contract(
        _contract(
            {"id": "verdicts", "path": "VERDICTS.md", "required": True},
            {"id": "slices", "path": "SLICES.md", "required": True},
            {"id": "stale", "path": "STALE.md", "required": False},
            {"id": "parked", "path": "PARKED.md", "required": False},
            {"id": "critic__review", "path": "critic/review.md", "required": True},
        ),
        artifacts_root=str(tmp_path),
    )
    assert result["status"] == "passed"
    assert result["missing_required"] == []
    assert result["missing_optional"] == []
    assert len(result["produced"]) == 5


# stale_artifact_markers
#
# A stored verification is a snapshot taken at run completion. These markers
# never re-verify pass/fail; they only flag, via mtime and presence, whether
# the artifacts that snapshot found may no longer match what is on disk now.


class TestStaleArtifactMarkers:
    def test_a_fresh_snapshot_is_marked_checked_with_no_staleness(self, tmp_path):
        (tmp_path / "report.md").write_text("content")
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))

        markers = stale_artifact_markers(result, artifacts_root=str(tmp_path))
        assert markers is not None
        assert markers["staleness_check"] == "checked"
        assert markers["changed_since_verification"] == []
        assert markers["absent_since_verification"] == []

    def test_a_file_touched_after_checked_at_is_flagged_changed(self, tmp_path):
        artifact = tmp_path / "report.md"
        artifact.write_text("content")
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))

        later = result["checked_at"] + 100
        os.utime(artifact, (later, later))

        markers = stale_artifact_markers(result, artifacts_root=str(tmp_path))
        assert markers is not None
        assert markers["changed_since_verification"] == ["report"]
        assert markers["absent_since_verification"] == []

    def test_a_file_removed_after_verification_is_flagged_absent(self, tmp_path):
        artifact = tmp_path / "report.md"
        artifact.write_text("content")
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))

        artifact.unlink()

        markers = stale_artifact_markers(result, artifacts_root=str(tmp_path))
        assert markers is not None
        assert markers["absent_since_verification"] == ["report"]
        assert markers["changed_since_verification"] == []

    def test_no_artifacts_root_produces_no_markers(self, tmp_path):
        (tmp_path / "report.md").write_text("content")
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))

        assert stale_artifact_markers(result, artifacts_root=None) is None
        assert stale_artifact_markers(result, artifacts_root="") is None

    def test_a_missing_required_artifact_is_not_reported_as_stale(self, tmp_path):
        # It was never produced, so there is nothing on disk to have changed —
        # that is `missing_required`'s claim to make, not this one's.
        contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))

        markers = stale_artifact_markers(result, artifacts_root=str(tmp_path))
        assert markers is not None
        assert markers["staleness_check"] == "checked"
        assert markers["changed_since_verification"] == []
        assert markers["absent_since_verification"] == []

    def test_a_size_change_with_preserved_mtime_is_flagged_changed(self, tmp_path):
        # A rewrite that preserves mtime defeats a bare mtime comparison; the
        # comparator also compares size (from the same stat() call) so this
        # false-negative window is narrowed rather than left silent.
        artifact = tmp_path / "report.md"
        artifact.write_text("content")
        contract = {"expected": [{"id": "report", "path": "report.md"}]}
        result = verify_artifact_contract(contract, artifacts_root=str(tmp_path))
        original_mtime = os.path.getmtime(artifact)

        artifact.write_text("content, but a fair bit longer than before")
        os.utime(artifact, (original_mtime, original_mtime))

        markers = stale_artifact_markers(result, artifacts_root=str(tmp_path))
        assert markers is not None
        assert markers["changed_since_verification"] == ["report"]

    def test_malformed_verification_produces_no_markers(self, tmp_path):
        assert stale_artifact_markers({}, artifacts_root=str(tmp_path)) is None
        assert (
            stale_artifact_markers({"checked_at": "not-a-number"}, artifacts_root=str(tmp_path))
            is None
        )

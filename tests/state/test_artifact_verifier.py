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
    validate_artifact_contract,
    verify_artifact_contract,
)

# ── _safe_join ────────────────────────────────────────────────────────────────


class TestSafeJoin:
    def test_simple_relative(self, tmp_path):
        result = _safe_join(str(tmp_path), "report.md")
        assert result == os.path.realpath(os.path.join(str(tmp_path), "report.md"))

    def test_subdir_relative(self, tmp_path):
        result = _safe_join(str(tmp_path), "subdir/file.txt")
        assert result.startswith(os.path.realpath(str(tmp_path)))

    def test_absolute_path_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="absolute path not allowed"):
            _safe_join(str(tmp_path), "/etc/passwd")

    def test_dotdot_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="segments not allowed"):
            _safe_join(str(tmp_path), "../escape.txt")

    def test_glob_star_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="glob characters"):
            _safe_join(str(tmp_path), "*.md")

    def test_glob_question_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="glob characters"):
            _safe_join(str(tmp_path), "file?.md")

    def test_glob_bracket_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError, match="glob characters"):
            _safe_join(str(tmp_path), "file[0].md")

    def test_empty_rel_rejected(self, tmp_path):
        with pytest.raises(ArtifactPathError):
            _safe_join(str(tmp_path), "")


# ── validate_artifact_contract ───────────────────────────────────────────────


class TestValidateArtifactContract:
    def test_none_is_valid(self):
        validate_artifact_contract(None)

    def test_valid_contract(self):
        validate_artifact_contract({"expected": [{"id": "report", "path": "report.md"}]})

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

    def test_absolute_path_rejected_via_validate(self):
        with pytest.raises(ArtifactPathError, match="absolute path not allowed"):
            validate_artifact_contract({"expected": [{"id": "x", "path": "/etc/passwd"}]})

    def test_required_must_be_bool(self):
        with pytest.raises(ArtifactPathError, match="required must be a bool"):
            validate_artifact_contract(
                {"expected": [{"id": "x", "path": "x.md", "required": "yes"}]}
            )

    def test_empty_expected_list_is_valid(self):
        validate_artifact_contract({"expected": []})


# ── resolve_artifact_contract ─────────────────────────────────────────────────


class TestResolveArtifactContract:
    def test_both_none_returns_none(self):
        assert resolve_artifact_contract(playbook_artifacts=None, agent_defaults=None) is None

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


# ── verify_artifact_contract ──────────────────────────────────────────────────


class TestVerifyArtifactContract:
    def test_none_contract_returns_none(self):
        assert verify_artifact_contract(None, artifacts_root="/tmp") is None

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


# ── missing_artifact_summary / evidence ──────────────────────────────────────


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


# ── canonical names required by ADR-0064 test plan ───────────────────────────


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

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The running code's self-report, and the checks that call it out when it drifts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lionagi.cli import _code_identity, doctor
from lionagi.cli._code_identity import code_identity, git_identity


class _Args:
    """Stand-in for the argparse namespace `run_doctor` reads."""

    json = False


def _git(tree: Path, *argv: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(tree),
            *argv,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(tree: Path, name: str) -> None:
    (tree / name).write_text(name)
    _git(tree, "add", name)
    _git(tree, "commit", "-m", name)


@pytest.fixture
def checkout_behind(tmp_path: Path) -> Path:
    """A working checkout whose HEAD is one commit behind its own upstream."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main", ".")
    _commit(work, "first")
    _commit(work, "second")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "reset", "--hard", "HEAD~1")
    return work


def test_checkout_behind_upstream_reads_as_drift(checkout_behind: Path) -> None:
    git = git_identity(checkout_behind)
    assert git["status"] == "ok"
    assert git["comparison_ref"] == "origin/main"
    assert git["comparison_ref_source"] == "upstream"
    assert git["behind"] == 1
    assert git["ahead"] == 0


def test_detached_checkout_falls_back_to_the_remote_default_branch(
    checkout_behind: Path,
) -> None:
    """A pinned deployment has no upstream — the remote's HEAD still answers for it."""
    _git(checkout_behind, "remote", "set-head", "origin", "-a")
    _git(checkout_behind, "checkout", "--detach", "HEAD")

    git = git_identity(checkout_behind)
    assert git["status"] == "ok"
    assert git["detached"] is True
    assert git["branch"] is None
    assert git["comparison_ref_source"] == "remote_head"
    assert git["behind"] == 1


def test_up_to_date_checkout_is_not_behind(checkout_behind: Path) -> None:
    """The guard fires on drift, not on every checkout that has a remote."""
    _git(checkout_behind, "merge", "--ff-only", "origin/main")

    git = git_identity(checkout_behind)
    assert git["status"] == "ok"
    assert git["behind"] == 0


def test_directory_outside_any_checkout_says_so_plainly(tmp_path: Path) -> None:
    git = git_identity(tmp_path)
    assert git["status"] == "not_a_git_checkout"
    assert str(tmp_path) in git["detail"]


def test_unreadable_head_is_unknown_not_ok(tmp_path: Path) -> None:
    """An initialized tree with no commits: git answers, HEAD does not resolve."""
    _git(tmp_path, "init", "-b", "main", ".")

    git = git_identity(tmp_path)
    assert git["status"] == "unknown"
    assert "HEAD" in git["detail"]


def test_missing_git_binary_is_unknown_not_a_missing_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(_code_identity.subprocess, "run", _no_git)

    git = git_identity(tmp_path)
    assert git["status"] == "unknown"
    assert "FileNotFoundError" in git["detail"]


def test_no_comparison_ref_is_unknown_not_ok(tmp_path: Path) -> None:
    """A checkout with no remote cannot be measured, so it is not declared current."""
    _git(tmp_path, "init", "-b", "main", ".")
    _commit(tmp_path, "only")

    git = git_identity(tmp_path)
    assert git["status"] == "ok"
    assert git["comparison_ref"] is None
    assert git["behind"] is None

    drift = _code_identity._drift(git, "1.0.0", "1.0.0")
    assert drift["status"] == "unknown"
    assert drift["unknown"]


# ── the drift verdict ────────────────────────────────────────────────────────


def test_version_mismatch_against_installed_distribution_is_drift() -> None:
    git = {"status": "not_a_git_checkout", "detail": "wheel install"}
    drift = _code_identity._drift(git, "0.1.0", "9.9.9")
    assert drift["status"] == "drift"
    assert any("9.9.9" in reason for reason in drift["reasons"])


def test_wheel_install_with_matching_version_is_ok() -> None:
    git = {"status": "not_a_git_checkout", "detail": "wheel install"}
    assert _code_identity._drift(git, "0.1.0", "0.1.0")["status"] == "ok"


def test_behind_outranks_unknown_in_the_verdict() -> None:
    git = {"status": "ok", "behind": 24, "comparison_ref": "origin/main"}
    drift = _code_identity._drift(git, "0.1.0", None)
    assert drift["status"] == "drift"
    assert any("24 commit(s) behind" in reason for reason in drift["reasons"])


def test_code_identity_reports_this_process() -> None:
    identity = code_identity()
    assert identity["version"]
    assert identity["package_path"].endswith("/lionagi")
    assert Path(identity["package_path"]).is_dir()
    assert identity["verb_count"] > 0
    assert identity["git"]["status"] in ("ok", "not_a_git_checkout", "unknown")
    assert identity["drift"]["status"] in ("ok", "drift", "unknown")


# ── the doctor check ─────────────────────────────────────────────────────────


def _identity(drift_status: str, **overrides: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "version": "0.1.0",
        "package_path": "/somewhere/lionagi",
        "distribution_version": "0.1.0",
        "verb_count": 40,
        "git": {
            "status": "ok",
            "commit_short": "abc123def456",
            "branch": None,
            "detached": True,
        },
        "drift": {
            "status": drift_status,
            "reasons": ["24 commit(s) behind origin/main"] if drift_status == "drift" else [],
            "unknown": ["git state unreadable"] if drift_status == "unknown" else [],
        },
    }
    identity.update(overrides)
    return identity


def test_doctor_fails_on_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_code_identity, "code_identity", lambda: _identity("drift"))
    result = doctor._check_code_identity()
    assert result["status"] == "fail"
    assert "24 commit(s) behind origin/main" in result["detail"]
    assert "40 verbs" in result["detail"]


def test_doctor_reports_unknown_rather_than_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_code_identity, "code_identity", lambda: _identity("unknown"))
    result = doctor._check_code_identity()
    assert result["status"] == "unknown"
    assert "git state unreadable" in result["detail"]


def test_doctor_ok_when_identity_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_code_identity, "code_identity", lambda: _identity("ok"))
    result = doctor._check_code_identity()
    assert result["status"] == "ok"
    assert "abc123def456 (detached)" in result["detail"]


def test_doctor_check_that_cannot_run_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, object]:
        raise RuntimeError("no")

    monkeypatch.setattr(_code_identity, "code_identity", _boom)
    assert doctor._check_code_identity()["status"] == "unknown"


def test_run_doctor_exits_nonzero_on_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda: {"code_identity": {"status": "unknown", "detail": "cannot tell"}},
    )
    assert doctor.run_doctor(_Args()) == 1


# ── the surfaces a client reads ──────────────────────────────────────────────


def test_handshake_carries_code_identity() -> None:
    from lionagi.cli.machine import handshake_data

    identity = handshake_data()["code_identity"]
    assert identity["package_path"]
    assert identity["verb_count"] > 0
    assert "status" in identity["drift"]


def test_server_info_carries_code_identity() -> None:
    from lionagi.mcp.dispatch import _server_info

    info = _server_info()
    assert info["code_identity"]["version"] == info["lionagi_version"]
    assert info["code_identity"]["verb_count"] == info["verb_count"]


def test_doctor_machine_payload_separates_unknown_from_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi.cli import machine

    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda: {
            "a": {"status": "unknown", "detail": "cannot tell"},
            "b": {"status": "fail", "detail": "broken"},
            "c": {"status": "ok", "detail": "fine"},
        },
    )
    data = machine.doctor_data()
    assert data["failed"] == ["b"]
    assert data["unknown"] == ["a"]

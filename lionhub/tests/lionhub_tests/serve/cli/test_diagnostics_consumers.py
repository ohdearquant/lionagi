# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI and client surfaces consume the running code's identity and drift verdict."""

from __future__ import annotations

import pytest

from lionhub.serve import diagnostics
from lionhub.serve.cli import doctor


class _Args:
    """Stand-in for the argparse namespace `run_doctor` reads."""

    json = False


def test_the_server_snapshots_its_position_before_it_starts_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionhub.serve.mcp import server

    monkeypatch.setattr(diagnostics, "_SNAPSHOT", None)
    seen: dict[str, object] = {}
    monkeypatch.setattr(server.mcp, "run", lambda: seen.update(snapshot=diagnostics._SNAPSHOT))

    server.main()
    assert seen["snapshot"] is not None


def test_doctor_says_the_tree_was_dirty_beside_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diagnostics,
        "code_identity",
        lambda: _identity(
            "unknown",
            git={
                "status": "ok",
                "commit_short": "abc123def456",
                "branch": None,
                "detached": True,
                "dirty": True,
            },
        ),
    )
    assert (
        "abc123def456 (detached) with uncommitted changes"
        in (doctor._check_code_identity()["detail"])
    )


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
    monkeypatch.setattr(diagnostics, "code_identity", lambda: _identity("drift"))
    result = doctor._check_code_identity()
    assert result["status"] == "fail"
    assert "24 commit(s) behind origin/main" in result["detail"]
    assert "40 verbs" in result["detail"]


def test_doctor_reports_unknown_rather_than_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "code_identity", lambda: _identity("unknown"))
    result = doctor._check_code_identity()
    assert result["status"] == "unknown"
    assert "git state unreadable" in result["detail"]


def test_doctor_ok_when_identity_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "code_identity", lambda: _identity("ok"))
    result = doctor._check_code_identity()
    assert result["status"] == "ok"
    assert "abc123def456 (detached)" in result["detail"]


def test_doctor_quotes_when_the_position_was_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit with no time attached reads as current; this one says what it is."""
    monkeypatch.setattr(
        diagnostics,
        "code_identity",
        lambda: _identity("ok", git_snapshot_taken_at="2026-07-26T00:00:00+00:00"),
    )
    detail = doctor._check_code_identity()["detail"]
    assert "as read at 2026-07-26T00:00:00+00:00" in detail


def test_doctor_check_that_cannot_run_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, object]:
        raise RuntimeError("no")

    monkeypatch.setattr(diagnostics, "code_identity", _boom)
    assert doctor._check_code_identity()["status"] == "unknown"


def test_run_doctor_exits_nonzero_on_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctor,
        "collect_checks",
        lambda: {"code_identity": {"status": "unknown", "detail": "cannot tell"}},
    )
    assert doctor.run_doctor(_Args()) == 1


def test_handshake_carries_code_identity() -> None:
    from lionhub.serve.cli.machine import handshake_data

    identity = handshake_data()["code_identity"]
    assert identity["package_path"]
    assert identity["verb_count"] > 0
    assert "status" in identity["drift"]


def test_server_info_carries_code_identity() -> None:
    from lionhub.serve.mcp.dispatch import _server_info

    info = _server_info()
    assert info["code_identity"]["version"] == info["lionagi_version"]
    assert info["code_identity"]["verb_count"] == info["verb_count"]


def test_doctor_machine_payload_separates_unknown_from_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionhub.serve.cli import machine

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

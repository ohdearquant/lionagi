# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.cli.main — _handle_play_shortcut."""

from lionagi.cli.main import _handle_play_shortcut


def test_handle_play_shortcut_rewrites_name_to_flow_argv():
    """play <name> [flags] is rewritten to o flow -p <name> [flags]."""
    result = _handle_play_shortcut(["play", "triage", "--x"])
    assert result == ["o", "flow", "-p", "triage", "--x"]


def test_handle_play_shortcut_rejects_flag_before_name(monkeypatch):
    """play --bad returns exit code 1 because flag comes before name."""
    import lionagi.cli._logging as log_mod

    monkeypatch.setattr(log_mod, "log_error", lambda *a, **kw: None)
    result = _handle_play_shortcut(["play", "--bad"])
    assert result == 1


def test_handle_play_shortcut_passthrough_for_non_play():
    """Non-play first arg returns argv unchanged."""
    argv = ["agent", "x"]
    result = _handle_play_shortcut(argv)
    assert result == argv


# ─── ADR-0029 §9: `li play check` pre-flight artifact contract ───


def test_play_check_no_args_prints_usage(capsys):
    """`li play check` (no name) returns 1 and prints usage."""
    result = _handle_play_shortcut(["play", "check"])
    captured = capsys.readouterr()
    assert result == 1
    assert "Usage: li play check" in captured.out


def test_play_check_missing_playbook_returns_error(caplog):
    """Unknown playbook name surfaces the resolution error and returns 1."""
    import logging

    # Other tests in the suite call configure_cli_logging() which sets
    # propagate=False on this channel; restore default so caplog captures.
    err_logger = logging.getLogger("lionagi.cli.error")
    err_logger.handlers.clear()
    err_logger.propagate = True

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        result = _handle_play_shortcut(["play", "check", "no-such-playbook-xyz"])
    assert result == 1
    assert any(
        "no-such-playbook-xyz" in rec.message or "not found" in rec.message
        for rec in caplog.records
    ), (
        f"expected error about missing playbook; got log records={[r.message for r in caplog.records]!r}"
    )


def test_play_check_playbook_with_contract(tmp_path, monkeypatch, capsys):
    """A playbook with `artifacts:` resolves and prints required/optional summary."""
    pb_dir = tmp_path
    pb_path = pb_dir / "fixture.playbook.yaml"
    pb_path.write_text(
        "name: fixture\n"
        "model: claude/sonnet\n"
        "prompt: |\n"
        "  do x\n"
        "artifacts:\n"
        "  expected:\n"
        "    - id: review\n"
        "      path: review.md\n"
        "      required: true\n"
        "      description: Reviewer output\n"
        "    - id: notes\n"
        "      path: notes.md\n"
        "      required: false\n"
    )

    # Redirect the playbook lookup root.
    from lionagi.cli import orchestrate as _orch

    real_resolve = _orch._resolve_playbook_path

    def fake_resolve(name):
        if name == "fixture":
            return pb_path, None
        return real_resolve(name)

    monkeypatch.setattr(_orch, "_resolve_playbook_path", fake_resolve)
    monkeypatch.setattr("lionagi.cli.main._resolve_playbook_path", fake_resolve, raising=False)

    result = _handle_play_shortcut(["play", "check", "fixture"])
    out = capsys.readouterr().out
    assert result == 0, f"expected pass, got {result}; output: {out!r}"
    assert "fixture" in out
    assert "1 required" in out and "1 optional" in out
    assert "review" in out and "notes" in out


def test_play_check_playbook_without_contract(tmp_path, monkeypatch, capsys):
    """A playbook without `artifacts:` exits 0 and reports verification skipped."""
    pb_path = tmp_path / "plain.playbook.yaml"
    pb_path.write_text("name: plain\nmodel: claude/sonnet\nprompt: do y\n")

    from lionagi.cli import orchestrate as _orch

    def fake_resolve(name):
        return (pb_path, None) if name == "plain" else _orch._resolve_playbook_path(name)

    monkeypatch.setattr(_orch, "_resolve_playbook_path", fake_resolve)
    monkeypatch.setattr("lionagi.cli.main._resolve_playbook_path", fake_resolve, raising=False)

    result = _handle_play_shortcut(["play", "check", "plain"])
    out = capsys.readouterr().out
    assert result == 0
    assert "no `artifacts:` block declared" in out


# ─── #1194: `li play <name> --help` surfaces forwarded global flags ───


def test_play_help_shows_common_flags(tmp_path, monkeypatch, capsys):
    """li play <name> --help must surface the forwarded li o flow flags."""
    pb_path = tmp_path / "mypb.playbook.yaml"
    pb_path.write_text(
        "name: mypb\nmodel: claude/sonnet\ndescription: My playbook\nprompt: do something\n"
    )

    from lionagi.cli import orchestrate as _orch

    monkeypatch.setattr(_orch, "_resolve_playbook_path", lambda n: (pb_path, None))

    result = _handle_play_shortcut(["play", "mypb", "--help"])
    out = capsys.readouterr().out
    assert result == 0
    # Forwarded flags must appear in help output.
    assert "--bypass" in out
    assert "--team-mode" in out
    assert "--timeout" in out


def test_play_flag_before_name_includes_usage(caplog):
    """li play --flag returns 1 and the error message includes a usage line."""
    import logging

    err_logger = logging.getLogger("lionagi.cli.error")
    err_logger.handlers.clear()
    err_logger.propagate = True

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        result = _handle_play_shortcut(["play", "--bypass"])

    assert result == 1
    full_msg = " ".join(r.message for r in caplog.records)
    assert "Usage" in full_msg or "li play" in full_msg

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.cli.main — _handle_play_shortcut."""

import subprocess
import sys

import pytest
import yaml

from lionagi.cli._util import EXIT_CODE_ENVIRONMENT_ERROR
from lionagi.cli.main import _handle_play_shortcut, main
from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS


def test_main_resolves_notify_settings_project_dir_from_cwd_flag(monkeypatch):
    """`li <cmd> --cwd DIR ...` must resolve notify.on_terminal against DIR's
    own .lionagi/settings.yaml, not the shell's own cwd -- the bootstrap
    call happens before argparse has parsed --cwd for any subcommand, so
    main() pre-scans argv for it the same way it already does for -v."""
    calls: list[dict] = []

    def _spy(*, project_dir=None):
        calls.append({"project_dir": project_dir})
        return False

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_settings_terminal_callback",
        _spy,
    )
    main(["skill", "--cwd", "/some/project", "definitely-not-a-real-skill-xyz"])
    assert calls == [{"project_dir": "/some/project"}]


def test_main_resolves_notify_settings_project_dir_from_cwd_equals_form(monkeypatch):
    calls: list[dict] = []

    def _spy(*, project_dir=None):
        calls.append({"project_dir": project_dir})
        return False

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_settings_terminal_callback",
        _spy,
    )
    main(["skill", "--cwd=/other/project", "definitely-not-a-real-skill-xyz"])
    assert calls == [{"project_dir": "/other/project"}]


def test_main_resolves_notify_settings_project_dir_last_of_repeated_cwd_flags(monkeypatch):
    """`--cwd /a --cwd /b` must resolve against /b -- argparse itself honors the
    last occurrence of a repeated flag, so the pre-argparse notify-bootstrap scan
    must mirror that precedence instead of taking the first match."""
    calls: list[dict] = []

    def _spy(*, project_dir=None):
        calls.append({"project_dir": project_dir})
        return False

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_settings_terminal_callback",
        _spy,
    )
    main(
        [
            "skill",
            "--cwd",
            "/a",
            "--cwd",
            "/b",
            "definitely-not-a-real-skill-xyz",
        ]
    )
    assert calls == [{"project_dir": "/b"}]


def test_main_resolves_notify_settings_project_dir_last_of_mixed_cwd_forms(monkeypatch):
    """Mixed `--cwd DIR` / `--cwd=DIR` forms still resolve to whichever occurs
    last in argv, regardless of which form it uses."""
    calls: list[dict] = []

    def _spy(*, project_dir=None):
        calls.append({"project_dir": project_dir})
        return False

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_settings_terminal_callback",
        _spy,
    )
    main(
        [
            "skill",
            "--cwd=/a",
            "--cwd",
            "/b",
            "definitely-not-a-real-skill-xyz",
        ]
    )
    assert calls == [{"project_dir": "/b"}]

    calls.clear()
    main(
        [
            "skill",
            "--cwd",
            "/a",
            "--cwd=/b",
            "definitely-not-a-real-skill-xyz",
        ]
    )
    assert calls == [{"project_dir": "/b"}]


def test_main_notify_settings_project_dir_none_without_cwd_flag(monkeypatch):
    calls: list[dict] = []

    def _spy(*, project_dir=None):
        calls.append({"project_dir": project_dir})
        return False

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_settings_terminal_callback",
        _spy,
    )
    main(["skill", "definitely-not-a-real-skill-xyz"])
    assert calls == [{"project_dir": None}]


def test_main_notify_settings_ignores_cwd_after_end_of_options_sentinel(monkeypatch):
    """A literal '--cwd VALUE' occurring only after '--' (e.g. inside a
    scheduled action_prompt's free-form text) must not be picked up, mirroring
    the existing -v/--verbose sentinel-respecting scan."""
    calls: list[dict] = []

    def _spy(*, project_dir=None):
        calls.append({"project_dir": project_dir})
        return False

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_settings_terminal_callback",
        _spy,
    )
    main(["skill", "definitely-not-a-real-skill-xyz", "--", "--cwd", "/should/not/be/used"])
    assert calls == [{"project_dir": None}]


@pytest.mark.parametrize(
    "filter_value",
    [
        "session",
        {"unexpected": True},
        {"kinds": 0},
        {"kinds": ["not-a-terminal-entity"]},
    ],
    ids=["non-mapping", "unknown-key", "non-list-kinds", "unknown-kind"],
)
def test_main_bootstrap_disables_invalid_notify_filter(tmp_path, filter_value):
    settings_dir = tmp_path / ".lionagi"
    settings_dir.mkdir()
    (settings_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "notify": {
                    "on_terminal": {
                        "enabled": True,
                        "adapter": {"kind": "exec", "argv": ["echo", "ok"]},
                        "filter": filter_value,
                    }
                }
            }
        )
    )

    name = "notify.settings.on_terminal"
    DEFAULT_TERMINAL_CALLBACKS.unregister(name)
    try:
        main(["skill", f"--cwd={tmp_path}", "definitely-not-a-real-skill-xyz"])
        assert name not in DEFAULT_TERMINAL_CALLBACKS
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(name)


def test_handle_play_shortcut_rewrites_name_to_flow_argv():
    """play <name> [flags] is rewritten to o flow -p <name> [flags]."""
    result = _handle_play_shortcut(["play", "triage", "--x"])
    assert result == ["o", "flow", "-p", "triage", "--x"]


def test_handle_play_shortcut_resume_passthrough():
    """play --resume <id> [...] is rewritten to o flow --resume <id> [...] verbatim."""
    result = _handle_play_shortcut(["play", "--resume", "abc123", "--allow-degraded-context"])
    assert result == ["o", "flow", "--resume", "abc123", "--allow-degraded-context"]


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


# ADR-0064 D3: `li play check` pre-flight artifact contract


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


@pytest.mark.parametrize(
    ("raised", "expected_code", "expected_text"),
    [
        (ModuleNotFoundError("No module named 'thing'", name="thing"), 78, "thing"),
        (RuntimeError("profile is malformed"), 1, "could not be loaded"),
    ],
)
def test_play_check_separates_a_missing_module_from_a_bad_profile(
    tmp_path, monkeypatch, caplog, raised, expected_code, expected_text
):
    """`li play check` loads the named agent profile, and that import can fail two ways.

    A malformed profile is a real finding and the check should report it as a
    failure. A profile this installation cannot import is not a finding at all:
    nothing was checked, and returning the same code makes a broken environment
    look like a playbook with a broken profile - which sends someone to edit a
    file that is fine.
    """
    pb_path = tmp_path / "withagent.playbook.yaml"
    pb_path.write_text("name: withagent\nmodel: claude/sonnet\nprompt: do z\nagent: someprofile\n")

    from lionagi.cli import orchestrate as _orch

    def fake_resolve(name):
        return (pb_path, None) if name == "withagent" else _orch._resolve_playbook_path(name)

    def fake_load(name):
        raise raised

    monkeypatch.setattr(_orch, "_resolve_playbook_path", fake_resolve)
    monkeypatch.setattr("lionagi.cli.main._resolve_playbook_path", fake_resolve, raising=False)
    monkeypatch.setattr("lionagi.cli._providers.load_agent_profile", fake_load)

    with caplog.at_level("ERROR"):
        result = _handle_play_shortcut(["play", "check", "withagent"])

    assert result == expected_code
    assert expected_text in caplog.text


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


# `li play <name> --help` surfaces forwarded global flags


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


# package-init laziness: lionagi.cli must not eagerly import main


def test_cli_package_init_is_lazy():
    """Importing lionagi.cli (or any submodule of it, e.g. via
    lionagi.studio.cli) must not pull in lionagi.cli.main — main.py imports
    lionagi.studio.cli at module level, so an eager package init would make
    every studio->cli._logging import re-enter a partially-initialized
    module. `from lionagi.cli import main` still resolves to the callable
    via the package's lazy __getattr__ re-export."""
    import subprocess
    import sys

    code = (
        "import sys; import lionagi.cli; "
        "assert 'lionagi.cli.main' not in sys.modules, 'eager main import'; "
        "import lionagi.studio.cli; "
        "assert 'lionagi.cli.main' not in sys.modules, 'studio pulled main'; "
        "from lionagi.cli import main; assert callable(main), type(main); "
        "import types; "
        "assert not isinstance(main, types.ModuleType), type(main)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


def test_cli_package_getattr_raises_for_unknown_attr():
    """The package's __getattr__ only special-cases `main`; anything
    else must raise AttributeError like a normal missing attribute."""
    import subprocess
    import sys

    code = (
        "import lionagi.cli as pkg\n"
        "try:\n"
        "    pkg.not_a_real_attribute\n"
        "except AttributeError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('expected AttributeError for unknown attr')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr


def test_command_registry_loaders_expose_parser_and_handler():
    """Every registry entry resolves before a command-specific parse needs it."""
    from lionagi._auto import build_cli_parser, iter_cli_seeds

    for seed in iter_cli_seeds():
        build = build_cli_parser(seed)
        assert build.registration is not None
        assert callable(build.registration.cli.parser_factory)
        assert callable(build.registration.handler)


def test_version_and_root_help_work_in_fresh_cli_processes():
    for argv in (["--version"], ["--help"]):
        result = subprocess.run(
            [sys.executable, "-m", "lionagi.cli", *argv],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr


def test_leaf_command_parse_error_preserves_full_root_usage():
    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "casts", "--__registry_bogus__"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    # The root usage in the error must list the whole command set, not just
    # the selected command.
    assert "agent" in result.stderr
    assert "doctor" in result.stderr


def test_broken_command_loader_reports_error_without_traceback(capsys, monkeypatch):
    import lionagi.cli.main as main_module

    def _boom(selected):
        raise ModuleNotFoundError(
            "No module named 'missing_lionagi_command_module'",
            name="missing_lionagi_command_module",
        )

    monkeypatch.setattr(main_module, "build_cli_parser", _boom)
    rc = main_module.main(["agent", "--help"])
    # A module missing from the environment is reported as an unusable
    # environment, not as a failed run: exit 1 here is what a run that started
    # and failed returns, and a caller reading the status could not tell the two
    # apart. The concise report is unchanged — only the status distinguishes.
    assert rc == EXIT_CODE_ENVIRONMENT_ERROR
    err = capsys.readouterr().err
    assert "missing_lionagi_command_module" in err
    assert "Traceback" not in err


def test_a_command_loader_failing_for_another_reason_is_an_ordinary_failure(capsys, monkeypatch):
    """Only a missing module means the environment is unusable.

    The loader boundary splits on the exception type, so the other side of that
    split needs its own case: a command module that imports fine but raises
    while being set up is a defect in this installation's code, not a missing
    piece of it, and reporting it as an unusable environment would send the
    caller off to install something that is already there.
    """
    import lionagi.cli.main as main_module

    def _explode(selected):
        raise RuntimeError("bad module")

    monkeypatch.setattr(main_module, "build_cli_parser", _explode)
    rc = main_module.main(["agent", "--help"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "failed to load" in err
    assert "Traceback" not in err

# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li hooks import claude|codex` and `li hooks trust`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from lionagi.cli.main import main as cli_main
from lionagi.plugins._user_settings import read_user_settings

pytestmark = pytest.mark.usefixtures("plugin_home")


CLAUDE_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": ["uv", "run", "guards/check.py"], "timeout": 20}
                ],
            }
        ],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "./hooks/hygiene"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": ["notify-stop"]}]}],
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "rm -rf $HOME"}]}
        ],
    }
}


def _write_claude_settings(project_dir: Path, data: dict) -> Path:
    path = project_dir / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_import_claude_writes_hooks_external_block(capsys, tmp_path):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)

    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0

    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    assert settings_path.is_file()
    data = yaml.safe_load(settings_path.read_text())
    external = data["hooks_external"]

    assert "PreToolUse" in external
    pre_entry = external["PreToolUse"][0]["hooks"][0]
    assert pre_entry["command"] == ["uv", "run", "guards/check.py"]
    assert pre_entry["source"] == "imported:claude"
    assert pre_entry["timeout"] == 20

    # UserPromptSubmit's shell-string command with no metacharacters tokenizes fine.
    ups_entry = external["UserPromptSubmit"][0]["hooks"][0]
    assert ups_entry["command"] == ["./hooks/hygiene"]


def test_import_claude_rejects_unmappable_event(capsys, tmp_path):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)
    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "rejected [Stop]" in out
    assert "no LionAGI seam" in out

    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text())
    assert "Stop" not in data["hooks_external"]


def test_import_claude_rejects_shell_metacharacter_command(capsys, tmp_path):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)
    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "rejected [PostToolUse]" in out
    assert "shell metacharacters" in out


def test_import_missing_config_file_errors(capsys, tmp_path):
    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 1


def test_import_codex_translates_hooks_json(capsys, tmp_path):
    codex_config = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "shell", "hooks": [{"type": "command", "command": ["deny_check"]}]}
            ]
        }
    }
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(codex_config))

    code = cli_main(["hooks", "import", "codex", "--cwd", str(tmp_path)])
    assert code == 0

    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    data = yaml.safe_load(settings_path.read_text())
    entry = data["hooks_external"]["PreToolUse"][0]["hooks"][0]
    assert entry["source"] == "imported:codex"


def test_import_merges_with_existing_hooks_external_block(tmp_path):
    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {"hooks_external": {"SessionStart": [{"hooks": [{"command": ["existing"]}]}]}}
        )
    )
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)

    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0

    data = yaml.safe_load(settings_path.read_text())
    assert data["hooks_external"]["SessionStart"][0]["hooks"][0]["command"] == ["existing"]
    assert "PreToolUse" in data["hooks_external"]


def test_import_refuses_to_write_through_a_symlinked_settings_file(capsys, tmp_path):
    """A project-controlled `.lionagi/settings.yaml` that is a symlink to a
    file outside the project must never be written through -- following it
    would truncate whatever the symlink points at, using the importing
    user's own write permissions."""
    outside_target = tmp_path / "outside" / "victim.txt"
    outside_target.parent.mkdir(parents=True, exist_ok=True)
    outside_target.write_text("do-not-touch")

    project_dir = tmp_path / "project"
    (project_dir / ".lionagi").mkdir(parents=True, exist_ok=True)
    settings_path = project_dir / ".lionagi" / "settings.yaml"
    settings_path.symlink_to(outside_target)

    _write_claude_settings(project_dir, CLAUDE_SETTINGS)

    code = cli_main(["hooks", "import", "claude", "--cwd", str(project_dir)])
    assert code == 1
    err = capsys.readouterr().err
    assert "symlink" in err.lower()
    assert str(outside_target) in err

    assert outside_target.read_text() == "do-not-touch"
    assert settings_path.is_symlink()


def test_import_refuses_to_write_through_a_symlinked_lionagi_directory(capsys, tmp_path):
    """A project-controlled `.lionagi` DIRECTORY (not just the final
    settings.yaml component) that is a symlink to a directory outside the
    project must never be written through -- the final-component
    O_NOFOLLOW guard alone does not protect against a symlinked
    intermediate component, so this must be caught by the fd-anchored
    walk that opens `.lionagi` itself with O_NOFOLLOW off the project
    root."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_settings = outside_dir / "settings.yaml"
    outside_settings.write_text("do-not-touch: true\n")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".lionagi").symlink_to(outside_dir, target_is_directory=True)

    _write_claude_settings(project_dir, CLAUDE_SETTINGS)

    code = cli_main(["hooks", "import", "claude", "--cwd", str(project_dir)])
    assert code == 1
    err = capsys.readouterr().err
    assert "symlink" in err.lower()
    assert str(outside_dir.resolve()) in err

    assert outside_settings.read_text() == "do-not-touch: true\n"
    assert (project_dir / ".lionagi").is_symlink()


def test_import_non_posix_fallback_writes_correctly(capsys, tmp_path, monkeypatch):
    """Simulate a platform without `os.O_DIRECTORY`/`os.O_NOFOLLOW` (e.g.
    Windows) by forcing the module's `_POSIX_FD_WALK` flag off, rather than
    faking the os module's actual attributes -- a valid import must still
    succeed and write the expected content via the fallback path."""
    import lionagi.cli.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "_POSIX_FD_WALK", False)
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)

    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0

    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    assert settings_path.is_file()
    data = yaml.safe_load(settings_path.read_text())
    external = data["hooks_external"]

    assert "PreToolUse" in external
    pre_entry = external["PreToolUse"][0]["hooks"][0]
    assert pre_entry["command"] == ["uv", "run", "guards/check.py"]
    assert pre_entry["source"] == "imported:claude"


def test_import_non_posix_fallback_refuses_symlinked_lionagi_directory(
    capsys, tmp_path, monkeypatch
):
    """Same attack as `test_import_refuses_to_write_through_a_symlinked_lionagi_directory`,
    but on the non-POSIX fallback path (`_POSIX_FD_WALK` forced off): there is
    no fd-anchored walk available there, so the realpath containment check
    must catch a symlinked `.lionagi` directory instead."""
    import lionagi.cli.hooks as hooks_mod

    monkeypatch.setattr(hooks_mod, "_POSIX_FD_WALK", False)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_settings = outside_dir / "settings.yaml"
    outside_settings.write_text("do-not-touch: true\n")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / ".lionagi").symlink_to(outside_dir, target_is_directory=True)

    _write_claude_settings(project_dir, CLAUDE_SETTINGS)

    code = cli_main(["hooks", "import", "claude", "--cwd", str(project_dir)])
    assert code == 1
    err = capsys.readouterr().err
    assert "symlink" in err.lower()

    assert outside_settings.read_text() == "do-not-touch: true\n"
    assert (project_dir / ".lionagi").is_symlink()


# ---------------------------------------------------------------------------
# `li hooks trust`
# ---------------------------------------------------------------------------


def test_trust_lists_and_records_pending_commands(capsys, tmp_path, monkeypatch):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)
    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0
    capsys.readouterr()

    # Content pinning (D7) requires a real, resolvable executable at trust
    # time -- create the relative-path hook script CLAUDE_SETTINGS
    # references (`./hooks/hygiene`); `uv` (the other imported command) is
    # already a real PATH-resolved binary in this environment.
    hygiene = tmp_path / "hooks" / "hygiene"
    hygiene.parent.mkdir(parents=True, exist_ok=True)
    hygiene.write_text("#!/bin/sh\nexit 0\n")
    hygiene.chmod(0o755)

    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "trusted" in out

    trusted = read_user_settings().get("trusted_hook_commands", [])
    assert len(trusted) >= 2  # PreToolUse + UserPromptSubmit imported commands


def test_trust_declined_leaves_untrusted(monkeypatch, capsys, tmp_path):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)
    cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _: "n")
    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path)])
    assert code == 1
    out = capsys.readouterr().out
    assert "not trusted" in out


def test_trust_with_nothing_pending(capsys, tmp_path):
    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "no pending" in out


def test_trust_is_idempotent_on_rerun(tmp_path):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)
    cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"])
    assert code == 0

    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path)])
    assert code == 0  # nothing left pending -> no prompt needed


def test_trust_rejects_malformed_argv_instead_of_recording_it(capsys, tmp_path):
    """`li hooks trust` must not hash-record a malformed imported command
    (an empty argv list here) -- it must reject it with a clear message,
    matching the argv validation the config loader enforces at execution
    time (ADR-0048 D4)."""
    from lionagi.hooks.external import compute_command_hash

    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "hooks_external": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {"command": [], "source": "imported:claude"},
                            ]
                        }
                    ]
                }
            }
        )
    )

    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "rejected" in out
    assert "no pending" in out

    trusted = read_user_settings().get("trusted_hook_commands", [])
    assert compute_command_hash([]) not in trusted


@pytest.mark.parametrize(
    "bad_command",
    [
        [],  # empty argv
        ["guard", "   "],  # blank entry
        ["guard", 1],  # non-string entry
    ],
    ids=["empty", "blank", "non-string"],
)
def test_trust_rejects_various_malformed_argv_shapes(capsys, tmp_path, bad_command):
    from lionagi.hooks.external import compute_command_hash

    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "hooks_external": {
                    "PreToolUse": [
                        {"hooks": [{"command": bad_command, "source": "imported:claude"}]}
                    ]
                }
            }
        )
    )

    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"])
    assert code == 0
    assert "rejected" in capsys.readouterr().out

    trusted = read_user_settings().get("trusted_hook_commands", [])
    assert compute_command_hash(bad_command) not in trusted


# ---------------------------------------------------------------------------
# Content-pinned trust records (Issue 3 fix), exercised through the CLI.
# ---------------------------------------------------------------------------


def test_trust_rejects_unresolvable_executable_instead_of_recording_it(capsys, tmp_path):
    """A syntactically valid command whose executable does not exist right
    now must be rejected, not silently recorded as pending -- content
    pinning (D7) cannot pin what it cannot resolve."""
    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "hooks_external": {
                    "PreToolUse": [
                        {"hooks": [{"command": ["./does-not-exist"], "source": "imported:claude"}]}
                    ]
                }
            }
        )
    )

    code = cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"])
    assert code == 0
    out = capsys.readouterr().out
    assert "rejected" in out
    assert "no pending" in out
    assert read_user_settings().get("trusted_hook_commands", []) == []


def test_trust_records_are_content_pinned_not_bare_argv_hashes(tmp_path):
    """The record `li hooks trust` writes carries argv_hash, resolved_path,
    AND content_digest -- not the pre-fix bare argv-hash string."""
    script = tmp_path / "guard"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "hooks_external": {
                    "PreToolUse": [
                        {"hooks": [{"command": ["./guard"], "source": "imported:claude"}]}
                    ]
                }
            }
        )
    )

    assert cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"]) == 0

    trusted = read_user_settings().get("trusted_hook_commands", [])
    assert len(trusted) == 1
    record = trusted[0]
    assert set(record) == {"argv_hash", "resolved_path", "content_digest"}
    assert record["resolved_path"] == str(script.resolve())


def test_trust_re_enters_pending_after_approved_executable_content_changes(tmp_path):
    """Matches the ADR-0048 D7 rule ('a changed argv changes the hash and
    re-enters pending state') extended to content: an unchanged argv whose
    resolved executable's BYTES change must also re-enter pending, not
    silently keep running under the stale approval."""
    script = tmp_path / "guard"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    settings_path = tmp_path / ".lionagi" / "settings.yaml"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        yaml.safe_dump(
            {
                "hooks_external": {
                    "PreToolUse": [
                        {"hooks": [{"command": ["./guard"], "source": "imported:claude"}]}
                    ]
                }
            }
        )
    )
    assert cli_main(["hooks", "trust", "--cwd", str(tmp_path), "--yes"]) == 0
    assert len(read_user_settings().get("trusted_hook_commands", [])) == 1

    # Swap the approved script's contents without changing argv.
    script.write_text("#!/bin/sh\necho different\nexit 0\n")
    script.chmod(0o755)

    from lionagi.cli.hooks import _iter_untrusted_commands
    from lionagi.hooks.external import is_command_trusted

    assert is_command_trusted(["./guard"], source="imported:claude", cwd=str(tmp_path)) is False
    pending, _ = _iter_untrusted_commands(tmp_path)
    assert len(pending) == 1

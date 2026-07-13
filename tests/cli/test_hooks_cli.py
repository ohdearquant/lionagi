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


# ---------------------------------------------------------------------------
# `li hooks trust`
# ---------------------------------------------------------------------------


def test_trust_lists_and_records_pending_commands(capsys, tmp_path, monkeypatch):
    _write_claude_settings(tmp_path, CLAUDE_SETTINGS)
    code = cli_main(["hooks", "import", "claude", "--cwd", str(tmp_path)])
    assert code == 0
    capsys.readouterr()

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

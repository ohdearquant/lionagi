# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li lionagi-dir trust|untrust|list`, and the trust steps that refuse inside a gated run."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import lionhub.serve
from lionagi._lionagi_dir_trust import GATE_ENV_VAR, RECORD_KEY, UntrustedLionagiDirError
from lionagi.plugins._user_settings import read_user_settings
from lionhub.serve.cli import lionagi_dir as lionagi_dir_cli
from lionhub.serve.cli.main import main as cli_main

pytestmark = pytest.mark.usefixtures("plugin_home")


@pytest.fixture
def project(plugin_home: Path) -> Path:
    root = plugin_home / "project"
    lionagi_dir = root / ".lionagi"
    (lionagi_dir / "agents").mkdir(parents=True)
    (lionagi_dir / "agents" / "builder.md").write_text(
        "---\nmodel: claude_code/sonnet\nbypass: true\n---\nBuild it.\n"
    )
    (lionagi_dir / "playbooks").mkdir()
    (lionagi_dir / "playbooks" / "ship.playbook.yaml").write_text("name: ship\nprompt: ship it\n")
    (lionagi_dir / "settings.yaml").write_text(
        "notify:\n  on_terminal:\n    adapter:\n      kind: exec\n      argv: [echo, done]\n"
        "model: claude_code/sonnet\n"
    )
    return root


def _record():
    return read_user_settings().get(RECORD_KEY)


def _no_prompt(prompt: str) -> str:
    raise AssertionError(f"asked {prompt!r}")


def test_trust_lists_what_the_directory_supplies_then_records_its_resolved_path(
    capsys, monkeypatch, project
):
    monkeypatch.setattr("builtins.input", _no_prompt)
    assert cli_main(["lionagi-dir", "trust", str(project), "--yes"]) == 0
    out = capsys.readouterr().out
    target = os.path.realpath(project / ".lionagi")
    assert f"Directory: {target}" in out
    assert "agent    builder" in out
    assert "bypass=True" in out and "yolo=" in out and "deny_read=" in out
    assert "playbook ship" in out
    assert "settings notify: " in out
    assert "settings other keys: model" in out
    assert f"trusted: {target}" in out
    assert _record() == [target]


def test_trust_defaults_to_the_working_directory_and_is_idempotent(capsys, monkeypatch, project):
    monkeypatch.chdir(project)
    assert cli_main(["lionagi-dir", "trust", "--yes"]) == 0
    assert cli_main(["lionagi-dir", "trust", "--yes"]) == 0
    assert "already trusted" in capsys.readouterr().out
    assert _record() == [os.path.realpath(project / ".lionagi")]


def test_the_prompt_path_asks_and_a_no_records_nothing(capsys, monkeypatch, project):
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    assert cli_main(["lionagi-dir", "trust", str(project)]) == 1
    assert asked == ["\nTrust this directory? [y/N] "]
    assert "not trusted." in capsys.readouterr().out
    assert _record() is None

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli_main(["lionagi-dir", "trust", str(project)]) == 0
    assert _record() == [os.path.realpath(project / ".lionagi")]


def test_trust_refuses_a_directory_with_no_lionagi(capsys, plugin_home):
    empty = plugin_home / "empty"
    empty.mkdir()
    assert cli_main(["lionagi-dir", "trust", str(empty), "--yes"]) == 1
    captured = capsys.readouterr()
    assert "there is no .lionagi directory" in captured.out + captured.err
    assert _record() is None


def test_trust_refuses_the_user_level_directory(capsys, plugin_home):
    (plugin_home / ".lionagi").mkdir()
    assert cli_main(["lionagi-dir", "trust", str(plugin_home), "--yes"]) == 1
    captured = capsys.readouterr()
    assert "is the user-level .lionagi directory" in captured.out + captured.err
    assert _record() is None


def test_trust_refuses_when_it_cannot_list_what_the_directory_supplies(capsys, project):
    (project / ".lionagi" / "settings.yaml").write_text("notify: [\n")
    assert cli_main(["lionagi-dir", "trust", str(project), "--yes"]) == 1
    captured = capsys.readouterr()
    assert "so it is not trusted" in captured.out + captured.err
    assert _record() is None


def test_untrust_removes_the_record(capsys, project):
    assert cli_main(["lionagi-dir", "trust", str(project), "--yes"]) == 0
    assert cli_main(["lionagi-dir", "untrust", str(project)]) == 0
    assert _record() == []
    assert cli_main(["lionagi-dir", "untrust", str(project)]) == 0
    assert "was not trusted" in capsys.readouterr().out


def test_list_prints_each_record_and_marks_a_missing_directory(capsys, project, plugin_home):
    assert cli_main(["lionagi-dir", "list"]) == 0
    assert "(no trusted .lionagi directories)" in capsys.readouterr().out
    assert cli_main(["lionagi-dir", "trust", str(project), "--yes"]) == 0
    capsys.readouterr()
    target = os.path.realpath(project / ".lionagi")
    project.rename(plugin_home / "moved")
    assert cli_main(["lionagi-dir", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{target} (missing)"]


@pytest.mark.parametrize(
    ("step", "argv"),
    (
        ("lionagi-dir trust", ["lionagi-dir", "trust", "{project}", "--yes"]),
        ("hooks trust", ["hooks", "trust", "--cwd", "{project}", "--yes"]),
        ("plugin trust", ["plugin", "trust", "web-research", "--yes"]),
    ),
)
def test_each_trust_step_refuses_inside_a_gated_run_and_writes_nothing(
    capsys, monkeypatch, project, plugin_home, step, argv
):
    # No project settings file: with one, the gated start of `li` itself refuses the untrusted
    # settings layer before any step runs, and this test is about the step's own refusal.
    (project / ".lionagi" / "settings.yaml").unlink()
    monkeypatch.setenv(GATE_ENV_VAR, "1")
    monkeypatch.setattr("builtins.input", _no_prompt)
    user_settings = plugin_home / ".lionagi" / "settings.yaml"
    assert cli_main([a.replace("{project}", str(project)) for a in argv]) == 1
    captured = capsys.readouterr()
    assert (
        f"li {step} refuses to run inside a run the MCP server started "
        "(LIONAGI_DIR_TRUST_GATE is set); run it from your own shell."
    ) in captured.out + captured.err
    assert not user_settings.exists()


def test_a_refusal_raised_under_a_command_exits_one_with_its_text(capsys, monkeypatch, project):
    lionagi_dir = project / ".lionagi"
    refusal = UntrustedLionagiDirError(
        "agent profile", lionagi_dir / "agents" / "x.md", lionagi_dir
    )

    def refuse() -> int:
        raise refusal

    monkeypatch.setattr(lionagi_dir_cli, "_run_list", refuse)
    assert cli_main(["lionagi-dir", "list"]) == 1
    captured = capsys.readouterr()
    assert str(refusal) in captured.out + captured.err


# Only a step a person runs writes the record; nothing the MCP server runs does.

_WRITER_MARKS = (
    "RECORD_KEY",
    "trusted_lionagi_dirs",
    "locked_user_settings",
    "write_user_settings",
)


def _writes_the_record(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return any(mark in text for mark in _WRITER_MARKS)


def test_no_module_under_serve_mcp_writes_the_record():
    serve = Path(lionhub.serve.__file__).parent
    # The known writer, through the same predicate, in the same run.
    assert _writes_the_record(serve / "cli" / "lionagi_dir.py")
    mcp_modules = sorted((serve / "mcp").rglob("*.py"))
    assert len(mcp_modules) > 10, mcp_modules
    assert [str(p) for p in mcp_modules if _writes_the_record(p)] == []

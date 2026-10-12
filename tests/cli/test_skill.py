# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for `li skill` — CC-compatible skill reader."""

from lionagi.cli.main import main as cli_main
from lionagi.cli.skill import (
    list_skill_names,
    read_skill_body,
    resolve_skill_path,
    strip_frontmatter,
)


class TestStripFrontmatter:
    def test_strips_complete_frontmatter(self):
        text = "---\nname: x\ndescription: y\n---\n\n# Body\n\ncontent\n"
        assert strip_frontmatter(text) == "# Body\n\ncontent\n"

    def test_no_frontmatter_passthrough(self):
        text = "# Just a markdown file\n\nNo frontmatter.\n"
        assert strip_frontmatter(text) == text

    def test_unterminated_frontmatter_passthrough(self):
        text = "---\nname: x\n(no closing delimiter)\n"
        assert strip_frontmatter(text) == text

    def test_empty_frontmatter(self):
        text = "---\n---\nbody\n"
        assert strip_frontmatter(text) == "body\n"

    def test_empty_input(self):
        assert strip_frontmatter("") == ""


class TestResolveSkillPath:
    def test_rejects_path_separator(self):
        p, err = resolve_skill_path("a/b")
        assert p is None
        assert "bare identifier" in err

    def test_rejects_hidden_name(self):
        p, err = resolve_skill_path(".hidden")
        assert p is None
        assert "bare identifier" in err

    def test_not_found_suggests_available(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".lionagi" / "skills"
        (skills_dir / "alpha").mkdir(parents=True)
        (skills_dir / "alpha" / "SKILL.md").write_text("body\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        p, err = resolve_skill_path("missing")
        assert p is None
        assert "not found" in err
        assert "alpha" in err

    def test_resolves_existing_skill(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".lionagi" / "skills"
        target_dir = skills_dir / "empaco"
        target_dir.mkdir(parents=True)
        target = target_dir / "SKILL.md"
        target.write_text("---\nname: empaco\n---\nbody\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        p, err = resolve_skill_path("empaco")
        assert err is None
        assert str(p) == str(target)

    def test_allows_a_skill_file_link_within_a_non_repository_root(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".lionagi" / "skills"
        target = skills_dir / "shared" / "body.md"
        target.parent.mkdir(parents=True)
        target.write_text("body\n")
        linked = skills_dir / "foo"
        linked.mkdir()
        (linked / "SKILL.md").symlink_to("../shared/body.md")
        monkeypatch.setenv("HOME", str(tmp_path))

        path, err = resolve_skill_path("foo")

        assert err is None
        assert path == linked / "SKILL.md"


class TestListSkillNames:
    def test_empty_when_no_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert list_skill_names() == []

    def test_lists_only_dirs_with_skill_md(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".lionagi" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "alpha").mkdir()
        (skills_dir / "alpha" / "SKILL.md").write_text("ok\n")
        (skills_dir / "beta").mkdir()
        (skills_dir / "beta" / "SKILL.md").write_text("ok\n")
        (skills_dir / "incomplete").mkdir()  # no SKILL.md
        (skills_dir / "stray_file.md").write_text("stray\n")  # file, not dir
        monkeypatch.setenv("HOME", str(tmp_path))
        assert list_skill_names() == ["alpha", "beta"]


class TestReadSkillBody:
    def test_returns_body_only(self, monkeypatch, tmp_path):
        skills_dir = tmp_path / ".lionagi" / "skills" / "x"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: x\ndescription: y\n---\n\n# Body\n\ncontent\n"
        )
        monkeypatch.setenv("HOME", str(tmp_path))
        body, err = read_skill_body("x")
        assert err is None
        assert body == "# Body\n\ncontent\n"

    def test_missing_skill_returns_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        body, err = read_skill_body("nope")
        assert body is None
        assert "not found" in err


class TestLiSkillCLI:
    def test_skill_name_prints_body(self, monkeypatch, tmp_path, capsys):
        skills_dir = tmp_path / ".lionagi" / "skills" / "hello"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: hello\ndescription: greet\n---\n# Greeting\n\nHi.\n"
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        code = cli_main(["skill", "hello"])
        assert code == 0
        out = capsys.readouterr().out
        assert out == "# Greeting\n\nHi.\n"

    def test_skill_list(self, monkeypatch, tmp_path, capsys):
        for name in ("alpha", "beta"):
            d = tmp_path / ".lionagi" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("x\n")
        monkeypatch.setenv("HOME", str(tmp_path))

        code = cli_main(["skill", "list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_skill_show_prints_full_file(self, monkeypatch, tmp_path, capsys):
        d = tmp_path / ".lionagi" / "skills" / "full"
        d.mkdir(parents=True)
        content = "---\nname: full\n---\nbody\n"
        (d / "SKILL.md").write_text(content)
        monkeypatch.setenv("HOME", str(tmp_path))

        code = cli_main(["skill", "show", "full"])
        assert code == 0
        assert capsys.readouterr().out == content

    def test_skill_no_args_usage(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        code = cli_main(["skill"])
        assert code == 1
        assert "Usage" in capsys.readouterr().out

    def test_skill_flag_before_name_errors(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        code = cli_main(["skill", "--bad"])
        assert code == 1

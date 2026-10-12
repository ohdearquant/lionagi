# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for symlink containment, HookRegistry aliases, and related CLI behaviors."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from lionagi.cli._logging import _LazyStderrHandler
from lionagi.cli._providers import _clamp_claude_effort
from lionagi.cli.main import _handle_play_shortcut
from lionagi.cli.orchestrate import _resolve_playbook_path, _validate_spec_fields
from lionagi.cli.skill import _containment_anchor, _first_line, resolve_skill_path
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_registry import HookRegistry

# Symlink containment


def _make_repository(root: Path) -> None:
    """Lay down the shape git requires of a repository directory."""
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")


def _real_repository_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real repository and a real linked worktree of it, made by git itself:
    the on-disk shape of a linked worktree is git's to define, not this test's."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@e.st", "-c", "user.name=t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    git("-c", "init.defaultBranch=main", "init", "-q")
    git("commit", "-qm", "seed", "--allow-empty")
    worktree = tmp_path / "wt"
    git("worktree", "add", "-q", "-b", "wt", str(worktree))
    return repo, worktree


class TestSkillSymlinkContainment:
    def test_rejects_skill_md_symlink_pointing_outside_root(self, monkeypatch, tmp_path: Path):
        """A SKILL.md inside the skills root that is itself a symlink to
        an arbitrary file must be rejected before read_text() follows it.
        """
        secret = tmp_path / "secret.txt"
        secret.write_text("DO NOT LEAK")

        home = tmp_path / "home"
        skills = home / ".lionagi" / "skills" / "leak"
        skills.mkdir(parents=True)
        # Symlink SKILL.md -> secret.txt, simulating the exploit vector.
        (skills / "SKILL.md").symlink_to(secret)

        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("leak")
        assert path is None
        assert err is not None
        assert "symlink escape" in err or "outside" in err

    def test_accepts_legitimate_skill(self, monkeypatch, tmp_path: Path):
        home = tmp_path / "home"
        skills = home / ".lionagi" / "skills" / "ok"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("---\nname: ok\ndescription: legit\n---\nbody")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("ok")
        assert err is None
        assert path is not None
        assert path.read_text().startswith("---")

    def test_accepts_skill_symlinked_within_the_root_repository(self, monkeypatch, tmp_path: Path):
        """A skills root inside a repository may hold skill directories that are
        symlinks to another tree of the same repository (a marketplace layout).
        """
        repo = tmp_path / "repo"
        _make_repository(repo)
        real = repo / "marketplace" / "plugin" / "skills" / "ok"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: ok\n---\nbody")
        (repo / "resources" / "skills").mkdir(parents=True)
        (repo / "resources" / "skills" / "ok").symlink_to(real)
        home = tmp_path / "home"
        (home / ".lionagi").mkdir(parents=True)
        (home / ".lionagi" / "skills").symlink_to(repo / "resources" / "skills")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("ok")
        assert err is None
        assert path is not None
        assert path.read_text().startswith("---")

    def test_rejects_skill_symlinked_to_another_repository(self, monkeypatch, tmp_path: Path):
        repo = tmp_path / "repo"
        _make_repository(repo)
        (repo / "resources" / "skills").mkdir(parents=True)
        other = tmp_path / "other"
        _make_repository(other)
        (other / "leak").mkdir()
        (other / "leak" / "SKILL.md").write_text("---\nname: leak\n---\nbody")
        (repo / "resources" / "skills" / "leak").symlink_to(other / "leak")
        home = tmp_path / "home"
        (home / ".lionagi").mkdir(parents=True)
        (home / ".lionagi" / "skills").symlink_to(repo / "resources" / "skills")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("leak")
        assert path is None
        assert err is not None and "symlink escape" in err

    def test_rejects_non_skill_file_inside_the_root_repository(self, monkeypatch, tmp_path: Path):
        """Widening the bound to the repository must not admit a SKILL.md that
        is a symlink to some other file of that repository.
        """
        repo = tmp_path / "repo"
        _make_repository(repo)
        (repo / "secret.txt").write_text("DO NOT LEAK")
        (repo / "resources" / "skills" / "leak").mkdir(parents=True)
        (repo / "resources" / "skills" / "leak" / "SKILL.md").symlink_to(repo / "secret.txt")
        home = tmp_path / "home"
        (home / ".lionagi").mkdir(parents=True)
        (home / ".lionagi" / "skills").symlink_to(repo / "resources" / "skills")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("leak")
        assert path is None
        assert err is not None and "symlink escape" in err

    @pytest.mark.parametrize(
        "marker",
        ["empty_dir", "head_only", "file_without_gitdir", "gitfile_to_nothing"],
    )
    def test_malformed_git_entry_above_the_root_does_not_widen_the_bound(
        self, monkeypatch, tmp_path: Path, marker: str
    ):
        """A ``.git`` entry that git itself would not read as a repository must
        not become the containment anchor, whatever shape it takes.
        """
        git = tmp_path / ".git"
        if marker == "empty_dir":
            git.mkdir()
        elif marker == "head_only":
            git.mkdir()
            (git / "HEAD").write_text("ref: refs/heads/main\n")
        elif marker == "file_without_gitdir":
            git.write_text("not a gitfile\n")
        elif marker == "gitfile_to_nothing":
            git.write_text(f"gitdir: {tmp_path / 'nowhere'}\n")
        else:
            raise AssertionError(f"unhandled marker {marker!r}")
        private = tmp_path / "private"
        private.mkdir()
        (private / "SKILL.md").write_text("---\nname: leak\n---\nbody")
        home = tmp_path / "home"
        skills = home / ".lionagi" / "skills"
        (skills / "leak").mkdir(parents=True)
        (skills / "leak" / "SKILL.md").symlink_to(private / "SKILL.md")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("leak")
        assert path is None
        assert err is not None and "symlink escape" in err

    def test_accepts_root_symlink(self, monkeypatch, tmp_path: Path):
        """The skills *root* itself may be a symlink (users point it at
        any directory they manage); the resolve check must accept that.
        """
        real_skills = tmp_path / "real" / "skills"
        (real_skills / "ok").mkdir(parents=True)
        (real_skills / "ok" / "SKILL.md").write_text("---\nname: ok\n---\nbody")
        home = tmp_path / "home"
        (home / ".lionagi").mkdir(parents=True)
        (home / ".lionagi" / "skills").symlink_to(real_skills)
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("ok")
        assert err is None
        assert path is not None

    def test_linked_worktree_root_is_the_containment_anchor(self, tmp_path: Path):
        """A linked worktree's private gitdir keeps its objects in the common
        directory, so the worktree root is still the repository that bounds
        containment. An exact anchor assert catches both failure directions:
        collapsing to the skills root, and widening to some ancestor.
        """
        _, worktree = _real_repository_with_worktree(tmp_path)
        skills = worktree / "resources" / "skills"
        skills.mkdir(parents=True)

        assert _containment_anchor(skills.resolve()) == worktree.resolve()

    def test_accepts_skill_symlinked_within_a_linked_worktree(self, monkeypatch, tmp_path: Path):
        _, worktree = _real_repository_with_worktree(tmp_path)
        real = worktree / "marketplace" / "plugin" / "skills" / "ok"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: ok\n---\nbody")
        (worktree / "resources" / "skills").mkdir(parents=True)
        (worktree / "resources" / "skills" / "ok").symlink_to(real)
        home = tmp_path / "home"
        (home / ".lionagi").mkdir(parents=True)
        (home / ".lionagi" / "skills").symlink_to(worktree / "resources" / "skills")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("ok")
        assert err is None
        assert path is not None

    def test_a_linked_worktree_without_private_refs_is_still_the_anchor(self, tmp_path: Path):
        """Older git writes a linked worktree's private gitdir with HEAD and commondir but no
        refs/; git reads refs/ and objects/ from the common directory, and so must the anchor."""
        repo, worktree = _real_repository_with_worktree(tmp_path)
        private = repo / ".git" / "worktrees" / "wt"
        if (private / "refs").exists():
            shutil.rmtree(private / "refs")
        assert not (private / "refs").exists() and not (private / "objects").exists()
        skills = worktree / "resources" / "skills"
        skills.mkdir(parents=True)

        assert _containment_anchor(skills.resolve()) == worktree.resolve()

    def test_rejects_skill_symlinked_out_of_a_linked_worktree(self, monkeypatch, tmp_path: Path):
        """The anchor is the worktree, not an ancestor of it: a skill pointing
        into the main checkout beside it is still an escape.
        """
        repo, worktree = _real_repository_with_worktree(tmp_path)
        (repo / "leak").mkdir()
        (repo / "leak" / "SKILL.md").write_text("---\nname: leak\n---\nbody")
        (worktree / "resources" / "skills").mkdir(parents=True)
        (worktree / "resources" / "skills" / "leak").symlink_to(repo / "leak")
        home = tmp_path / "home"
        (home / ".lionagi").mkdir(parents=True)
        (home / ".lionagi" / "skills").symlink_to(worktree / "resources" / "skills")
        monkeypatch.setenv("HOME", str(home))

        path, err = resolve_skill_path("leak")
        assert path is None
        assert err is not None and "symlink escape" in err


class TestPointerReads:
    """A pointer path holds a short line of text. Anything else there is not a pointer, and reading
    it must not cost the caller a wait."""

    @staticmethod
    def _read_within(path: Path, seconds: float = 5.0) -> str | None:
        """`_first_line(path)` on a daemon thread: a blocking read fails this as a timeout instead
        of hanging the suite, which is the whole property under test."""
        answer: list[str] = []
        worker = threading.Thread(target=lambda: answer.append(_first_line(path)), daemon=True)
        worker.start()
        worker.join(seconds)
        return None if worker.is_alive() else answer[0]

    @staticmethod
    def _descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    @pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="no /dev/fd to count descriptors in")
    def test_the_refused_shapes_leave_no_open_descriptor(self, tmp_path: Path):
        """The open precedes the shape test, so every refusal owns a descriptor it must close.
        Counted over many rounds: one leaked descriptor per call shows as a rising count, while a
        single sample would be lost in whatever else the process holds open."""
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        directory = tmp_path / "dir"
        directory.mkdir()
        device = tmp_path / "dev"
        device.symlink_to("/dev/zero")
        regular = tmp_path / "commondir"
        regular.write_text("../.git\n")
        shapes = (fifo, directory, device, regular)

        for path in shapes:  # first call pays for any lazy import; count after that
            _first_line(path)
        before = self._descriptors()
        for _ in range(50):
            for path in shapes:
                _first_line(path)
        assert self._descriptors() == before

    def test_a_regular_pointer_still_reads(self, tmp_path: Path):
        """The control: without it a function that always returned "" would pass every case below."""
        pointer = tmp_path / "commondir"
        pointer.write_text("../.git\nignored second line\n")
        assert self._read_within(pointer) == "../.git"

    def test_a_fifo_at_a_pointer_path_returns_empty_rather_than_blocking(self, tmp_path: Path):
        """A FIFO with no writer blocks a plain open until one appears, which would stop a skill
        lookup for as long as the attacker likes."""
        pointer = tmp_path / "commondir"
        os.mkfifo(pointer)
        assert self._read_within(pointer) == ""

    @pytest.mark.skipif(not Path("/dev/zero").exists(), reason="no character device to point at")
    def test_a_character_device_at_a_pointer_path_returns_empty(self, tmp_path: Path):
        """The arm that makes the regular-file test load-bearing rather than decorative. A device
        opens and reads without blocking, so the non-blocking open alone would hand back 4096 NUL
        bytes as a pointer line; only the shape test refuses it."""
        pointer = tmp_path / "commondir"
        pointer.symlink_to("/dev/zero")
        assert self._read_within(pointer) == ""

    def test_a_directory_at_a_pointer_path_returns_empty(self, tmp_path: Path):
        pointer = tmp_path / "commondir"
        pointer.mkdir()
        assert self._read_within(pointer) == ""

    def test_a_repository_whose_commondir_is_a_fifo_is_not_an_anchor(self, tmp_path: Path):
        """The reachable path: `_is_git_dir` consults `commondir` only for a gitdir without its
        own `objects/` and `refs/`, which is exactly a linked worktree's shape."""
        git = tmp_path / "anchor" / ".git"
        (git / "refs").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        os.mkfifo(git / "commondir")
        skills = tmp_path / "anchor" / "skills"
        skills.mkdir()

        answer: list[Path] = []
        worker = threading.Thread(
            target=lambda: answer.append(_containment_anchor(skills)), daemon=True
        )
        worker.start()
        worker.join(5.0)
        assert not worker.is_alive(), "resolving an anchor blocked on a FIFO commondir"
        assert answer[0] == skills  # no repository found, so the root bounds itself


class TestPlaybookSymlinkContainment:
    def test_rejects_playbook_symlink_pointing_outside_root(self, monkeypatch, tmp_path: Path):
        secret = tmp_path / "secret.yaml"
        secret.write_text("evil: true\n")

        home = tmp_path / "home"
        playbooks = home / ".lionagi" / "playbooks"
        playbooks.mkdir(parents=True)
        (playbooks / "leak.playbook.yaml").symlink_to(secret)

        monkeypatch.setenv("HOME", str(home))

        path, err = _resolve_playbook_path("leak")
        assert path is None
        assert err is not None
        assert "symlink escape" in err or "outside" in err


# HookRegistry alias both spellings


class TestHookRegistryAliases:
    def test_pre_event_create_accepted(self):
        def hook(*a, **kw):
            pass

        reg = HookRegistry(hooks={"pre_event_create": hook})
        assert HookEventTypes.PreEventCreate in reg._hooks

    def test_pre_event_create_hook_accepted(self):
        """Legacy alias — constructor decorator method is called
        `pre_event_create_hook`, so callers often use this spelling.
        """

        def hook(*a, **kw):
            pass

        reg = HookRegistry(hooks={"pre_event_create_hook": hook})
        assert HookEventTypes.PreEventCreate in reg._hooks


# max_ops/max_agents zero remains valid


class TestMaxOpsZeroAccepted:
    def test_max_ops_zero_accepted(self):
        assert _validate_spec_fields({"max_ops": 0}) is None

    def test_max_agents_zero_accepted(self):
        assert _validate_spec_fields({"max_agents": 0}) is None

    def test_negative_still_rejected(self):
        err = _validate_spec_fields({"max_ops": -1})
        assert err is not None

    def test_too_large_still_rejected(self):
        err = _validate_spec_fields({"max_ops": 51})
        assert err is not None


# _clamp_claude_effort coverage


class TestClampClaudeEffort:
    def test_xhigh_preserved_on_opus_4_7(self):
        assert _clamp_claude_effort("xhigh", "claude/claude-opus-4-7") == "xhigh"

    def test_xhigh_preserved_on_bare_opus_4_7(self):
        assert _clamp_claude_effort("xhigh", "opus-4-7") == "xhigh"

    def test_xhigh_preserved_on_bare_opus(self):
        assert _clamp_claude_effort("xhigh", "opus") == "xhigh"

    def test_xhigh_clamped_on_sonnet(self):
        assert _clamp_claude_effort("xhigh", "claude/claude-sonnet-4-6") == "high"

    def test_xhigh_clamped_on_haiku(self):
        assert _clamp_claude_effort("xhigh", "claude/claude-haiku-4-5") == "high"

    def test_non_xhigh_untouched_on_any_model(self):
        for effort in ("none", "minimal", "low", "medium", "high", "max"):
            assert _clamp_claude_effort(effort, "claude/claude-sonnet-4-6") == effort
            assert _clamp_claude_effort(effort, "claude/claude-opus-4-7") == effort


# _handle_play_shortcut coverage


class TestHandlePlayShortcut:
    def test_empty_argv_returns_unchanged(self):
        assert _handle_play_shortcut([]) == []

    def test_non_play_passthrough(self):
        argv = ["agent", "claude/sonnet", "hi"]
        assert _handle_play_shortcut(argv) == argv

    def test_play_no_args_prints_usage(self, capsys):
        code = _handle_play_shortcut(["play"])
        assert code == 1
        out = capsys.readouterr().out
        assert "Usage" in out

    def test_play_rewrite(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        rewritten = _handle_play_shortcut(["play", "rewrite", "--tabs", "5", "query text"])
        assert rewritten == [
            "o",
            "flow",
            "-p",
            "rewrite",
            "--tabs",
            "5",
            "query text",
        ]

    def test_play_list_empty_dir(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        # find_lionagi_dirs() also walks up from cwd for a project-local
        # `.lionagi/`; chdir into the scratch dir so this test's "empty"
        # expectation isn't defeated by a real one reachable from cwd.
        monkeypatch.chdir(tmp_path)
        code = _handle_play_shortcut(["play", "list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "no playbooks" in out.lower()

    def test_play_list_with_playbooks(self, monkeypatch, tmp_path, capsys):
        pb = tmp_path / ".lionagi" / "playbooks"
        pb.mkdir(parents=True)
        (pb / "alpha.playbook.yaml").write_text("prompt: a\n")
        (pb / "beta.playbook.yaml").write_text("prompt: b\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        code = _handle_play_shortcut(["play", "list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_play_flag_before_name_errors(self):
        code = _handle_play_shortcut(["play", "--bogus", "foo"])
        assert code == 1


# _LazyStderrHandler re-binds stream


class TestLazyStderrHandler:
    def test_emit_uses_current_stderr(self, capsys, monkeypatch):
        import logging

        handler = _LazyStderrHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))

        logger = logging.getLogger("lionagi.cli._logging_test")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        logger.info("first-message")
        err1 = capsys.readouterr().err
        assert "first-message" in err1

        logger.info("second-message")
        err2 = capsys.readouterr().err
        assert "second-message" in err2

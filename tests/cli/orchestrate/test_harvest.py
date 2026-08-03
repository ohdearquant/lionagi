# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The leg artifact harvester treats its input as hostile, because it is: the
scratch tree is written by a leg running the caller's brief, and the harvester
runs unsandboxed inside the run directory.

Every escape test asserts the OUTCOME on both sides — the escape did not
happen AND the legitimate artifact beside it was still harvested. A harvester
that refuses everything also passes "the attack had no effect".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lionagi.cli.orchestrate._harvest import (
    SKIP_HARDLINK,
    SKIP_SPECIAL,
    SKIP_SWAPPED,
    SKIP_SYMLINK,
    SKIP_UNREADABLE,
    STATE_ABSENT,
    STATE_EMPTY,
    STATE_FAILED,
    harvest_leg_artifacts,
)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    return tmp_path / "run" / "artifacts" / "review-a"


def _skips(result) -> dict[str, str]:
    return {path: reason for path, reason in result.skipped}


class TestOrdinaryHarvest:
    def test_copies_regular_files_and_reports_what_it_took(self, scratch, dest):
        (scratch / "verdict.md").write_text("APPROVE\n")
        (scratch / "notes.md").write_text("some notes\n")

        result = harvest_leg_artifacts(scratch, dest)

        assert result.state == "harvested-2"
        assert result.files == 2
        assert result.bytes == len("APPROVE\n") + len("some notes\n")
        assert result.artifacts == ("notes.md", "verdict.md")
        assert (dest / "verdict.md").read_text() == "APPROVE\n"
        assert not result.failed

    def test_preserves_relative_paths_under_the_destination(self, scratch, dest):
        (scratch / "sub" / "deeper").mkdir(parents=True)
        (scratch / "sub" / "deeper" / "log.txt").write_text("x")

        result = harvest_leg_artifacts(scratch, dest)

        assert result.artifacts == ("sub/deeper/log.txt",)
        assert (dest / "sub" / "deeper" / "log.txt").read_text() == "x"

    def test_an_empty_scratch_is_not_a_failure(self, scratch, dest):
        result = harvest_leg_artifacts(scratch, dest)

        assert result.state == STATE_EMPTY
        assert not result.failed

    def test_an_absent_scratch_is_not_a_failure(self, tmp_path, dest):
        result = harvest_leg_artifacts(tmp_path / "never-created", dest)

        assert result.state == STATE_ABSENT
        assert not result.failed

    def test_a_scratch_path_that_is_a_file_is_a_failure_not_an_empty_read(self, tmp_path, dest):
        """`dir-empty` says the leg wrote nothing. A leg that replaced its own
        channel with a file wrote something the harvester cannot serve, and
        the two must not read the same."""
        impostor = tmp_path / "scratch-as-file"
        impostor.write_text("not a directory")

        result = harvest_leg_artifacts(impostor, dest)

        assert result.state == STATE_FAILED
        assert "not a directory" in (result.detail or "")

    def test_artifact_order_is_stable_across_harvests(self, scratch, tmp_path):
        for name in ("c.md", "a.md", "b.md"):
            (scratch / name).write_text(name)

        first = harvest_leg_artifacts(scratch, tmp_path / "one")
        second = harvest_leg_artifacts(scratch, tmp_path / "two")

        assert first.artifacts == second.artifacts == ("a.md", "b.md", "c.md")


class TestSymlinkEscape:
    def test_a_symlink_to_a_file_outside_the_root_is_refused_and_not_copied(
        self, scratch, dest, tmp_path
    ):
        secret = tmp_path / "outside.txt"
        secret.write_text("PRIVATE")
        (scratch / "escape.md").symlink_to(secret)
        (scratch / "verdict.md").write_text("APPROVE\n")

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result)["escape.md"] == SKIP_SYMLINK
        assert not (dest / "escape.md").exists()
        # The feature still works beside the refusal.
        assert (dest / "verdict.md").read_text() == "APPROVE\n"
        assert result.files == 1

    def test_a_symlinked_directory_is_not_descended(self, scratch, dest, tmp_path):
        outside = tmp_path / "outside-tree"
        outside.mkdir()
        (outside / "secret.md").write_text("PRIVATE")
        (scratch / "sub").symlink_to(outside, target_is_directory=True)
        (scratch / "verdict.md").write_text("APPROVE\n")

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result)["sub"] == SKIP_SYMLINK
        assert not (dest / "sub").exists()
        assert result.artifacts == ("verdict.md",)

    def test_a_dangling_symlink_is_refused_as_a_symlink_not_as_unreadable(
        self, scratch, dest, tmp_path
    ):
        (scratch / "dangling.md").symlink_to(tmp_path / "does-not-exist")

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result)["dangling.md"] == SKIP_SYMLINK


class TestHardLinkEscape:
    def test_a_hard_link_to_a_file_outside_the_root_is_refused(self, scratch, dest, tmp_path):
        """A hard link is a regular file: it passes a regular-files-only rule
        while making the harvester serve an inode the leg never produced under
        the scratch root."""
        secret = tmp_path / "outside.txt"
        secret.write_text("PRIVATE")
        os.link(secret, scratch / "linked.md")
        (scratch / "verdict.md").write_text("APPROVE\n")

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result)["linked.md"] == SKIP_HARDLINK
        assert not (dest / "linked.md").exists()
        assert (dest / "verdict.md").read_text() == "APPROVE\n"

    def test_a_link_created_between_the_check_and_the_open_is_refused(
        self, scratch, dest, tmp_path, monkeypatch
    ):
        """Why the link count is read from the descriptor rather than from the
        entry's stat: at stat time this file has one link and at open time it
        has two, so a check against the pre-open stat would wave it through."""
        import lionagi.cli.orchestrate._harvest as harvest_mod

        victim = scratch / "verdict.md"
        victim.write_text("APPROVE\n")
        (scratch / "other.md").write_text("kept\n")

        real_open = os.open
        linked: list[str] = []

        def linking_open(path, flags, *args, **kwargs):
            if path == "verdict.md" and not linked:
                linked.append(path)
                os.link(victim, tmp_path / "second-name")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(harvest_mod.os, "open", linking_open)

        result = harvest_leg_artifacts(scratch, dest)

        assert linked == ["verdict.md"]
        assert _skips(result)["verdict.md"] == SKIP_HARDLINK
        assert not (dest / "verdict.md").exists()
        assert (dest / "other.md").read_text() == "kept\n"

    def test_a_hard_link_between_two_files_inside_the_root_is_also_refused(self, scratch, dest):
        """The rule is link count, not location: the harvester cannot tell an
        inside link from an outside one by looking at the entry, and a rule
        that tried would be resolving paths again."""
        original = scratch / "verdict.md"
        original.write_text("APPROVE\n")
        os.link(original, scratch / "copy.md")

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result) == {"copy.md": SKIP_HARDLINK, "verdict.md": SKIP_HARDLINK}
        assert result.files == 0


class TestCheckToOpenSwap:
    def test_a_file_swapped_between_the_check_and_the_open_is_refused(
        self, scratch, dest, tmp_path, monkeypatch
    ):
        """Reproduces the race the descriptor anchoring exists to close: the
        entry is a regular file when stat'd and something else by the time it
        is opened. The swap is performed at the module's own open seam, so the
        pre-open stat and the opened descriptor genuinely describe different
        inodes."""
        import lionagi.cli.orchestrate._harvest as harvest_mod

        secret = tmp_path / "outside.txt"
        secret.write_text("PRIVATE")
        victim = scratch / "verdict.md"
        victim.write_text("APPROVE\n")
        (scratch / "other.md").write_text("kept\n")

        real_open = os.open
        swapped: list[str] = []

        def swapping_open(path, flags, *args, **kwargs):
            if path == "verdict.md" and not swapped:
                swapped.append(path)
                # Replace the checked file with a different inode carrying the
                # same name — what an attacking leg does between the two calls.
                victim.unlink()
                os.link(secret, victim)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(harvest_mod.os, "open", swapping_open)

        result = harvest_leg_artifacts(scratch, dest)

        assert swapped == ["verdict.md"]
        assert _skips(result)["verdict.md"] == SKIP_SWAPPED
        assert not (dest / "verdict.md").exists()
        assert (dest / "other.md").read_text() == "kept\n"

    def test_a_swap_that_keeps_the_link_count_at_one_is_still_caught(
        self, scratch, dest, tmp_path, monkeypatch
    ):
        """The swap above could in principle be caught by the link-count rule
        rather than by identity. Here the replacement arrives by rename, so its
        link count is 1 and only the device+inode comparison can refuse it."""
        import lionagi.cli.orchestrate._harvest as harvest_mod

        victim = scratch / "verdict.md"
        victim.write_text("APPROVE\n")
        (tmp_path / "decoy.txt").write_text("PRIVATE")

        real_open = os.open
        swapped: list[str] = []

        def swapping_open(path, flags, *args, **kwargs):
            if path == "verdict.md" and not swapped:
                swapped.append(path)
                os.replace(tmp_path / "decoy.txt", victim)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(harvest_mod.os, "open", swapping_open)

        result = harvest_leg_artifacts(scratch, dest)

        assert swapped == ["verdict.md"]
        assert victim.stat().st_nlink == 1  # the link-count rule cannot fire
        assert _skips(result)["verdict.md"] == SKIP_SWAPPED
        assert not (dest / "verdict.md").exists()


class TestSpecialFiles:
    def test_a_fifo_is_skipped_rather_than_opened(self, scratch, dest):
        """Opening a FIFO for reading blocks until a writer appears. A
        harvester that treated it as an ordinary file would hang the round."""
        os.mkfifo(scratch / "pipe")
        (scratch / "verdict.md").write_text("APPROVE\n")

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result)["pipe"] == SKIP_SPECIAL
        assert result.artifacts == ("verdict.md",)


class TestCaps:
    def test_the_byte_cap_aborts_mid_copy_and_never_serves_a_truncated_file(self, scratch, dest):
        (scratch / "big.bin").write_bytes(b"x" * 5000)

        result = harvest_leg_artifacts(scratch, dest, max_bytes=1000)

        assert result.state == STATE_FAILED
        assert "byte cap" in (result.detail or "")
        # A truncated artifact that reads as complete is exactly what the
        # abort exists to avoid.
        assert not (dest / "big.bin").exists()
        assert "big.bin" not in result.artifacts

    def test_the_byte_cap_counts_across_files_not_per_file(self, scratch, dest):
        (scratch / "a.bin").write_bytes(b"x" * 600)
        (scratch / "b.bin").write_bytes(b"x" * 600)

        result = harvest_leg_artifacts(scratch, dest, max_bytes=1000)

        assert result.state == STATE_FAILED
        assert result.artifacts == ("a.bin",)
        assert not (dest / "b.bin").exists()

    def test_the_file_cap_fails_the_harvest_rather_than_silently_stopping(self, scratch, dest):
        for i in range(5):
            (scratch / f"f{i}.md").write_text("x")

        result = harvest_leg_artifacts(scratch, dest, max_files=3)

        assert result.state == STATE_FAILED
        assert "file cap" in (result.detail or "")
        assert result.files == 3

    def test_a_harvest_exactly_at_the_byte_cap_succeeds(self, scratch, dest):
        """The cap is a bound, not an off-by-one trap: the boundary value is
        legitimate output."""
        (scratch / "exact.bin").write_bytes(b"x" * 1000)

        result = harvest_leg_artifacts(scratch, dest, max_bytes=1000)

        assert result.state == "harvested-1"
        assert result.bytes == 1000
        assert (dest / "exact.bin").stat().st_size == 1000


class TestUnreadableEntries:
    def test_a_file_that_cannot_be_opened_is_recorded_not_dropped(self, scratch, dest, monkeypatch):
        """A skip recorded only as a smaller file count is indistinguishable
        from a leg that wrote less."""
        import lionagi.cli.orchestrate._harvest as harvest_mod

        (scratch / "verdict.md").write_text("APPROVE\n")
        (scratch / "locked.md").write_text("secret\n")

        real_open = os.open

        def refusing_open(path, flags, *args, **kwargs):
            if path == "locked.md":
                raise PermissionError("refused")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(harvest_mod.os, "open", refusing_open)

        result = harvest_leg_artifacts(scratch, dest)

        assert _skips(result)["locked.md"] == SKIP_UNREADABLE
        assert result.artifacts == ("verdict.md",)

    def test_a_read_failure_mid_copy_leaves_no_partial_artifact(self, scratch, dest, monkeypatch):
        (scratch / "verdict.md").write_text("APPROVE\n")
        (scratch / "flaky.bin").write_bytes(b"x" * 4000)

        import builtins

        real_open_builtin = builtins.open

        class _FailingReader:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self._reads = 0

            def read(self, size):
                self._reads += 1
                if self._reads > 1:
                    raise OSError("device went away mid-read")
                return self._wrapped.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._wrapped.__exit__(*exc)

        def flaky_open(file, mode="r", *args, **kwargs):
            handle = real_open_builtin(file, mode, *args, **kwargs)
            if isinstance(file, int) and "b" in mode and "r" in mode:
                handle.__enter__()
                return _FailingReader(handle)
            return handle

        monkeypatch.setattr(builtins, "open", flaky_open)

        result = harvest_leg_artifacts(scratch, dest, max_bytes=10_000)

        monkeypatch.undo()
        assert _skips(result)["flaky.bin"] == SKIP_UNREADABLE
        assert not (dest / "flaky.bin").exists()


class TestSkippedEntriesAreNotEmptiness:
    def test_a_tree_of_only_refused_entries_is_not_reported_as_empty(self, scratch, dest, tmp_path):
        """`dir-empty` is a claim about the leg. A tree the harvester refused
        entirely is a claim about the harvest, and the record must be able to
        tell them apart."""
        secret = tmp_path / "outside.txt"
        secret.write_text("PRIVATE")
        (scratch / "escape.md").symlink_to(secret)

        result = harvest_leg_artifacts(scratch, dest)

        assert result.state == "harvested-0"
        assert result.state != STATE_EMPTY
        assert _skips(result) == {"escape.md": SKIP_SYMLINK}


class TestHostileNames:
    @pytest.mark.parametrize(
        "name",
        [
            "..hidden.md",
            "-rf.md",
            "--flag.md",
            "name with spaces.md",
            "naïve-café.md",
            "文件.md",
            "quote'and\"double.md",
            "semi;colon&amp.md",
            "$(whoami).md",
            "newline\nin-name.md",
        ],
    )
    def test_a_hostile_file_name_is_copied_as_data_never_interpreted(self, scratch, dest, name):
        """Names reach a shell nowhere on this path — every operation is a
        descriptor-relative syscall — so the only correct behaviour is to
        treat the name as bytes and copy the file."""
        (scratch / name).write_text("content\n")

        result = harvest_leg_artifacts(scratch, dest)

        assert result.artifacts == (name,)
        assert (dest / name).read_text() == "content\n"

    def test_a_name_that_looks_like_traversal_cannot_escape_the_destination(self, scratch, dest):
        """`..` is not representable as a single directory entry, so this
        tests the closest reachable thing: a name that merely looks like one."""
        (scratch / "..parent.md").write_text("x")

        result = harvest_leg_artifacts(scratch, dest)

        written = [p for p in dest.rglob("*") if p.is_file()]
        assert result.artifacts == ("..parent.md",)
        assert all(dest in p.parents for p in written)

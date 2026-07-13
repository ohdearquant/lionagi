# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for create_path/acreate_path directory traversal.

These tests verify that create_path and acreate_path refuse to create files
outside the supplied base directory when filenames contain path-traversal
components, absolute-path redirection, or symlink indirection.

Issue: acreate_path only rejected backslashes. A
caller could pass filename='../escape.txt' or 'sub/../../../escape.txt'
and receive or create paths outside the intended base directory.

Fix: reject '.' and '..' filename components before mkdir; resolve and
assert the candidate path stays within the resolved base directory. Both
constructors share this containment check (_build_safe_path) so sync
create_path and async acreate_path have equivalent symlink-containment
semantics — the sync variant previously had none at all.
"""

from pathlib import Path

import pytest

from lionagi.ln._utils import acreate_path, create_path


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    """Create a symlink, or skip the test on platforms without symlink support."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks not supported on this platform: {exc}")


class TestAcreatePathTraversalContainment:
    """Directory traversal must be refused before any filesystem side effect."""

    @pytest.mark.anyio
    async def test_dotdot_filename_raises_value_error(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match=r"'\.\.'|escape|traversal"):
            await acreate_path(directory=base, filename="../escape.txt")

    @pytest.mark.anyio
    async def test_nested_dotdot_raises_value_error(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename="sub/../../../etc/passwd")

    @pytest.mark.anyio
    async def test_double_dotdot_in_subdir_raises(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename="good/../../escape.txt")

    @pytest.mark.anyio
    async def test_dot_component_raises_value_error(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match=r"'\.'|'\.\.'"):
            await acreate_path(directory=base, filename="./sneaky.txt")

    @pytest.mark.anyio
    async def test_dotdot_standalone_raises(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename="..")

    @pytest.mark.anyio
    async def test_dot_standalone_raises(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename=".")

    @pytest.mark.anyio
    async def test_no_traversal_is_created_outside_base(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        escape_target = tmp_path / "escape.txt"
        try:
            await acreate_path(directory=base, filename="../escape.txt")
        except ValueError:
            pass
        # The escaped path must NOT exist
        assert not escape_target.exists(), "acreate_path created a file outside the base directory"

    @pytest.mark.anyio
    async def test_normal_subdir_filename_still_works(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        result = await acreate_path(directory=base, filename="sub/file.txt")
        assert result.name == "file.txt"
        assert result.parent.name == "sub"
        # Must be under base
        result.relative_to(base)

    @pytest.mark.anyio
    async def test_backslash_still_rejected(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match="directory separators"):
            await acreate_path(directory=base, filename="win\\path.txt")

    @pytest.mark.anyio
    async def test_symlinked_subdir_escape_rejected(self, tmp_path):
        """A symlinked subdirectory pointing outside the base must be rejected.

        Regression: the base root must be captured BEFORE the filename redirects
        `directory` into a subdir, otherwise resolve() through the symlink makes
        the escaped location look like the base.
        """
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (base / "link").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes base directory"):
            await acreate_path(directory=base, filename="link/escape.txt")
        assert not (outside / "escape.txt").exists()

    @pytest.mark.anyio
    async def test_symlinked_final_component_escape_rejected(self, tmp_path):
        """A symlinked final filename pointing outside the base must be rejected.

        Regression: validating `dir_resolved / filename` WITHOUT resolving the
        final component let `base/link.txt -> /outside/target.txt` pass, since
        the unresolved path is lexically under base. The candidate must be fully
        resolve()-d so the final symlink is followed.
        """
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "target.txt"
        target.write_text("secret")
        (base / "link.txt").symlink_to(target)

        with pytest.raises(ValueError, match="escapes base directory"):
            await acreate_path(directory=base, filename="link.txt", file_exist_ok=True)

    @pytest.mark.anyio
    async def test_absolute_filename_redirect_escape_rejected(self, tmp_path):
        """An absolute-looking filename segment must not redirect outside base.

        `"/etc"` joined onto a slash-separated filename lexically replaces
        the whole path when built with plain string concatenation; the
        containment re-check must still catch it.
        """
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match="escapes base directory"):
            await acreate_path(directory=base, filename="/etc/passwd-escape.txt")


class TestCreatePathTraversalContainment:
    """Sync create_path must reject the same escapes as async acreate_path.

    Prior to this fix, sync create_path performed no traversal or
    containment validation at all — only acreate_path did. These mirror the
    async TestAcreatePathTraversalContainment cases to prove the sync and
    async constructors now share identical semantics.
    """

    def test_dotdot_filename_raises_value_error(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match=r"'\.\.'|escape|traversal"):
            create_path(directory=base, filename="../escape.txt")

    def test_nested_dotdot_raises_value_error(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            create_path(directory=base, filename="sub/../../../etc/passwd")

    def test_dot_component_raises_value_error(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match=r"'\.'|'\.\.'"):
            create_path(directory=base, filename="./sneaky.txt")

    def test_dotdot_standalone_raises(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            create_path(directory=base, filename="..")

    def test_no_traversal_is_created_outside_base(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        escape_target = tmp_path / "escape.txt"
        try:
            create_path(directory=base, filename="../escape.txt")
        except ValueError:
            pass
        assert not escape_target.exists(), "create_path created a file outside the base directory"

    def test_normal_subdir_filename_still_works(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        result = create_path(directory=base, filename="sub/file.txt")
        assert result.name == "file.txt"
        assert result.parent.name == "sub"
        result.relative_to(base)

    def test_backslash_still_rejected(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match="directory separators"):
            create_path(directory=base, filename="win\\path.txt")

    def test_absolute_filename_redirect_escape_rejected(self, tmp_path):
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match="escapes base directory"):
            create_path(directory=base, filename="/etc/passwd-escape.txt")

    def test_symlinked_subdir_escape_rejected(self, tmp_path):
        """A symlinked subdirectory pointing outside the base must be rejected.

        This is the exact vector sync create_path had zero protection
        against before this fix: it never resolved or containment-checked
        the directory at all.
        """
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _symlink_or_skip(base / "link", outside, target_is_directory=True)

        with pytest.raises(ValueError, match="escapes base directory"):
            create_path(directory=base, filename="link/escape.txt")
        assert not (outside / "escape.txt").exists()

    def test_symlinked_final_component_escape_rejected(self, tmp_path):
        """A symlinked final filename pointing outside the base must be rejected."""
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "target.txt"
        target.write_text("secret")
        _symlink_or_skip(base / "link.txt", target)

        with pytest.raises(ValueError, match="escapes base directory"):
            create_path(directory=base, filename="link.txt", file_exist_ok=True)

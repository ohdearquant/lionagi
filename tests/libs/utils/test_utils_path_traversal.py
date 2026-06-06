# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Attack-driven regression tests for acreate_path directory traversal.

These tests verify that acreate_path refuses to create files outside the
supplied base directory when filenames contain path-traversal components.

Issue (LIONAGI-AUDIT-003): acreate_path only rejected backslashes. A
caller could pass filename='../escape.txt' or 'sub/../../../escape.txt'
and receive or create paths outside the intended base directory.

Fix: reject '.' and '..' filename components before mkdir; resolve and
assert the candidate path stays within the resolved base directory.
"""

import pytest

from lionagi.ln._utils import acreate_path


class TestAcreatePathTraversalContainment:
    """Directory traversal must be refused before any filesystem side effect."""

    @pytest.mark.anyio
    async def test_dotdot_filename_raises_value_error(self, tmp_path):
        """Classic '../escape.txt' traversal must be rejected."""
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match=r"'\.\.'|escape|traversal"):
            await acreate_path(directory=base, filename="../escape.txt")

    @pytest.mark.anyio
    async def test_nested_dotdot_raises_value_error(self, tmp_path):
        """Nested 'sub/../../../etc/passwd' traversal must be rejected."""
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename="sub/../../../etc/passwd")

    @pytest.mark.anyio
    async def test_double_dotdot_in_subdir_raises(self, tmp_path):
        """'good/../../escape.txt' must be rejected (escapes base after subdir)."""
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename="good/../../escape.txt")

    @pytest.mark.anyio
    async def test_dot_component_raises_value_error(self, tmp_path):
        """Filename component '.' must be rejected."""
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError, match=r"'\.'|'\.\.'"):
            await acreate_path(directory=base, filename="./sneaky.txt")

    @pytest.mark.anyio
    async def test_dotdot_standalone_raises(self, tmp_path):
        """Bare '..' as filename must be rejected."""
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename="..")

    @pytest.mark.anyio
    async def test_dot_standalone_raises(self, tmp_path):
        """Bare '.' as filename must be rejected."""
        base = tmp_path / "base"
        base.mkdir()
        with pytest.raises(ValueError):
            await acreate_path(directory=base, filename=".")

    @pytest.mark.anyio
    async def test_no_traversal_is_created_outside_base(self, tmp_path):
        """Verify no file is created outside base even on traversal attempt."""
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
        """Legitimate subdirectory in filename continues to work."""
        base = tmp_path / "base"
        base.mkdir()
        result = await acreate_path(directory=base, filename="sub/file.txt")
        assert result.name == "file.txt"
        assert result.parent.name == "sub"
        # Must be under base
        result.relative_to(base)

    @pytest.mark.anyio
    async def test_backslash_still_rejected(self, tmp_path):
        """Pre-existing backslash check is preserved."""
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

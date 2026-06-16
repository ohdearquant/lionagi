"""Tests for lionagi.ln._utils: acreate_path, get_bins, import_module."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lionagi.ln._utils import (
    acreate_path,
    get_bins,
    import_module,
    is_import_installed,
    now_utc,
)

# =============================================================================
# now_utc() Tests
# =============================================================================


class TestNowUtc:
    @pytest.mark.unit
    def test_now_utc_returns_datetime(self):
        result = now_utc()
        assert result is not None
        assert hasattr(result, "year")
        assert hasattr(result, "month")


# =============================================================================
# acreate_path() Async Tests
# =============================================================================


class TestAcreatePath:
    @pytest.mark.anyio
    async def test_acreate_path_basic(self, tmp_path):
        result = await acreate_path(directory=tmp_path, filename="test.txt")
        assert result.name == "test.txt"
        assert result.parent == tmp_path

    @pytest.mark.anyio
    async def test_acreate_path_with_subdirectory(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="subdir/test.txt",
        )
        assert result.name == "test.txt"
        assert result.parent.name == "subdir"
        assert await result.parent.exists()

    @pytest.mark.anyio
    async def test_acreate_path_backslash_raises(self, tmp_path):
        with pytest.raises(ValueError, match="cannot contain directory separators"):
            await acreate_path(directory=tmp_path, filename="test\\file.txt")

    @pytest.mark.anyio
    async def test_acreate_path_with_extension_in_filename(self, tmp_path):
        result = await acreate_path(directory=tmp_path, filename="test.txt")
        assert result.name == "test.txt"
        assert result.suffix == ".txt"

    @pytest.mark.anyio
    async def test_acreate_path_with_explicit_extension(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test",
            extension=".log",
        )
        assert result.name == "test.log"
        assert result.suffix == ".log"

    @pytest.mark.anyio
    async def test_acreate_path_extension_overrides_filename_ext(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            extension=".log",
        )
        # When filename has extension, it's used; extension param only for files without ext
        assert result.suffix == ".txt"

    @pytest.mark.anyio
    async def test_acreate_path_with_timestamp_prefix(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            timestamp=True,
            time_prefix=True,
        )
        # Should be YYYYMMDDHHMMSS_test.txt
        assert result.suffix == ".txt"
        assert "_test" in result.stem
        # Verify timestamp format (14 digits)
        prefix = result.stem.split("_")[0]
        assert len(prefix) == 14
        assert prefix.isdigit()

    @pytest.mark.anyio
    async def test_acreate_path_with_timestamp_suffix(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            timestamp=True,
            time_prefix=False,
        )
        # Should be test_YYYYMMDDHHMMSS.txt
        assert result.suffix == ".txt"
        assert "test_" in result.stem
        suffix = result.stem.split("_")[1]
        assert len(suffix) == 14
        assert suffix.isdigit()

    @pytest.mark.anyio
    async def test_acreate_path_with_custom_timestamp_format(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            timestamp=True,
            timestamp_format="%Y%m%d",
        )
        # Should have 8-digit date format
        assert result.suffix == ".txt"
        parts = result.stem.split("_")
        assert any(len(p) == 8 and p.isdigit() for p in parts)

    @pytest.mark.anyio
    async def test_acreate_path_with_random_hash(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            random_hash_digits=8,
        )
        # Should be test-XXXXXXXX.txt
        assert result.suffix == ".txt"
        assert "-" in result.stem
        hash_part = result.stem.split("-")[1]
        assert len(hash_part) == 8

    @pytest.mark.anyio
    async def test_acreate_path_with_timestamp_and_hash(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            timestamp=True,
            time_prefix=True,
            random_hash_digits=6,
        )
        assert result.suffix == ".txt"
        assert "_" in result.stem
        assert "-" in result.stem

    @pytest.mark.anyio
    async def test_acreate_path_file_exists_ok_true(self, tmp_path):
        # Create file first
        test_file = tmp_path / "test.txt"
        test_file.touch()

        # Should not raise error
        result = await acreate_path(
            directory=tmp_path,
            filename="test.txt",
            file_exist_ok=True,
        )
        assert result.name == "test.txt"

    @pytest.mark.anyio
    async def test_acreate_path_file_exists_raises(self, tmp_path):
        # Create file first
        test_file = tmp_path / "test.txt"
        test_file.touch()

        with pytest.raises(FileExistsError, match="already exists"):
            await acreate_path(
                directory=tmp_path,
                filename="test.txt",
                file_exist_ok=False,
            )

    @pytest.mark.anyio
    async def test_acreate_path_creates_parent_directories(self, tmp_path):
        result = await acreate_path(
            directory=tmp_path,
            filename="deep/nested/structure/test.txt",
        )
        assert await result.parent.exists()
        assert result.parent.name == "structure"


# =============================================================================
# get_bins() Tests
# =============================================================================


class TestGetBins:
    @pytest.mark.unit
    def test_get_bins_basic(self):
        result = get_bins(["a" * 10, "b" * 10, "c" * 10], upper=25)
        assert len(result) == 2
        assert result[0] == [0, 1]
        assert result[1] == [2]

    @pytest.mark.unit
    def test_get_bins_empty_input(self):
        result = get_bins([], upper=100)
        assert result == []

    @pytest.mark.unit
    def test_get_bins_single_item_fits(self):
        result = get_bins(["a" * 50], upper=100)
        assert result == [[0]]

    @pytest.mark.unit
    def test_get_bins_single_item_exceeds_upper(self):
        """Test get_bins when single item exceeds upper limit.

        Note: Algorithm creates empty bin first when item exceeds limit,
        resulting in [[], [0]] for oversized single item.
        """
        result = get_bins(["a" * 200], upper=100)
        assert len(result) == 2
        assert result == [[], [0]]  # Empty bin, then oversized item

    @pytest.mark.unit
    def test_get_bins_exact_boundary(self):
        # First two items total exactly 100 (50 + 49 = 99 < 100)
        result = get_bins(["a" * 50, "b" * 49, "c" * 30], upper=100)
        assert len(result) == 2
        assert result[0] == [0, 1]
        assert result[1] == [2]

    @pytest.mark.unit
    def test_get_bins_all_items_fit_one_bin(self):
        result = get_bins(["a" * 10, "b" * 10, "c" * 10], upper=100)
        assert len(result) == 1
        assert result[0] == [0, 1, 2]

    @pytest.mark.property
    @given(
        strings=st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=20),
        upper=st.integers(min_value=10, max_value=200),
    )
    @settings(max_examples=50)
    def test_get_bins_property_invariants(self, strings, upper):
        result = get_bins(strings, upper)

        # All indices should be present exactly once
        all_indices = [idx for bin_ in result for idx in bin_]
        assert sorted(all_indices) == list(range(len(strings)))

        # Each bin should not exceed upper limit (except single oversized items)
        for bin_ in result:
            bin_length = sum(len(strings[i]) for i in bin_)
            # Allow single oversized item
            if len(bin_) == 1:
                continue
            assert bin_length < upper  # Note: < not <=, based on source code logic


# =============================================================================
# import_module() Tests
# =============================================================================


class TestImportModule:
    @pytest.mark.unit
    def test_import_module_package_only(self):
        result = import_module("json")
        assert result is not None
        assert hasattr(result, "dumps")

    @pytest.mark.unit
    def test_import_module_with_module_name(self):
        result = import_module("os", "path")
        assert result is not None

    @pytest.mark.unit
    def test_import_module_with_single_import_name(self):
        result = import_module("json", import_name="dumps")
        assert callable(result)
        # Verify it's the actual dumps function
        assert result({"test": 1}) == '{"test": 1}'

    @pytest.mark.unit
    def test_import_module_with_list_import_names(self):
        result = import_module("json", import_name=["dumps", "loads"])
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(callable(f) for f in result)

    @pytest.mark.unit
    def test_import_module_invalid_package_raises(self):
        with pytest.raises(ImportError, match="Failed to import"):
            import_module("nonexistent_package_xyz")

    @pytest.mark.unit
    def test_import_module_invalid_module_raises(self):
        with pytest.raises(ImportError, match="Failed to import"):
            import_module("os", "nonexistent_module_xyz")


# =============================================================================
# is_import_installed() Tests
# =============================================================================


class TestIsImportInstalled:
    @pytest.mark.unit
    def test_is_import_installed_true_for_stdlib(self):
        assert is_import_installed("json") is True
        assert is_import_installed("os") is True
        assert is_import_installed("sys") is True

    @pytest.mark.unit
    def test_is_import_installed_true_for_installed_packages(self):
        assert is_import_installed("pytest") is True
        assert is_import_installed("anyio") is True

    @pytest.mark.unit
    def test_is_import_installed_false_for_nonexistent(self):
        assert is_import_installed("nonexistent_package_xyz_12345") is False

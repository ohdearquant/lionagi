# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Coverage tests for lionagi/protocols/generic/pile.py."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class Item(Element):
    value: int = 0


class OtherItem(Element):
    name: str = ""


@pytest.fixture
def three_items():
    return [Item(value=i) for i in range(3)]


@pytest.fixture
def five_items():
    return [Item(value=i) for i in range(5)]


@pytest.fixture
def pile_3(three_items):
    return Pile(collections=three_items)


@pytest.fixture
def pile_5(five_items):
    return Pile(collections=five_items)


# ---------------------------------------------------------------------------
# 1. to_df / dump (pandas-dependent)
# ---------------------------------------------------------------------------

pandas_missing = importlib.util.find_spec("pandas") is None


@pytest.mark.skipif(pandas_missing, reason="pandas not installed")
class TestToDataFrame:
    def test_to_df_returns_dataframe(self, pile_3):
        import pandas as pd

        df = pile_3.to_df()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 3

    def test_to_df_has_expected_columns(self, pile_3):
        df = pile_3.to_df()
        for col in ("id", "created_at", "value"):
            assert col in df.columns

    def test_to_df_column_subset(self, pile_3):
        df = pile_3.to_df(columns=["id", "value"])
        assert list(df.columns) == ["id", "value"]
        assert len(df) == 3

    def test_to_df_empty_pile(self):
        import pandas as pd

        df = Pile().to_df()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_to_df_values_match(self, five_items):
        p = Pile(collections=five_items)
        df = p.to_df()
        assert sorted(df["value"].tolist()) == list(range(5))


@pytest.mark.skipif(pandas_missing, reason="pandas not installed")
class TestDump:
    def test_dump_json(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fp = Path(f.name)
        pile_3.dump(fp, obj_key="json")
        content = fp.read_text()
        assert len(content) > 0
        for item in pile_3.values():
            assert str(item.id) in content
        fp.unlink()

    def test_dump_csv(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            fp = Path(f.name)
        pile_3.dump(fp, obj_key="csv")
        lines = fp.read_text().strip().splitlines()
        assert lines[0].startswith("id")
        assert len(lines) == 4  # header + 3 rows
        fp.unlink()

    def test_dump_invalid_key_raises(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            fp = Path(f.name)
        with pytest.raises(ValueError, match="Unsupported obj_key"):
            pile_3.dump(fp, obj_key="xml")
        fp.unlink()

    def test_dump_parquet(self, pile_3):
        pytest.importorskip("pyarrow")
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            fp = Path(f.name)
        pile_3.dump(fp, obj_key="parquet")
        assert fp.stat().st_size > 0
        fp.unlink()

    def test_dump_csv_clear(self):
        items = [Item(value=i) for i in range(3)]
        p = Pile(collections=items)
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            fp = Path(f.name)
        pytest.importorskip("pyarrow")
        p.dump(fp, obj_key="parquet", clear=True)
        assert len(p) == 0
        fp.unlink()

    @pytest.mark.asyncio
    async def test_adump_json(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fp = Path(f.name)
        await pile_3.adump(fp, obj_key="json")
        content = fp.read_text()
        assert len(content) > 0
        fp.unlink()


# ---------------------------------------------------------------------------
# 2. Set operations — __ior__, __iand__, __ixor__ (in-place; these work)
# ---------------------------------------------------------------------------

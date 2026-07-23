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

    def test_to_df_preserves_progression_order(self, three_items):
        """to_df must follow logical (progression) order, not dict insertion order."""
        p = Pile(collections=three_items)
        head = Item(value=99)
        p.insert(0, head)  # logical order is now head, *three_items
        logical = [str(x.id) for x in p]
        df = p.to_df()
        assert [str(x) for x in df["id"].tolist()] == logical


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

    def test_dump_json_clear_empties_pile(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fp = Path(f.name)
        assert len(pile_3) == 3
        pile_3.dump(fp, obj_key="json", clear=True)
        assert len(pile_3) == 0
        # data was written to the file before clearing
        assert len(fp.read_text().strip().splitlines()) == 3
        fp.unlink()

    def test_dump_csv_clear_empties_pile(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            fp = Path(f.name)
        pile_3.dump(fp, obj_key="csv", clear=True)
        assert len(pile_3) == 0
        assert len(fp.read_text().strip().splitlines()) == 4  # header + 3 rows
        fp.unlink()

    def test_dump_json_no_clear_preserves_pile(self, pile_3):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            fp = Path(f.name)
        pile_3.dump(fp, obj_key="json", clear=False)
        assert len(pile_3) == 3
        fp.unlink()

    def test_dump_fp_none_returns_serialized_string(self, pile_3):
        out = pile_3.dump(None, obj_key="json")
        assert isinstance(out, str) and out.strip()
        assert len(pile_3) == 3  # default clear=False leaves the pile intact

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

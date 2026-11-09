"""The moved-module table is looked up through an index on the first name component.

``lionagi._moved`` groups the rows of ``MOVED`` by the first dotted component of their old
name, and a lookup runs the longest-prefix match over the one group that shares the name's
first component. The grouping follows the current value of ``MOVED``: a lookup after ``MOVED``
is rebound to another tuple sees that tuple's rows. These tests count the rows a lookup reads,
using rows that record every read of ``old``, compare every result with a plain linear
longest-prefix scan over the whole table, and check that a rebound table is picked up and a
restored one is dropped again.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from lionagi import _moved

pytestmark = pytest.mark.unit


class _CountedRow:
    """A table row that counts the reads of ``old`` a lookup makes."""

    def __init__(self, old: str) -> None:
        self.text = old
        self.new = "moved." + old
        self.deprecated_in = "0.37.0"
        self.removed_in = "2.0.0"
        self.reads = 0

    @property
    def old(self) -> str:
        self.reads += 1
        return self.text


_SYNTHETIC_OLD_NAMES = (
    "lionagi.alpha",
    "lionagi.alpha.inner",
    "lionagi.beta",
    "other.pkg",
    "third.pkg.sub",
    "lionagi.gamma",
)

_UNRELATED_NAMES = (
    "json.decoder",
    "lionhub.orchestration.spawn",
    "os",
    "sys",
    "lionhub",
    "Lionagi.providers",
    "a.b.c",
    "",
)


@pytest.fixture(autouse=True)
def _restore_index(monkeypatch):
    """Put the module's index back after a test that rebinds the table."""
    monkeypatch.setattr(_moved, "_INDEX", _moved._INDEX)


@pytest.fixture
def counted_rows(monkeypatch):
    """Synthetic rows in the module's table and index, with the reads of building it cleared."""
    rows = tuple(_CountedRow(old) for old in _SYNTHETIC_OLD_NAMES)
    index = _moved._index_by_first_component(rows)
    for row in rows:
        row.reads = 0
    # The table and an index built from it are installed together, so a lookup finds the
    # index current and every read of ``old`` it makes is counted.
    monkeypatch.setattr(_moved, "MOVED", rows)
    monkeypatch.setattr(_moved, "_INDEX", (rows, index))
    return rows


def _current_index():
    """The index for the table as it stands, after a lookup has had the chance to rebuild it."""
    _moved._row("")
    return _moved._INDEX[1]


def _read_rows(rows) -> list[str]:
    return [row.text for row in rows if row.reads]


def _linear_longest_prefix(fullname, rows):
    """The row whose old name is the longest dotted prefix of ``fullname``, over every row."""
    matching = [row for row in rows if fullname == row.old or fullname.startswith(row.old + ".")]
    longest = max((len(row.old) for row in matching), default=None)
    return next((row for row in matching if len(row.old) == longest), None)


@pytest.mark.parametrize("name", _UNRELATED_NAMES)
def test_a_name_that_shares_no_first_component_reads_no_row(counted_rows, name):
    assert _moved._row(name) is None
    assert _read_rows(counted_rows) == []


def test_a_name_reads_only_the_rows_that_share_its_first_component(counted_rows):
    assert _moved._row("third.pkg.sub.leaf") is counted_rows[4]
    assert _read_rows(counted_rows) == ["third.pkg.sub"]

    for row in counted_rows:
        row.reads = 0
    assert _moved._row("lionagi.alpha.inner.leaf") is counted_rows[1]
    assert _read_rows(counted_rows) == [
        "lionagi.alpha",
        "lionagi.alpha.inner",
        "lionagi.beta",
        "lionagi.gamma",
    ]


def test_the_counter_registers_a_linear_scan_of_every_row(counted_rows):
    assert _linear_longest_prefix("json.decoder", counted_rows) is None
    assert _read_rows(counted_rows) == list(_SYNTHETIC_OLD_NAMES)


def test_the_index_is_read_only_and_not_public():
    index = _current_index()
    assert isinstance(index, MappingProxyType)
    with pytest.raises(TypeError):
        index["extra"] = ()
    assert "_INDEX" not in _moved.__all__
    assert "_index_by_first_component" not in _moved.__all__


def test_the_index_holds_every_row_once_in_table_order():
    index = _current_index()
    assert sum(len(bucket) for bucket in index.values()) == len(_moved.MOVED)
    for first, bucket in index.items():
        assert isinstance(bucket, tuple)
        assert bucket == tuple(row for row in _moved.MOVED if row.old.split(".", 1)[0] == first)


def _overlapping_pairs():
    return [
        (outer, inner)
        for outer in _moved.MOVED
        for inner in _moved.MOVED
        if inner.old.startswith(outer.old + ".")
    ]


def _real_table_names() -> list[str]:
    names = []
    for row in _moved.MOVED:
        names += [row.old, row.old + ".descendant", row.old + ".deeper.descendant"]
        # Names that continue or truncate an old name without being below it.
        names += [row.old + "x", row.old + "_", row.old[:-1], row.old + "."]
    for outer, inner in _overlapping_pairs():
        names += [inner.old, inner.old + ".leaf", outer.old, outer.old + ".sibling"]
    names += [
        "lionagi",
        "lionagi.",
        "lionagi.providersx",
        "lionagi.provider",
        "lionagi.providers_",
        "lionagix.providers",
        "lionagi_providers",
        "lionagi.cli",
        "lionagi.cli._runsx",
        "lionagi.operations",
        "lionagi.operations.lndl_middle_extra",
        "lionagi.orchestrationx.y",
    ]
    names += _UNRELATED_NAMES
    return names


def test_lookup_equals_a_linear_longest_prefix_scan_over_the_real_table():
    names = _real_table_names()
    resolved = 0
    for name in names:
        found = _moved._row(name)
        assert found is _linear_longest_prefix(name, _moved.MOVED), name
        resolved += found is not None
    # The comparison covers matches as well as misses.
    assert resolved >= len(_moved.MOVED)
    assert resolved < len(names)


def test_overlapping_rows_resolve_to_the_most_specific_one():
    pairs = {(outer.old, inner.old) for outer, inner in _overlapping_pairs()}
    assert ("lionagi.providers", "lionagi.providers._secret_resolution") in pairs
    assert _moved._row("lionagi.providers").old == "lionagi.providers"
    assert _moved._row("lionagi.providers.openai").old == "lionagi.providers"
    assert (
        _moved._row("lionagi.providers._secret_resolution").new
        == "lionagi.service.accounts._secret_resolution"
    )
    assert (
        _moved._row("lionagi.providers.openai._chat_schemas.leaf").old
        == "lionagi.providers.openai._chat_schemas"
    )
    assert _moved._row("lionagi.providersx") is None
    assert _moved._row("lionagi.provider") is None


@pytest.mark.parametrize("specific_first", [True, False], ids=["specific-first", "parent-first"])
def test_a_specific_row_beats_its_parent_in_either_table_order(monkeypatch, specific_first):
    parent = _moved.Moved("lionagi.cli", "hypothetical.cli", "0.37.0", "2.0.0")
    specific = _moved.Moved("lionagi.cli._util", "hypothetical.util", "0.37.0", "2.0.0")
    rows = (specific, parent) if specific_first else (parent, specific)
    monkeypatch.setattr(_moved, "MOVED", rows)
    for name in (
        "lionagi.cli",
        "lionagi.cli._util",
        "lionagi.cli._util.leaf",
        "lionagi.cli._utilx",
        "lionagi.cli.other",
        "lionagi.client",
        "json.decoder",
    ):
        assert _moved._row(name) is _linear_longest_prefix(name, rows), name
    assert _moved._row("lionagi.cli._util.leaf") is specific
    assert _moved._row("lionagi.cli._utilx") is parent


_EXTRA_IN_LIONAGI = _moved.Moved("lionagi.synthetic_extra", "hypothetical.extra", "0.37.0", "2.0.0")
_EXTRA_OTHER_ROOT = _moved.Moved("synthetic_root.pkg", "hypothetical.pkg", "0.37.0", "2.0.0")


def test_a_row_added_by_rebinding_the_table_is_seen_on_the_next_lookup(monkeypatch):
    original = _moved.MOVED
    names = ("lionagi.synthetic_extra.leaf", "synthetic_root.pkg.leaf")
    for name in names:
        assert _moved._row(name) is None
        assert _moved.resolve(name) is None

    monkeypatch.setattr(_moved, "MOVED", original + (_EXTRA_IN_LIONAGI, _EXTRA_OTHER_ROOT))

    assert _moved._row("lionagi.synthetic_extra.leaf") is _EXTRA_IN_LIONAGI
    assert _moved.resolve("lionagi.synthetic_extra.leaf") == "hypothetical.extra.leaf"
    assert _moved._row("synthetic_root.pkg.leaf") is _EXTRA_OTHER_ROOT
    assert _moved.resolve("synthetic_root.pkg") == "hypothetical.pkg"
    # Rows that were already in the table still resolve, and the index now belongs to the
    # rebound table.
    assert _moved.resolve("lionagi.providers.openai") == "lionhub.providers.openai"
    assert _moved._INDEX[0] is _moved.MOVED


def test_restoring_the_original_table_drops_a_row_added_by_rebinding(monkeypatch):
    original = _moved.MOVED
    monkeypatch.setattr(_moved, "MOVED", original + (_EXTRA_IN_LIONAGI, _EXTRA_OTHER_ROOT))
    assert _moved._row("lionagi.synthetic_extra") is _EXTRA_IN_LIONAGI
    assert _moved._row("synthetic_root.pkg") is _EXTRA_OTHER_ROOT

    monkeypatch.setattr(_moved, "MOVED", original)

    for name in ("lionagi.synthetic_extra.leaf", "lionagi.synthetic_extra", "synthetic_root.pkg"):
        assert _moved._row(name) is None
        assert _moved.resolve(name) is None
    assert _moved.resolve("lionagi.providers.openai") == "lionhub.providers.openai"
    assert _moved._INDEX[0] is original


def test_the_index_is_kept_while_the_table_is_the_same_tuple_and_rebuilt_for_another(monkeypatch):
    _moved._row("")
    pair = _moved._INDEX
    assert pair[0] is _moved.MOVED
    for name in ("lionagi.providers", "json.decoder", "lionagi.cli._util.leaf"):
        _moved._row(name)
        assert _moved._INDEX is pair

    # An equal tuple that is not the same object is another table: the index follows
    # identity, so it is rebuilt from the tuple now in place.
    equal_copy = tuple(list(_moved.MOVED))
    assert equal_copy == _moved.MOVED
    assert equal_copy is not _moved.MOVED
    monkeypatch.setattr(_moved, "MOVED", equal_copy)
    _moved._row("lionagi.providers")
    assert _moved._INDEX is not pair
    assert _moved._INDEX[0] is equal_copy

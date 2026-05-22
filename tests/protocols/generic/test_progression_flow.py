"""Flow and Progression behavior tests: lifecycle, add/remove, move/swap/sub."""

import pytest

from lionagi._errors import ItemExistsError, ItemNotFoundError
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.flow import Flow
from lionagi.protocols.generic.progression import Progression, prog


@pytest.fixture()
def four_elem_prog():
    elems = [Element() for _ in range(4)]
    p = Progression(order=[e.id for e in elems])
    return p, elems


def _flow_with_prog(name: str):
    f = Flow()
    elem = Element()
    f.add_item(elem)
    p = Progression(order=[elem.id], name=name)
    f.add_progression(p)
    return f, elem, p


class TestFlowInstantiation:
    def test_default_flow_empty(self):
        f = Flow()
        assert len(f.items) == 0
        assert len(f.progressions) == 0

    def test_named_flow(self):
        f = Flow(name="pipeline")
        assert f.name == "pipeline"

    def test_repr_contains_flow(self):
        assert "Flow" in repr(Flow())

    def test_len_equals_item_count(self):
        f = Flow()
        for _ in range(3):
            f.add_item(Element())
        assert len(f) == 3


class TestFlowAddProgression:
    def test_add_progression_with_items(self):
        f = Flow()
        elem = Element()
        f.add_item(elem)
        p = Progression(order=[elem.id], name="with-items")
        f.add_progression(p)
        assert len(f.progressions) == 1

    def test_progression_retrievable_by_name(self):
        f, elem, p = _flow_with_prog("stage1")
        assert f.get_progression("stage1").id == p.id

    def test_progression_name_indexed(self):
        f, elem, p = _flow_with_prog("idx-test")
        assert "idx-test" in f._progression_names

    def test_duplicate_name_raises(self):
        f, elem, p1 = _flow_with_prog("dup")
        elem2 = Element()
        f.add_item(elem2)
        p2 = Progression(order=[elem2.id], name="dup")
        with pytest.raises(ItemExistsError):
            f.add_progression(p2)

    def test_progression_referencing_unknown_item_raises(self):
        f = Flow()
        unknown_id = Element().id
        p = Progression(order=[unknown_id], name="missing")
        with pytest.raises(ItemNotFoundError):
            f.add_progression(p)

    def test_two_progressions_same_flow(self):
        f = Flow()
        e1, e2 = Element(), Element()
        f.add_item(e1)
        f.add_item(e2)
        p1 = Progression(order=[e1.id], name="p1")
        p2 = Progression(order=[e2.id], name="p2")
        f.add_progression(p1)
        f.add_progression(p2)
        assert len(f.progressions) == 2


class TestFlowRemoveProgression:
    def test_remove_by_name(self):
        f, elem, p = _flow_with_prog("s1")
        f.remove_progression("s1")
        assert len(f.progressions) == 0

    def test_remove_by_uuid(self):
        f, elem, p = _flow_with_prog("s2")
        f.remove_progression(p.id)
        assert len(f.progressions) == 0

    def test_remove_by_instance(self):
        f, elem, p = _flow_with_prog("s3")
        f.remove_progression(p)
        assert len(f.progressions) == 0

    def test_name_removed_from_index(self):
        f, elem, p = _flow_with_prog("to-delete")
        f.remove_progression("to-delete")
        assert "to-delete" not in f._progression_names


class TestFlowGetProgression:
    def test_get_by_name(self):
        f, elem, p = _flow_with_prog("find-me")
        assert f.get_progression("find-me").id == p.id

    def test_get_by_uuid(self):
        f, elem, p = _flow_with_prog("by-id")
        assert f.get_progression(p.id).id == p.id

    def test_get_by_instance(self):
        f, elem, p = _flow_with_prog("by-inst")
        assert f.get_progression(p).id == p.id

    def test_get_missing_name_raises(self):
        f = Flow()
        with pytest.raises(ItemNotFoundError):
            f.get_progression("nope")


class TestFlowAddItem:
    def test_item_added_to_pile(self):
        f = Flow()
        elem = Element()
        f.add_item(elem)
        assert elem.id in f.items

    def test_item_added_to_named_progression(self):
        f, initial_elem, p = _flow_with_prog("p1")
        new_elem = Element()
        f.add_item(new_elem, progressions="p1")
        assert new_elem.id in p

    def test_item_added_to_multiple_progressions(self):
        f = Flow()
        seed1, seed2 = Element(), Element()
        f.add_item(seed1)
        f.add_item(seed2)
        p1 = Progression(order=[seed1.id], name="p1")
        p2 = Progression(order=[seed2.id], name="p2")
        f.add_progression(p1)
        f.add_progression(p2)
        new_elem = Element()
        f.add_item(new_elem, progressions=["p1", "p2"])
        assert new_elem.id in p1
        assert new_elem.id in p2

    def test_item_added_without_progression(self):
        f = Flow()
        elem = Element()
        f.add_item(elem)
        assert len(f.items) == 1
        assert len(f.progressions) == 0


class TestFlowRemoveItem:
    def test_remove_from_pile(self):
        f = Flow()
        elem = Element()
        f.add_item(elem)
        f.remove_item(elem)
        assert elem.id not in f.items

    def test_remove_cleans_progressions(self):
        f, elem, p = _flow_with_prog("track")
        assert elem.id in p
        f.remove_item(elem)
        assert elem.id not in p

    def test_remove_by_uuid(self):
        f = Flow()
        elem = Element()
        f.add_item(elem)
        f.remove_item(elem.id)
        assert elem.id not in f.items


class TestFlowClear:
    def test_clear_empties_items_and_progressions(self):
        f, elem, p = _flow_with_prog("s")
        f.clear()
        assert len(f.items) == 0
        assert len(f.progressions) == 0
        assert f._progression_names == {}


class TestProgressionMove:
    def test_move_to_later_position(self, four_elem_prog):
        p, elems = four_elem_prog
        first_id = p[0]
        p.move(0, 2)
        assert first_id in list(p.order)
        assert len(p) == 4

    def test_move_to_earlier_position(self, four_elem_prog):
        p, elems = four_elem_prog
        last_id = p[3]
        p.move(3, 0)
        assert list(p.order)[0] == last_id

    def test_move_preserves_length(self, four_elem_prog):
        p, elems = four_elem_prog
        p.move(0, 3)
        assert len(p) == 4

    def test_move_negative_from_index(self, four_elem_prog):
        p, elems = four_elem_prog
        last_id = p[-1]
        p.move(-1, 0)
        assert list(p.order)[0] == last_id

    def test_move_middle_element(self, four_elem_prog):
        p, elems = four_elem_prog
        mid_id = p[1]
        p.move(1, 3)
        assert mid_id in list(p.order)
        assert len(p) == 4


class TestProgressionSwap:
    def test_swap_adjacent(self, four_elem_prog):
        p, elems = four_elem_prog
        id0, id1 = p[0], p[1]
        p.swap(0, 1)
        assert p[0] == id1 and p[1] == id0

    def test_swap_first_and_last(self, four_elem_prog):
        p, elems = four_elem_prog
        id_first, id_last = p[0], p[3]
        p.swap(0, 3)
        assert p[0] == id_last and p[3] == id_first

    def test_swap_same_index_noop(self, four_elem_prog):
        p, elems = four_elem_prog
        original = list(p.order)
        p.swap(2, 2)
        assert list(p.order) == original

    def test_swap_negative_indices(self, four_elem_prog):
        p, elems = four_elem_prog
        id_last, id_penultimate = p[-1], p[-2]
        p.swap(-1, -2)
        assert p[-1] == id_penultimate and p[-2] == id_last

    def test_swap_preserves_length(self, four_elem_prog):
        p, elems = four_elem_prog
        p.swap(0, 2)
        assert len(p) == 4


class TestProgressionSub:
    def test_sub_removes_shared_ids(self, four_elem_prog):
        p, elems = four_elem_prog
        result = p - [elems[0].id, elems[1].id]
        result_ids = list(result.order)
        assert elems[0].id not in result_ids
        assert elems[1].id not in result_ids
        assert elems[2].id in result_ids
        assert elems[3].id in result_ids

    def test_sub_returns_new_progression(self, four_elem_prog):
        p, elems = four_elem_prog
        result = p - [elems[0].id]
        assert isinstance(result, Progression)
        assert len(p) == 4
        assert len(result) == 3

    def test_sub_single_element(self, four_elem_prog):
        p, elems = four_elem_prog
        result = p - elems[2]
        result_ids = list(result.order)
        assert elems[2].id not in result_ids
        assert len(result) == 3

    def test_sub_all_elements(self, four_elem_prog):
        p, elems = four_elem_prog
        all_ids = [e.id for e in elems]
        result = p - all_ids
        assert len(result) == 0


class TestProgressionReversed:
    def test_reversed_returns_progression(self, four_elem_prog):
        p, elems = four_elem_prog
        assert isinstance(reversed(p), Progression)

    def test_reversed_correct_order(self, four_elem_prog):
        p, elems = four_elem_prog
        original = list(p.order)
        assert list(reversed(p).order) == original[::-1]

    def test_reversed_does_not_mutate_original(self, four_elem_prog):
        p, elems = four_elem_prog
        original = list(p.order)
        _ = reversed(p)
        assert list(p.order) == original

    def test_last_id_via_reversed(self, four_elem_prog):
        p, elems = four_elem_prog
        rev = reversed(p)
        assert list(rev.order)[0] == elems[3].id


class TestProgressionNegativeIndex:
    def test_neg1_is_last(self, four_elem_prog):
        p, elems = four_elem_prog
        assert p[-1] == elems[3].id

    def test_neg2_is_second_to_last(self, four_elem_prog):
        p, elems = four_elem_prog
        assert p[-2] == elems[2].id

    def test_neg_len_is_first(self, four_elem_prog):
        p, elems = four_elem_prog
        assert p[-4] == elems[0].id

    def test_out_of_range_raises(self, four_elem_prog):
        p, elems = four_elem_prog
        with pytest.raises(ItemNotFoundError):
            _ = p[-10]


class TestProgressionValidateIndex:
    def test_negative_converted_to_positive(self):
        elems = [Element() for _ in range(3)]
        p = Progression(order=[e.id for e in elems])
        assert p._validate_index(-1) == 2

    def test_empty_progression_raises(self):
        p = Progression()
        with pytest.raises(ItemNotFoundError):
            p._validate_index(0)

    def test_out_of_range_raises(self):
        elems = [Element() for _ in range(2)]
        p = Progression(order=[e.id for e in elems])
        with pytest.raises(ItemNotFoundError):
            p._validate_index(5)

    def test_allow_end_permits_len(self):
        elems = [Element() for _ in range(3)]
        p = Progression(order=[e.id for e in elems])
        assert p._validate_index(3, allow_end=True) == 3


class TestProgFactory:
    def test_creates_named_progression(self):
        elems = [Element() for _ in range(3)]
        p = prog([e.id for e in elems], "my-prog")
        assert isinstance(p, Progression)
        assert p.name == "my-prog"
        assert len(p) == 3

    def test_creates_unnamed_progression(self):
        p = prog([])
        assert p.name is None
        assert len(p) == 0

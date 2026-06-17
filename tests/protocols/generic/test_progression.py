import asyncio
import random
import string
from typing import Any
from uuid import UUID

import pytest
from pydantic import Field

from lionagi._errors import ItemNotFoundError
from lionagi.protocols.types import ID, Element, Progression
from lionagi.testing import MockElement


@pytest.fixture
def sample_elements():
    return [MockElement(value=i) for i in range(5)]


@pytest.fixture
def sample_progression(sample_elements):
    return Progression(order=[e.id for e in sample_elements])


def generate_random_string(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


@pytest.mark.parametrize(
    "input_data",
    [
        [],
        [MockElement(value=i) for i in range(3)],
    ],
)
def test_initialization(input_data):
    prog = Progression(order=input_data)
    assert len(prog) == len(input_data)
    for item in input_data:
        assert ID.get_id(item) in prog.order


def test_initialization_with_name():
    name = "test_progression"
    prog = Progression(name=name)
    assert prog.name == name


def test_contains(sample_progression, sample_elements):
    for element in sample_elements:
        assert element in sample_progression
        assert element.id in sample_progression


def test_not_contains(sample_progression):
    assert "non_existent_id" not in sample_progression
    assert MockElement(value="new") not in sample_progression


def test_list_conversion(sample_progression, sample_elements):
    prog_list = sample_progression.__list__()
    assert isinstance(prog_list, list)
    assert len(prog_list) == len(sample_elements)
    assert all(isinstance(item, UUID) for item in prog_list)


def test_len(sample_progression, sample_elements):
    assert len(sample_progression) == len(sample_elements)


@pytest.mark.parametrize(
    "index, expected_type",
    [
        (0, UUID),
        (slice(0, 2), Progression),
    ],
)
def test_getitem(sample_progression, index, expected_type):
    result = sample_progression[index]
    assert isinstance(result, expected_type)


def test_getitem_out_of_range(sample_progression):
    with pytest.raises(ItemNotFoundError):
        _ = sample_progression[len(sample_progression)]


def test_setitem(sample_progression):
    new_element = MockElement(value="new")
    sample_progression[0] = new_element
    assert sample_progression[0] == new_element.id


def test_setitem_slice(sample_progression):
    new_elements = [MockElement(value=f"new_{i}") for i in range(2)]
    sample_progression[0:2] = new_elements
    assert sample_progression[0] == new_elements[0].id
    assert sample_progression[1] == new_elements[1].id


def test_delitem(sample_progression):
    original_length = len(sample_progression)
    del sample_progression[0]
    assert len(sample_progression) == original_length - 1


def test_delitem_slice(sample_progression):
    original_length = len(sample_progression)
    del sample_progression[0:2]
    assert len(sample_progression) == original_length - 2


def test_iter(sample_progression, sample_elements):
    for prog_item, element in zip(sample_progression, sample_elements, strict=False):
        assert prog_item == element.id


def test_next(sample_progression, sample_elements):
    assert next(sample_progression) == sample_elements[0].id


def test_next_empty():
    empty_prog = Progression()
    with pytest.raises(StopIteration):
        next(empty_prog)


def test_clear(sample_progression):
    sample_progression.clear()
    assert len(sample_progression) == 0


@pytest.mark.parametrize(
    "input_item",
    [
        MockElement(value="new"),
        [MockElement(value="new1"), MockElement(value="new2")],
    ],
)
def test_append(sample_progression, input_item):
    original_length = len(sample_progression)
    sample_progression.append(input_item)
    assert len(sample_progression) > original_length

    if isinstance(input_item, list):
        for item in input_item:
            assert ID.get_id(item) in sample_progression
    else:
        assert ID.get_id(input_item) in sample_progression


def test_pop(sample_progression):
    original_length = len(sample_progression)
    popped_item = sample_progression.pop()
    assert len(sample_progression) == original_length - 1
    assert popped_item not in sample_progression


def test_pop_with_index(sample_progression):
    original_first_item = sample_progression[0]
    popped_item = sample_progression.pop(0)
    assert popped_item == original_first_item
    assert popped_item not in sample_progression


def test_pop_empty():
    empty_prog = Progression()
    with pytest.raises(ItemNotFoundError):
        empty_prog.pop()


@pytest.mark.parametrize(
    "input_item",
    [
        MockElement(value="new"),
        [MockElement(value="new1"), MockElement(value="new2")],
    ],
)
def test_include(sample_progression, input_item):
    original_length = len(sample_progression)
    sample_progression.include(input_item)
    assert len(sample_progression) > original_length

    if isinstance(input_item, list):
        for item in input_item:
            assert ID.get_id(item) in sample_progression
    else:
        assert ID.get_id(input_item) in sample_progression


@pytest.mark.parametrize(
    "input_item",
    [
        MockElement(value="new"),
        [MockElement(value="new1"), MockElement(value="new2")],
    ],
)
def test_exclude(sample_progression, input_item):
    sample_progression.include(input_item)
    original_length = len(sample_progression)
    sample_progression.exclude(input_item)
    assert len(sample_progression) < original_length

    if isinstance(input_item, list):
        for item in input_item:
            assert ID.get_id(item) not in sample_progression
    else:
        assert ID.get_id(input_item) not in sample_progression


def test_remove(sample_progression, sample_elements):
    to_remove = sample_elements[2]
    sample_progression.remove(to_remove)
    assert to_remove not in sample_progression


def test_remove_non_existent(sample_progression):
    with pytest.raises(ItemNotFoundError):
        sample_progression.remove("non_existent_id")


def test_popleft(sample_progression, sample_elements):
    first_element = sample_elements[0]
    popped = sample_progression.popleft()
    assert popped == first_element.id
    assert first_element not in sample_progression


def test_popleft_empty():
    empty_prog = Progression()
    with pytest.raises(ItemNotFoundError):
        empty_prog.popleft()


def test_concurrent_operations():
    prog = Progression()

    async def add_items():
        for _ in range(100):
            prog.append(MockElement(value=generate_random_string(10)))
            await asyncio.sleep(0.01)

    async def remove_items():
        for _ in range(50):
            if prog:
                prog.pop()
            await asyncio.sleep(0.02)

    async def run_concurrent():
        await asyncio.gather(add_items(), remove_items())

    asyncio.run(run_concurrent())
    assert 50 <= len(prog) <= 100


def test_progression_with_custom_elements():
    class CustomElement(Element):
        data: dict

    elements = [CustomElement(data={"value": i}) for i in range(5)]
    prog = Progression(order=elements)
    assert len(prog) == 5
    for element in elements:
        assert element in prog


def test_progression_serialization():
    prog = Progression(name="test_prog")
    serialized = prog.to_dict()
    deserialized = Progression.from_dict(serialized)
    assert deserialized == prog
    assert deserialized.name == prog.name


def test_progression_deep_copy():
    import copy

    prog = Progression(name="test_prog")
    prog_copy = copy.deepcopy(prog)
    assert prog == prog_copy
    assert prog is not prog_copy


def test_progression_memory_usage():
    import sys

    small_prog = Progression(order=[Element() for _ in range(10)])
    large_prog = Progression(order=[Element() for _ in range(10000)])
    small_size = sys.getsizeof(small_prog)
    large_size = sys.getsizeof(large_prog)
    assert large_size >= small_size
    assert large_size <= small_size * 1000


def test_progression_with_async_generator():
    import asyncio

    async def async_gen():
        for i in range(5):
            await asyncio.sleep(0.1)
            yield Element()

    async def run_test():
        p = Progression(order=[i async for i in async_gen()])
        assert len(p) == 5

    asyncio.run(run_test())


def test_progression_index_with_element():
    elements = [MockElement(value=i) for i in range(5)]
    p = Progression(order=elements)
    assert p.index(elements[2]) == 2


def test_progression_count_with_element():
    el1 = Element()
    el2 = Element()
    elements = [el1, el1, el1, el2, el2]
    p = Progression(order=elements)
    assert p.count(elements[1]) == 3


@pytest.mark.parametrize("method", ["append", "include"])
def test_progression_append_include_equivalence(method):
    p1 = Progression()
    p2 = Progression()

    for i in range(5):
        ele = Element()
        getattr(p1, method)(ele)
        getattr(p2, method)(ele)

    assert p1 == p2


def test_progression_remove_with_element():
    elements = [MockElement(value=i) for i in range(5)]
    p = Progression(order=elements)
    p.remove(elements[2])
    assert len(p) == 4
    assert elements[2].id not in p


def test_progression_memory_efficiency():
    import sys

    p = Progression(order=[Element() for _ in range(1000000)])

    memory_usage = sys.getsizeof(p) + sum(sys.getsizeof(item) for item in p.order)

    # Check if memory usage is reasonable (less than 100MB for 1 million elements)
    assert memory_usage < 100 * 1024 * 1024  # 100MB in bytes


def test_progression_serialization_advanced():
    import json

    class ComplexElement(Element):
        data: dict

    p = Progression(
        order=[ComplexElement(data={"value": i, "nested": {"x": i * 2}}) for i in range(5)]
    )

    serialized = p.model_dump_json()
    deserialized = Progression.from_dict(json.loads(serialized))

    assert len(deserialized) == 5
    assert all(isinstance(elem, UUID) for elem in deserialized)  # IDs are UUIDs
    assert p == deserialized

from typing import Any, Type

from lion_core.abc import Observable
from lion_core.generic import Pile as CorePile


class Pile(CorePile):
    """
    A sophisticated, ordered collection of Elements with hybrid list and dictionary-like access.

    The Pile class serves as a core container in the Lion framework, designed to efficiently
    store and manage collections of Element objects. It maintains both the order of items
    and allows fast access by unique identifiers, providing a versatile data structure
    for various use cases within the framework.

    Key Features:
    1. Hybrid Access: Supports both list-like (index/slice) and dict-like (key) access.
    2. Type Safety: Optionally enforces item type restrictions.
    3. Order Preservation: Maintains a specific order of items using a Progression.
    4. Flexible Inclusion/Exclusion: Provides methods for adding and removing items.
    5. Iteration Support: Allows iteration over items in their specified order.
    6. Set-like Operations: Supports operations like union and difference.
    7. Generic Typing: Uses Generic[T] for type hinting of contained items.

    Attributes:
        pile_ (dict[str, T]): Internal storage mapping identifiers to items.
        item_type (set[Type[Observable]] | None): Set of allowed item types.
        order (Progression): Progression maintaining the order of item identifiers.
        strict (bool): Flag to enforce strict type checking when item_type is defined.

    Type Parameters:
        T: The type of items stored in the Pile, must be a subclass of Observable.

    Usage:
        # Create a Pile with specific item type
        my_pile = Pile(item_type={CustomElement})

        # Add items
        my_pile.include(CustomElement())

        # Access items
        item = my_pile[0]  # By index
        item = my_pile['item_id']  # By ID

        # Iterate over items
        for item in my_pile:
            print(item)

    The Pile class is particularly useful in scenarios requiring:
    - Ordered storage of items with fast random access
    - Type-safe collections with flexible access patterns
    - Complex data structures needing both list and dict-like operations

    Methods:
        __init__(items: Any = None, item_type: set[Type[Observable]] | None = None,
                 order: Progression | list | None = None, strict: bool = False)
        __getitem__(key: Any) -> T | "Pile"
        __setitem__(key: Any, item: Any) -> None
        __contains__(item: Any) -> bool
        __len__() -> int
        __iter__() -> Iterable
        __str__() -> str
        __repr__() -> str
        __bool__() -> bool
        __list__() -> list
        __add__(other: T) -> Pile
        __sub__(other: Any) -> Pile
        __iadd__(other: T) -> Pile
        __isub__(other: Any) -> Pile
        __radd__(other: T) -> Pile
        keys() -> list
        values() -> list
        items() -> list[tuple[str, T]]
        get(key: Any, default=LN_UNDEFINED) -> T | "Pile" | None
        pop(key: Any, default=LN_UNDEFINED) -> T | "Pile" | None
        remove(item: T) -> None
        include(item: Any) -> None
        exclude(item: Any) -> None
        clear() -> None
        update(other: Any) -> None
        is_empty() -> bool
        size() -> int
        append(item: T) -> None
        insert(index: int, item: Any) -> None

    Notes:
        - The Pile class inherits from Element, providing a unique identifier,
          and Collective, defining it as a collection type.
        - It uses a combination of a dictionary (pile_) for fast access and a
          Progression (order) to maintain item order.
        - The class supports generic typing, allowing for type-safe collections
          of specific Observable subclasses.
        - When strict mode is enabled, only items of the exact specified type(s)
          are allowed; otherwise, subclasses are also accepted.

    Performance Considerations:
        - Access by ID (dict-like) is O(1), while access by index (list-like) is O(1) on average.
        - Inclusion and exclusion operations are generally O(1), but may be O(n) in worst-case
          scenarios involving order manipulation.
        - Iteration over items is efficient, yielding items in the specified order.

    Thread Safety:
        While basic read operations are thread-safe, concurrent modifications
        should be properly synchronized to ensure data consistency.

    See Also:
        Element: Base class providing core Lion framework functionality.
        Collective: Interface defining collection operations.
        Observable: Base class for items that can be stored in a Pile.
        Progression: Class used for maintaining item order.
    """


def pile(
    items: Any = None,
    item_type: Type[Observable] | set[Type[Observable]] | None = None,
    order: list[str] | None = None,
    strict: bool = False,
) -> Pile:
    return Pile(items=items, item_type=item_type, order=order, strict=strict)


__all__ = ["Pile", "pile"]

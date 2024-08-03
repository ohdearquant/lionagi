from lion_core.generic.progression import Progression as CoreProgression


class Progression(CoreProgression):
    """
    A sophisticated, ordered sequence container for managing and manipulating lists of items in the Lion framework.

    The Progression class is a fundamental component designed to maintain an ordered list
    of item identifiers (Lion IDs) while providing a comprehensive set of operations for
    manipulation and access. It seamlessly combines list-like functionality with
    Lion framework-specific features, offering a powerful tool for managing ordered data.

    Key Features:
    1. Ordered Storage: Maintains a strict order of item identifiers (Lion IDs).
    2. Hybrid Access: Supports both integer indexing and Lion ID-based operations.
    3. Rich Manipulation: Offers methods for adding, removing, and reordering items.
    4. Sequence Operations: Implements common sequence operations (len, iter, contain, etc.).
    5. Arithmetic Support: Enables combining progressions through arithmetic operations.
    6. Type Safety: Ensures all stored identifiers are valid Lion IDs.
    7. Named Progressions: Allows optional naming for easy identification.

    Attributes:
        name (str | None): Optional name for the progression. Useful for identification in complex systems.
        order (list[str]): The core ordered list of item identifiers (Lion IDs).

    Usage:
        import lionagi as li

        # Create a new Progression
        nodes = [Node() for _ in range(10)]
        p = li.prog(order=nodes[5], name='MyProgression')
        p1 = li.prog()
        p2 = p + p1

        # manipulate object items

        # add items in place
        p += nodes[3]           # Add a single item
        p += nodes[3].ln_id     # Add a single item by Lion ID
        p += nodes[4:7]         # Add a sequence of items

        p.append(item)       # treat input progression as its own object
        p.include(item)      # if not present, include, if present do nothing
        p.extend(item)       # treat input progression as its items
        p.insert(index, item)

        # Access items
        p[0]                    # use int
        p[1:3]                  # use slice
        p[-1:-5:-1]             # Negative indices are supported
        p.index(nodes[2])        # Get index of a specific item
        p.count(nodes[2])        # Count occurrences of a specific item

        # Remove items in place
        p.pop(index=None)           # Remove right end item if index is None
        p.popleft()                 # Remove left end item
        p.remove(nodes[2])          # Remove a specific item by object, remove first ocurrence
        p.exlcude(nodes[2])         # Remove a specific item by object, remove all ocurrences
        p -= nodes[2]               # Remove a specific item by object

        # others
        list(p)
        len(p)
        reversed(p)
        p.size()
        p.is_empty()

        # check is empty
        is p

        # iteration
        for i in p

        # check containment
        i in p


    Note:
        - The Progression class is designed to work seamlessly with other components
          of the Lion framework, particularly those that use Lion IDs for identification.
        - When working with Progression instances, it's important to understand that
          operations are performed on Lion IDs, not on the actual objects they represent.
        - The class provides both mutable (in-place) and immutable operations, allowing
          for flexible usage in different scenarios.

    Methods:
        __init__(order: list[str] | None = None, name: str | None = None)
        __contains__(item: Any) -> bool
        __list__() -> list[str]
        __len__() -> int
        __getitem__(key: int | slice) -> str | Progression
        __setitem__(key: int | slice, value: Any) -> None
        __delitem__(key: int | slice) -> None
        __iter__() -> Iterator[str]
        __next__() -> str
        __eq__(other: object) -> bool
        __bool__() -> bool
        __add__(other: Any) -> Progression
        __radd__(other: Any) -> Progression
        __iadd__(other: Any) -> Progression
        __isub__(other: Any) -> Progression
        __sub__(other: Any) -> Progression
        __repr__() -> str
        __str__() -> str
        size() -> int
        clear() -> None
        append(item: Any) -> None
        pop(index: int | None = None) -> str
        include(item: Any) -> None
        exclude(item: int | Any) -> None
        is_empty() -> bool
        __reverse__() -> Iterator[str]
        index(item: Any, start: int = 0, end: int | None = None) -> int
        remove(item: Any) -> None
        popleft() -> str
        extend(item: Progression | Any) -> None
        count(item: Any) -> int
        insert(index: int, item: Any) -> None

    Performance Characteristics:
        - Accessing by index: O(1)
        - Searching by Lion ID: O(n)
        - Appending: Amortized O(1)
        - Insertion/Deletion at arbitrary positions: O(n)
        - Iteration: O(n)

    Thread Safety:
        The Progression class is not inherently thread-safe. When using in a multi-threaded
        environment, external synchronization should be applied to ensure data consistency.

    Error Handling:
        - Invalid indices or Lion IDs raise appropriate exceptions (e.g., ItemNotFoundError).
        - Type mismatches (e.g., trying to extend with a non-Progression) raise LionTypeError.

    Customization:
        The _validate_order method can be overridden in subclasses to implement
        custom validation logic for the order of items.

    See Also:
        Element: Base class providing core Lion framework functionality.
        Ordering: Interface defining ordering operations.
        SysUtil: Utility class for Lion ID operations.
        ItemNotFoundError: Exception raised when an item is not found in the progression.
        LionTypeError: Exception raised for type-related errors.

    Warning:
        Modifying the 'order' attribute directly can lead to inconsistent state.
        Always use the provided methods to manipulate the progression's contents.
    """


prog = Progression


__all__ = ["Progression", "prog"]

# File: lionagi/os/generic/progression.py

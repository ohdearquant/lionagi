from typing import Any
from lion_core.generic.flow import Flow as CoreFlow

from lionagi.os.primitives.utils import core_to_lionagi_container


class Flow(CoreFlow):
    """
    Manages a collection of Progression instances within the Lion framework.

    The Flow class represents a dynamic sequence of Progression objects,
    providing a flexible structure for organizing and manipulating ordered
    data flows. It combines the functionality of both Component and Container
    base classes, offering a rich set of methods for progression management.

    Key Features:
    1. Multiple Progression Management: Stores and manages multiple
       Progression instances simultaneously.
    2. Registry System: Maintains a registry for quick access to progressions
       by name or ID.
    3. Default Progression: Supports a default progression for simplified
       operations.
    4. Flexible Item Handling: Allows adding, removing, and accessing items
       within progressions.
    5. Unique Item Tracking: Provides access to unique items across all
       progressions.
    6. Size and Length Operations: Offers methods to determine the total
       number of items and progressions.

    Attributes:
        progressions (Pile[Progression]): A collection of Progression
            instances, stored in a Pile for efficient management.
        registry (Note): A registry mapping progression names and IDs to
            their respective Progression instances.
        default_name (str): The name of the default progression, typically
            set to "main".

    Usage:
        flow = Flow()
        flow.include(item, "progression_name")
        item = flow.get("progression_name").popleft()
        flow.exclude(item, how="all")

    The Flow class is particularly useful in scenarios requiring:
    - Management of multiple, ordered data streams
    - Dynamic addition and removal of items in various progressions
    - Flexible access to items across different progressions
    - Tracking of unique items in a multi-progression environment

    Note:
        - The Flow class is designed to be thread-safe for basic operations.
        - It's recommended to use the `flow()` factory function for creating
          new Flow instances with default configurations.

    See Also:
        Progression: The individual progression units managed by Flow.
        Pile: The underlying storage mechanism for progressions.
        Component: Base class providing core functionality.
        Container: Interface defining container operations.
    """

    def _validate_sequences(self, value: Any):
        return core_to_lionagi_container(super()._validate_sequences(value))


flow = Flow

__all__ = ["Flow", "flow"]

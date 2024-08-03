from lion_core.generic.exchange import Exchange as CoreExchange


class Exchange(CoreExchange):
    """
    A sophisticated item exchange system for managing incoming and outgoing flows of Mail items within the Lion framework.

    The Exchange class provides a robust mechanism for handling the flow of Mail items,
    supporting both incoming and outgoing directions. It maintains separate collections
    for pending items, allowing for efficient management and tracking of item states.

    Key Features:
    1. Bidirectional Flow Management: Handles both incoming and outgoing item flows.
    2. Sender-based Organization: Groups incoming items by sender for easy access and management.
    3. Pending Item Tracking: Maintains separate collections for pending incoming and outgoing items.
    4. Type Safety: Ensures only Mail items are included in the exchange.
    5. Duplicate Prevention: Prevents the inclusion of duplicate items in the exchange.
    6. Efficient Item Lookup: Provides fast item presence checking.

    Attributes:
        pile (Pile[Mail]): A collection of all items currently in the exchange.
        pending_ins (dict[str, Progression]): A dictionary mapping sender IDs to
            Progression objects containing pending incoming items.
        pending_outs (Progression): A Progression object containing pending outgoing items.

    Usage:
        exchange = Exchange()
        mail_item = Mail(...)
        exchange.include(mail_item, direction="in")
        if mail_item in exchange:
            exchange.exclude(mail_item)

    The Exchange class is particularly useful in scenarios requiring:
    - Management of bidirectional communication flows
    - Tracking of pending items in a communication system
    - Grouping of incoming items by sender
    - Efficient inclusion and exclusion of items in a managed exchange

    Methods:
        include(item: Mail, direction: Literal["in", "out"]):
            Add a Mail item to the exchange in the specified direction.
        exclude(item: Mail):
            Remove a Mail item from all collections in the exchange.
        __contains__(item: Mail) -> bool:
            Check if a Mail item is present in the exchange.
        __bool__() -> bool:
            Return True if the exchange contains any items, False otherwise.
        __len__() -> int:
            Return the total number of items in the exchange.

    Properties:
        senders: List[str]
            Get a list of all sender IDs with pending incoming items.

    Notes:
        - The Exchange class inherits from both Element and Container, providing
          a unique identifier and container-like behavior.
        - All operations are designed to maintain consistency across the pile,
          pending_ins, and pending_outs collections.
        - The class enforces type safety by only allowing Mail objects to be included.

    Performance Considerations:
        - The use of Pile and Progression classes ensures efficient item management.
        - Sender-based organization of incoming items allows for quick access and updates.
        - The __contains__ method provides fast item presence checking.

    Thread Safety:
        While basic operations are generally thread-safe, concurrent modifications
        to the exchange should be properly synchronized to ensure data consistency.

    See Also:
        Mail: The type of items managed by the Exchange.
        Pile: The underlying storage mechanism for all items in the exchange.
        Progression: Used for managing ordered sequences of items.
        Element: Base class providing core Lion framework functionality.
        Container: Interface defining container operations.
    """

    pass


__all__ = ["Exchange"]

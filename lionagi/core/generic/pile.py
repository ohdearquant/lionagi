from typing import TypeVar, Type
from collections import deque
from ._abc import BaseComponent
from .sequence import CategorizedSequence

T = TypeVar("T", bound=BaseComponent)

class Pile:
    """
    Represents a buffer that records the IDs of BaseComponent items. 
    Only instances of BaseComponent or its subclasses are allowed.
    """
    
    def __init__(self, item_type: Type[BaseComponent] = BaseComponent) -> None:
        """
        Initializes the Pile with a specific item type.

        Args:
            item_type (Type[BaseComponent]): Type of the items to be stored. 
                Defaults to BaseComponent.
        """
        self.pile = {}
        self.item_type = item_type
        
    def _append(self, item: T) -> None:
        """
        Appends an item to the pile, ensuring it matches the specified item type.

        Args:
            item (T): Item to be appended, must be an instance of item_type.

        Raises:
            ValueError: If the item is not an instance of item_type.
        """
        if not isinstance(item, self.item_type):
            raise ValueError(f"Item must be a {self.item_type.__name__} instance.")
        self.pile[item.id_] = item

    def pop(self, item: T | str) -> T:
        """
        Removes and returns the item identified by its ID from the pile.

        Args:
            item (T | str): Item or item ID to be removed.

        Returns:
            T: The removed item, or None if not found.
        """
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        return self.pile.pop(item_id, None)

    def __len__(self) -> int:
        """
        Returns the number of items in the pile.

        Returns:
            int: Total count of items.
        """
        return len(self.pile)
    

class SequencedPile:
    """
    Manages a collection of items, supporting categorization and sequence tracking.
    Items are instances of BaseComponent or its subclasses.
    """
    
    def __init__(self, item_type: Type[T] = BaseComponent):
        """
        Initializes the SequencedPile with an item type for validation and storage.

        Args:
            item_type (Type[T]): The type of the items to be managed.
        """
        self.pile = Pile(item_type)
        self.sequence = deque()
        self.item_type = item_type
        self.categorized_sequence = CategorizedSequence()

    def _append(self, item: T, category: str = None):
        """
        Adds an item to the appropriate sequence or categorized sequence.

        Args:
            item (T): Item to be added.
            category (str, optional): Category for the item if specified.

        Raises:
            ValueError: If the item is not an instance of the specified item_type.
        """
        if not isinstance(item, self.item_type):
            raise ValueError(f"Item must be a {self.item_type.__name__} instance.")
        
        self.pile._append(item)
        if category:
            self.categorized_sequence.append(category, item.id_)
        else:
            self.sequence.append(item.id_)
        
    def popleft(self, category: str = None) -> T:
        """
        Removes and returns the first item from the specified category or sequence.

        Args:
            category (str, optional): Category from which to remove the item.

        Returns:
            T: The removed item.
        """
        item_id = self.categorized_sequence.popleft(category) if category else self.sequence.popleft()
        return self.pile.pop(item_id)
        
    def __len__(self) -> int:
        """
        Returns the total count of items across all sequences and categories.

        Returns:
            int: Total item count.
        """
        return len(self.sequence) + len(self.categorized_sequence)
        
    def __str__(self) -> str:
        """
        Returns a string representation of the current state of the buffer.

        Returns:
            str: Descriptive text about the buffer contents.
        """
        return (
            f"Buffer with {len(self.pile)} items, "
            f"{len(self.categorized_sequence)} categorized, and "
            f"{len(self.sequence)} uncategorized."
        )


class BiDirectionalPile:
    """
    Manages two collections of items using Pile instances, one for the left and one for the right.
    Allows items to be added to either side and uniquely identified across both piles.
    """

    def __init__(self, item_type: Type[BaseComponent] = BaseComponent):
        """
        Initializes the BiDirectionalPile with two separate piles.

        Args:
            item_type (Type[BaseComponent]): The type of the items stored in both piles.
                Defaults to BaseComponent.
        """
        self.left = Pile(item_type)
        self.right = Pile(item_type)

    def append(self, item: BaseComponent, to_left: bool = False):
        """
        Appends an item to the left or right pile based on the `to_left` flag.

        Args:
            item (BaseComponent): The item to be added to the pile.
            to_left (bool): Determines whether to add to the left pile (True) or
                the right pile (False). Defaults to False.
        """
        if to_left:
            self.left._append(item)
        else:
            self.right._append(item)
    
    @property
    def pile(self) -> dict:
        """
        Returns a combined dictionary of items from both left and right piles.

        Returns:
            dict: A dictionary containing all items from both piles.
        """
        return {**self.left.pile, **self.right.pile}
    
    def pop(self, item: str | BaseComponent):
        """
        Removes and returns the item from the left or right pile, based on its presence.

        Args:
            item (Union[str, BaseComponent]): The identifier or the item instance to be removed.

        Returns:
            BaseComponent: The removed item.

        Raises:
            ValueError: If the item identifier is not found in either pile.
        """
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        if item_id not in self.pile:
            raise ValueError(f"Item with id {item_id} not found in the pile.")

        if item_id in self.left.pile:
            return self.left.pop(item_id)
        return self.right.pop(item_id)
    
    def __len__(self) -> int:
        """
        Returns the total number of items in both the left and right piles.

        Returns:
            int: Combined count of items in both piles.
        """
        return len(self.left) + len(self.right)

from collections.abc import Mapping, Generator
from collections import deque
from typing import TypeVar

from pydantic import Field, field_validator
from .abc import BaseComponent


T = TypeVar("T", bound=BaseComponent)


class Pile(BaseComponent):
    """
    Represents a collection of items of lionagi kind in a dictionary.

    Attributes:
        pile (dict[str, T]): A dictionary of items in the pile {item.id_: item}.
        item_type (set[str] | None): The type of items that can be added to the pile.
            If None, any type is allowed.
    """

    pile: dict[str, T] = Field(
        default_factory=dict,
        description="A dictionary of items in the pile {item.id_: item}",
    )

    item_type: set[str] | None = Field(
        None,
        description="The type of items that can be added to the pile. "
                    "If None, any type is allowed.",
        frozen=True,
    )
   
    def append(self, item: T):
        """
        Appends an item to the pile.

        Args:
            item (T): The item to append.

        Raises:
            ValueError: If the item type is invalid.
        """
        if not isinstance(item, BaseComponent):
            raise ValueError("Invalid item type.")
        self.update(item)
    
    def items(self):
        """
        Returns an iterator over the items in the pile.

        Returns:
            iterator: An iterator over the items in the pile.
        """
        return self.pile.items()
    
    def get(self, item: T | str, default=False) -> T:
        """
        Retrieves an item from the pile by its ID or item instance.

        Args:
            item (T | str): The item or item ID to retrieve.
            default (Any): The default value to return if the item is not found.

        Returns:
            T: The retrieved item, or the default value if not found.
        """
        item_id = self._check_item_id(item)
        if default is not False:
            return self.pile.get(item_id, default)
        return self.pile.get(item_id)

    def update(self, other: dict[str, T]):
        """
        Updates the pile with items from another dictionary.

        Args:
            other (dict[str, T]): The dictionary of items to update the pile with.
        """
        _pile = self.validate_pile(other)
        self.pile.update(_pile)

    def pop(self, item: T | str, default=False) -> T:
        """
        Removes and returns an item from the pile by its ID or item instance.

        Args:
            item (T | str): The item or item ID to remove and return.
            default (Any): The default value to return if the item is not found.

        Returns:
            T: The removed item, or the default value if not found.
        """
        item_id = self._check_item_id(item)
        if default is not False:
            return self.pile.pop(item_id, default)
        return self.pile.pop(item_id)

    @field_validator("item_type", mode="before")
    def validate_item_type(cls, value):
        """
        Validates the item type field.

        Args:
            value (Any): The value to validate.

        Returns:
            set[str] | None: The validated item type set, or None if no item type is specified.

        Raises:
            TypeError: If the item type is invalid or duplicated.
        """
        if value is None:
            return None
    
        # if it is a string, it must be a lionagi object class name
        # we don't check if it is not when passing in str
        if isinstance(value, str) and "lionagi." in value:
            return {value}

        if isinstance(value, (tuple, list, set, Generator)):
            value = list(value)
        
        if isinstance(value, Mapping):
            value = list(value.values())
        
        if not isinstance(value, list):
            if isinstance(value, type(BaseComponent)):
                return {value.class_name()}
        
        for i in value:
            if not isinstance(i, type(BaseComponent)):
                raise TypeError("Invalid item type.")

        if len(value) != len(set(value)):
            raise TypeError("Detected duplicated item types in item_type.")
        
        if len(value) > 0:
            return set(value)
            

    @field_validator("pile", mode="before")
    def validate_pile(cls, value):
        """
        Validates the pile field.

        Args:
            value (Any): The value to validate.

        Returns:
            dict[str, T]: The validated pile dictionary.

        Raises:
            ValueError: If the pile value is invalid or contains an invalid item type.
        """
        if isinstance(value, BaseComponent):
            value = [value]
        elif isinstance(value, (tuple, list, set, Generator, deque)):
            value = list(value)
        elif isinstance(value, Mapping):
            value = list(value.values())

        if getattr(cls, "item_type", None) is not None:
            for i in value:
                if type(i) in cls.item_type:
                    continue
                else:
                    raise ValueError(f"Invalid item type in pile. Expected {cls.item_type}")

        if isinstance(value, list):
            return {i.id_: i for i in value}
        
        raise ValueError("Invalid pile value")

    def _check_item_id(self, item: T | str) -> str:
        """
        Checks if the item or item ID exists in the pile.

        Args:
            item (T | str): The item or item ID to check.

        Returns:
            str: The item ID.

        Raises:
            ValueError: If the item is not found in the pile.
        """
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        if item_id not in self.pile:
            raise ValueError(f"Item {item_id} not found in pile.")
        return item_id

    def __getitem__(self, item: str) -> T:        
        """
        Retrieves an item from the pile by its ID.

        Args:
            item (str): The item ID to retrieve.

        Returns:
            T: The retrieved item, or None if not found.
        """
        return self.pile.get(item)

    def __iter__(self):
        """
        Returns an iterator over the items in the pile.

        Returns:
            iterator: An iterator over the items in the pile.
        """
        return iter(self.pile.values())
    
    def __contains__(self, x):
        """
        Checks if an item or item ID is present in the pile.

        Args:
            item (Any): The item or item ID to check.

        Returns:
            bool: True if the item or item ID is present in the pile, False otherwise.
        """
        if isinstance(x, BaseComponent):
            return x in self.pile.values()
        return x in self.pile
    
    def __len__(self) -> int:
        """
        Returns the number of items in the pile.

        Returns:
            int: The number of items in the pile.
        """
        return len(self.pile)
    

class SequentialPile(Pile):
    """
    Represents a sequential pile of items.

    Attributes:
        sequence (deque): The sequence of item IDs in the pile.
    """
    
    sequence: deque = Field(
        default_factory=deque,
        description="The sequence of item IDs in the pile.",
    )

    def append(self, item: T):
        """
        Appends an item to the end of the sequencial pile.

        Args:
            item (T): The item to append.
        """
        super().append(item)
        self.sequence.append(item.id_)
    
    def popleft(self) -> T:
        """
        Removes and returns the leftmost item from the pile.

        Returns:
            T: The removed item.
        """
        item_id = self.sequence.popleft()
        return self.pop(item_id)
    
    def pop(self, item: T | str, default=False) -> T:
        """
        Removes and returns an item from the pile by its ID or item instance.

        Args:
            item (T | str): The item or item ID to remove and return.
            default (Any): The default value to return if the item is not found.

        Returns:
            T: The removed item, or the default value if not found.
        """
        item_id = self._check_item_id(item)
        self.sequence.remove(item_id)
        return super().pop(item_id, default)
    
    
class BiDirectionalPile(Pile):
    """
    Represents a bidirectional pile of items.

    Attributes:
        left (list[str]): The list of item IDs on the left side of the pile.
        right (list[str]): The list of item IDs on the right side of the pile.
    """
    
    left: list[str] = Field(
        default_factory=list, 
        title="Left side of the pile"
    )

    right: list[str] = Field(
        default_factory=list, 
        title="Right side of the pile"
    )

    def append(self, item: BaseComponent, to_left: bool = False):
        """
        Appends an item to the pile, either to the left or right side.

        Args:
            item (BaseComponent): The item to append.
            to_left (bool): If True, appends the item to the left side of the pile.
                If False (default), appends the item to the right side of the pile.
        """
        super().append(item)
        if to_left:
            self.left.append(item.id_)
        else:
            self.right.append(item.id_)
    
    def pop(self, item: str | BaseComponent, default=False):
        """
        Removes and returns an item from the pile by its ID or item instance.

        Args:
            item (str | BaseComponent): The item ID or item instance to remove and return.
            default (Any): The default value to return if the item is not found.

        Returns:
            BaseComponent: The removed item, or the default value if not found.
        """
        item = super().pop(item, default)
        if item is not False and item != default:
            item_id = item.id_ if isinstance(item, BaseComponent) else item
            if item_id in self.left:
                self.left.remove(item_id)
            elif item_id in self.right:
                self.right.remove(item_id)
        return item



class MultiSequence(BaseComponent):
    """
    Represents a multi-sequence of items.

    Attributes:
        sequence (dict[str, deque]): The dictionary of sequences, where each key represents
            a category and the corresponding value is a deque of item IDs.
    """
    
    sequence: dict[str, deque] = Field(default_factory=dict,)
        
    def items(self):
        """
        Returns an iterator over the items in the multi-sequence.

        Returns:
            iterator: An iterator over the items in the multi-sequence.
        """
        return self.sequence.items()
    
    def append(self, key: str, item):
        """
        Appends an item to the specified sequence category.

        Args:
            key (str): The category key.
            item (Any): The item to append.
        """
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        if not key in self.sequence:
            self.sequence[key] = deque()
        self.sequence[key].append(item_id)
        
    def __iter__(self):
        """
        Returns an iterator over the category keys in the multi-sequence.

        Returns:
            iterator: An iterator over the category keys.
        """
        return iter(self.sequence.keys())

    def __getitem__(self, key: str) -> deque | None:
        """
        Retrieves the sequence for the specified category key.

        Args:
            key (str): The category key.

        Returns:
            deque | None: The sequence for the specified category key, or None if not found.
        """
        return self.sequence.get(key, None)

    def __setitem__(self, category: str, items: deque):
        """
        Sets the sequence for the specified category key.

        Args:
            category (str): The category key.
            items (deque): The sequence of items to set.
        """
        self.sequence[category] = items

    def __len__(self) -> int:
        """
        Returns the total number of items in the multi-sequence.

        Returns:
            int: The total number of items in the multi-sequence.
        """
        return sum(len(items) for items in self.sequence.values())
        
        
class MultiSequencdPile(Pile):
    """
    Represents a pile with multiple sequences.

    Attributes:
        mseq (MultiSequence): The multi-sequence associated with the pile.
    """

    mseq: MultiSequence = Field(
        default_factory=MultiSequence,
        description="The multi-sequence associated with the pile.",
    )

    def append(self, key: str, item: T):
        super().append(item)
        self.mseq.append(key, item)
    
    def pop(self, item: T | str):
        item = super().pop(item)
        for key, seq in self.mseq.items():
            if item.id_ in seq:
                seq.remove(item.id_)
                self.mseq[key] = seq
        return item

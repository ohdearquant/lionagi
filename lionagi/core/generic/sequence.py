from collections import deque

class CategorizedSequence:
    """
    Represents a collection where items are grouped under categories.
    Each category maps to a sequence of item identifiers.
    """

    def __init__(self, sequence: dict[str, deque] = None) -> None:
        """
        Initializes the CategorizedSequence with an optional dictionary of deques.
        
        Args:
            sequence (dict[str, deque], optional): A dictionary mapping category
                keys to deques of item identifiers. Defaults to an empty dictionary.
        """
        self.sequence = sequence or {}
        
    @property
    def unique_categories(self) -> set:
        """
        Returns a set of unique category keys in the sequence.

        Returns:
            set: A set of category strings.
        """
        return set(self.sequence.keys())
    
    def __len__(self) -> int:
        """
        Returns the total number of items across all categories.

        Returns:
            int: Total count of items.
        """
        return sum(len(items) for items in self.sequence.values())
    
    def append(self, category: str, item: str) -> None:
        """
        Appends an item to the deque corresponding to the given category. 
        Creates a new deque if the category does not exist.

        Args:
            category (str): Category under which the item is categorized.
            item (str): Item identifier to be appended.
        """
        if category not in self.sequence:
            self.sequence[category] = deque()
        self.sequence[category].append(item)
        
    def popleft(self, category: str):
        """
        Removes and returns an item from the front of the deque for the specified category.
        Returns None if the deque is empty or the category does not exist.

        Args:
            category (str): Category from which an item should be removed.

        Returns:
            str | None: The item identifier if available, None otherwise.
        """
        try:
            return self.sequence[category].popleft()
        except (KeyError, IndexError):
            return None

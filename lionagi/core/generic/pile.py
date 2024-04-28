from typing import TypeVar, Type
from collections import deque
from .sequence import CategorizedSequence
from .abc import BaseComponent


T = TypeVar("T")

class Pile:
    
    """
    represents a buffer that record the ids lionagi items in it. 
    Item must be a BaseComponent hierarchy instance.
    """
    
    def __init__(self, item_type=BaseComponent) -> None:
        self.pile = {}
        self.item_type = item_type
        
    def _append(self, item: BaseComponent) -> None:
        if not isinstance(item, self.item_type):
            raise ValueError("Item must be a BaseComponent instance.")
        self.pile[item.id_] = item

    def pop(self, item: str | BaseComponent) -> BaseComponent:
        item = item.id_ if isinstance(item, BaseComponent) else item
        return self.pile.pop(item, None)

    def __len__(self):
        return len(self.pile)
    

class SequencedPile:
    
    """
    a buffer that keeps track of the sequence of items in it. The items can be categorized.
    """
    
    def __init__(self, item_type: Type[BaseComponent] = None):
        self.pile = Pile(item_type)
        self.sequence = deque()
        self.item_type = BaseComponent
        self.categorized_sequence = CategorizedSequence(item_type)

    def _append(self, item: BaseComponent, category=None):
        if not isinstance(item, self.item_type):
            raise ValueError(f"Item must be a {self.item_type} instance.")
        
        self.pile._append(item)
        if category:
            self.categorized_sequence.append(category, item.id_)
        else:
            self.sequence.append(item.id_)
        
    def popleft(self, category=None):
        item = (
            self.categorized_sequence.popleft(category) if category 
            else self.sequence.popleft()
        )
        return self.sequence.popleft(item)
        
    def __len__(self):
        return len(self.sequence) + len(self.categorized_sequence)
        
    def __str__(self) -> str:
        return (
            f"Buffer with {len(self.pile)} sequenced items, {len(self.categorized_sequence)} categorized and {len(self.sequence)} uncategorized"
        )


class BiDirectionalPile:
    
    def __init__(self, item_type=BaseComponent):
        self.left = Pile(item_type)
        self.right = Pile(item_type)

    def _append(self, item: BaseComponent, left=False):
        if left:
            self.left._append(item)
        else:
            self.right._append(item)
    
    @property
    def pile(self):
        return {**self.left.pile, **self.right.pile}
    
    def pop(self, item: str | BaseComponent):
        item = item.id_ if isinstance(item, BaseComponent) else item
        if item not in self.pile:
            raise ValueError(f"Item with id {item} not found in the pile.")
        if item in self.left.pile:
            return self.left.pop(item)
        return self.right.pop(item)
    
    def __len__(self):
        return len(self.left) + len(self.right)
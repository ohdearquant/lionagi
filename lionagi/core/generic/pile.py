from collections import deque
from typing import TypeVar, Type
from .abc import BaseComponent

T = TypeVar("T", bound=BaseComponent)


class Pile:
    """represents a collection of items of lionagi kind in a dictionary"""
    
    def __init__(self, items = []):
        self.pile = {item.id_: item for item in items} if items else {}
    
    def items(self):
        return self.pile.items()
    
    def get(self, item: T | str) -> T:
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        return self.pile.get(item_id, None)
    
    def append(self, item: T) -> None:
        self.pile[item.id_] = item

    def pop(self, item: T | str) -> T:
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        return self.pile.pop(item_id, None)
    
    def __getitem__(self, item: str) -> T:
        return self.pile.get(item, None)
    
    def __setitem__(self, item: str, value: T):
        self.pile[item] = value
    
    def __iter__(self):
        return iter(self.pile.values())
    
    def __contains__(self, x):
        return x in self.pile or x in self.pile.values()
    
    def __len__(self) -> int:
        return len(self.pile)
    

class BiDirectionalPile(Pile):

    def __init__(self, items = []):
        super().__init__(items)
        self.left = []
        self.right = []

    def append(self, item: BaseComponent, to_left: bool = False):
        super().append(item)
        if to_left:
            self.left.append(item.id_)
        else:
            self.right.append(item.id_)
    
    def pop(self, item: str | BaseComponent):
        item = super().pop(item)
        if item.id_ in self.left:
            self.left.remove(item.id_)
        elif item.id_ in self.right:
            self.right.remove(item.id_)
        return item


class SequentialPile(Pile):
    
    def __init__(self, items = []):
        super().__init__(items)
        self.sequence: deque = deque()

    def append(self, item: T):
        super().append(item)
        self.sequence.append(item.id_)
    
    def popleft(self) -> T:
        item_id = self.sequence.popleft()
        return self.pop(item_id)
    
    def pop(self, item: T | str) -> T:
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        self.sequence.remove(item_id)
        return super().pop(item_id)


class MultiSequence:
    
    def __init__(self, sequence: dict[str, deque] = None) -> None:
        self.sequence = sequence or {}
        
    def items(self):
        return self.sequence.items()
    
    def append(self, key: str, item):
        item_id = item.id_ if isinstance(item, BaseComponent) else item
        if not key in self.sequence:
            self.sequence[key] = deque()
        self.sequence[key].append(item_id)
        
    def __iter__(self):
        return iter(self.sequence.keys())

    def __getitem__(self, key: str) -> deque | None:
        return self.sequence.get(key, None)

    def __setitem__(self, category: str, items: deque):
        self.sequence[category] = items

    def __len__(self) -> int:
        return sum(len(items) for items in self.sequence.values())
        
        
class MultiSequencialPile(Pile):

    def __init__(self, items = [], sequence=None):
        super().__init__(items)
        self.mseq = sequence or MultiSequence()
    
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

from .abc import Sequence, BaseComponent
from collections import deque


class CategorizedSequence(Sequence):
    """represents a dictionary of sequences of item ids categorised by a key"""
    
    def __init__(self, sequence: dict[str, deque]=None) -> None:
        self.sequence=sequence or {}
        
    @property
    def _unique_categories(self):
        return set(self.sequence.keys())
    
    def __len__(self):
        return sum([len(v) for v in self.sequence.values()])
    
    def append(self, category: str=None, item: str | BaseComponent=None):
        if category not in self.sequence:
            self.sequence[category] = deque()
            
        self.sequence[category].append(item if isinstance(item, str) else item.id_)
        
    def popleft(self, category: str):
        try:
            return self.sequence[category].popleft()
        except IndexError:
            return None

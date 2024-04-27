from .abc import Sequence
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
    
    def append(self, category: str=None, item: any=None):
        self.sequence[category] = self.sequence.get(category, deque())
        self.sequence[category].append(item)
        
    def popleft(self, category: str):
        try:
            return self.sequence[category].popleft()
        except IndexError:
            return None

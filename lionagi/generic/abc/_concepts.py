from abc import ABC, abstractmethod


class Record(ABC):
    """represents concretized information"""

    @abstractmethod
    def items(self):
        ...
        
        
class Ordering(ABC):
    """represents a sequence of certain order"""
    
    @abstractmethod
    def __len__(self):
        ...
    
    @abstractmethod
    def __iter__(self):
        ...
    
    @abstractmethod
    def __next__(self):
        ...
    
    @abstractmethod
    def __contains__(self, item):
        ...
    

class Condition(ABC):
    """represents a situation"""

    @abstractmethod
    async def applies(self, value, /, *args, **kwargs) -> any:
        ...
    

class Action(ABC):
    """represents a purposed process"""

    @abstractmethod
    async def invoke(self, /, *args, **kwargs) -> any:
        ...
    
    
class Workable(ABC):
    """represents a processable entity"""

    @abstractmethod
    async def perform(self, /, *args, **kwargs) -> any:
        ...

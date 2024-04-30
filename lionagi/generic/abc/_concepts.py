from abc import ABC, abstractmethod


class Record(ABC):
    """represents concretized information"""

    @abstractmethod
    def items(self):
        ...
        
    @abstractmethod
    def __len__(self):
        ...
    
    @abstractmethod
    def __getitem__(self, key):
        ...
        
    @abstractmethod
    def __setitem__(self, key, value):
        ...
        
    @abstractmethod
    def __contains__(self, key):
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
    async def apply(self, value, /, *args, **kwargs) -> any:
        pass
    

class Action(ABC):
    """represents a purposed process"""

    @abstractmethod
    async def invoke(self, /, *args, **kwargs) -> any:
        pass
    
    
class Workable(ABC):
    """represents a processable entity"""

    @abstractmethod
    async def perform(self, /, *args, **kwargs) -> any:
        ...

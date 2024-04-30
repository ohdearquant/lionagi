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
    """represents sequence of specific order"""
    
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
    """represents a process"""

    @abstractmethod
    async def perform(self, /, *args, **kwargs) -> any:
        pass
    
    
class Workable(ABC):
    """represents an entity that can be processed on"""

    @abstractmethod
    @property
    def is_workable(self) -> bool:
        ...


class Executor(ABC):
    """represents an entity that can carry through situations"""

    @abstractmethod
    async def execute(self, /, *args, **kwargs) -> any:
        pass
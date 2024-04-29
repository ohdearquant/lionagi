from ..generic.abc import BaseComponent
from abc import abstractmethod, ABC

class Rule(BaseComponent):
    
    def __init__(self, fields=[], validation_kwargs={}, fix=False, strict=True) -> None:
        self.fields = fields
        self.validation_kwargs = validation_kwargs
        self.fix = fix
        self.strict=strict

    def applies_to(self, field):
        if field not in self.fields:
            return False
        return True

    @abstractmethod
    async def apply(self, value, strict, **kwargs):
        pass
    
    @classmethod
    def name(cls):
        return cls.__name__
from ..generic.pile import MultiSequencialPile, MultiSequence


class Validator:
    
    DEFAULT_NAME = "default"
    
    def __init__(self, rules=None, flows=None, strict=True) -> None:
        self.rules = rules or MultiSequencialPile()
        self.flows = flows or MultiSequence()
        self.strict: bool = strict
    
    
    async def validate(self, field, value, *args, flow_name=None, strict=None, **kwargs):
        flow_name = flow_name or self.DEFAULT_NAME
        _flow: list[str] = list(self.flows[flow_name])
        
        if not _flow:
            if strict:
                raise ValueError(f"no rules found for flow: {flow_name}")
            return value
    
        for i in _flow:
            if self.rules[i].applies_to(field):
                try:
                    if (a:= await self.rules[i].apply(value, *args, **kwargs)) is not None:
                        return a
                except Exception as e:
                    if strict or self.strict:
                        raise ValueError(f"failed to validate field: {field}") from e
        
        if strict or self.strict:
            raise ValueError(f"failed to validate field: {field}")
        return value
    
    
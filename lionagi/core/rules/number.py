from lionagi.libs.ln_convert import to_num
from ..generic.abc import Rule

class NumberRule(Rule):
    
    """
    upper_bound: int | float | None = None,
    lower_bound: int | float | None = None,
    num_type: Type[int | float] = float,
    precision: int | None = None,
    """

    async def apply(self, value):
        return await self._apply(value, (int, float))
    
    async def fix_field(self, value):

        value = to_num(value, **self.validation_kwargs)
        if isinstance(value, (int, float)):
            return value
        raise ValueError(f"Failed to convert {value} into a numeric value")
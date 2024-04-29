from lionagi.libs.ln_convert import to_str
from ..generic.abc import Rule


class StringRule(Rule):

    async def apply(self, value):
        return await self._apply(value, (str,))

    async def fix_field(self, value):
        try:
            return to_str(value, **self.validation_kwargs)
        except Exception as e:
            raise ValueError(f"Failed to convert {value} into a string value") from e
        
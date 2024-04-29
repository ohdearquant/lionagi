from lionagi.libs.ln_convert import to_str, strip_lower
from ..generic.abc import Rule


class BooleanRule(Rule):

    async def apply(self, value):
        return await self._apply(value, (bool,))

    async def fix_field(self, value):

        value = strip_lower(to_str(value))
        if value in ["true", "1", "correct", "yes"]:
            return True

        elif value in ["false", "0", "incorrect", "no", "none", "n/a"]:
            return False

        raise ValueError(f"Failed to convert {value} into a boolean value")
    
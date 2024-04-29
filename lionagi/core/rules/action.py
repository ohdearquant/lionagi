from enum import Enum

from lionagi.libs import ParseUtil
from lionagi.libs.ln_convert import to_list, to_dict
from .mapping import MappingRule


class ActionRequestKeys(Enum):
    FUNCTION = "function"
    ARGUMENTS = "arguments"
    
    
class ActionRequestRule(MappingRule):
    
    def __init__(self, fields=[], validation_kwargs={"discard": False}, fix=False, strict=True):
        super().__init__(fields, validation_kwargs, fix, strict, keys=ActionRequestKeys)

    async def fix_field(self, value):
        corrected = []
        if isinstance(value, str):
            value = ParseUtil.fuzzy_parse_json(value)

        try:
            value = to_list(value)
            for i in value:
                i = to_dict(i)
                if list(i.keys()) >= ["function", "arguments"]:
                    corrected.append(i)
                elif not self.validation_kwargs.get("discard", None):
                    raise ValueError(f"Invalid action field: {i}")
        except Exception as e:
            raise ValueError(f"Invalid action field: {e}") from e

        return corrected
    
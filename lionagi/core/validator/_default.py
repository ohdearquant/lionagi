from enum import Enum
from collections.abc import Mapping
from lionagi.libs import StringMatch, convert, ParseUtil
from .rule import Rule
from .util import _validate_keys


class ChoiceRule(Rule):
    """
    Enforces that the field value is among provided choices.

    Parameters:
        fields (list): Fields to apply the rule.
        validation_kwargs (dict): Dictionary of validation arguments.
        fix (bool): If True, tries to auto-correct the field if validation fails.
        choices (Iterable, optional): Valid choices for the field values.

    Raises:
        ValueError: If choices are not provided during initialization or validation.
    """
    
    def __init__(self, fields=None, validation_kwargs=None, fix=False, strict=True, choices=None):
        super().__init__(fields or [], validation_kwargs or {}, fix, strict)
        self.choices = self._validate_choices(choices)
        
    def _validate_choices(self, choices):
        if not choices:
            if not 'choices' in self.validation_kwargs:
                raise ValueError(f"choices not provided")
            choices = self.validation_kwargs['choices']
        
        try:
            choices = _validate_keys(choices)
        except Exception as e:
            raise ValueError(f"failed to get choices") from e
        
        return choices

    async def apply(self, value, choices=None, fix=None, **kwargs):
        choices = choices or self.choices
        if not choices:
            raise ValueError(f"choices not provided")
        
        same_dtype, dtype_ = convert.is_same_dtype(choices, return_dtype=True)
        if not same_dtype:
            raise ValueError(
                f"Field type ENUM requires all choices to be of the same type, got {choices}"
            )

        if not isinstance(value, dtype_):
            raise ValueError(
                f"Default value for ENUM must be an instance of the {dtype_.__name__}, got {type(value).__name__}"
            )
        
        if value not in self.choices:
            if not fix and not self.fix:
                raise ValueError(f"invalid value {value} not in {self.choices}")
            else:
                value = await self.fix_field(value)
        
        return value
    
    async def fix_field(self, value):
        return StringMatch.choose_most_similar(value, self.choices)


class MappingRule(Rule):
    """
    check whether the value is a dictionary with correct keys
    
    validation_kwargs:
        keys: Iterable | Mapping | None = None,  keys to check against
    """

    def __init__(self, fields=[], validation_kwargs={}, fix=False, strict=True, keys=None) -> None:
        super().__init__(fields, validation_kwargs, fix, strict)
        self.keys = self._validate_keys(keys)

    def _validate_keys(self, keys):
        if not keys:
            if not 'keys' in self.validation_kwargs:
                raise ValueError(f"keys not provided")
            keys = self.validation_kwargs['keys']
        
        try:
            keys = _validate_keys(keys)
        except Exception as e:
            raise ValueError(f"failed to get keys") from e
        
        return keys
    
    
    async def apply(self, value, keys=None, fix=None, **kwargs):
        if not keys and not self.keys:
            raise ValueError("Correct mapping keys must be provided.")
        
        keys = self._validate_keys(keys) if keys else self.keys

        if not fix and not self.fix:
            if not isinstance(value, Mapping):
                raise ValueError("Invalid mapping field type.")
            check_keys = set(value.keys())
            if check_keys != set(keys):
                raise ValueError(f"Invalid mapping keys. Current keys {[i for i in check_keys]} != {keys}")

        if fix or self.fix:
            return await self.fix_field(value)
        
    async def fix_field(self, value):
        if not isinstance(value, dict):
            try:
                value = convert.to_dict(value)
            except Exception as e:
                raise ValueError("Invalid dict field type.") from e

        if isinstance(value, dict):
            check_keys = set(value.keys())
            if check_keys != set(self.keys):
                try:
                    return StringMatch.force_validate_dict(value, keys=self.keys)
                except Exception as e:
                    raise ValueError("Invalid dict keys.") from e
            return value


class NumberRule(Rule):
    
    """
    upper_bound: int | float | None = None,
    lower_bound: int | float | None = None,
    num_type: Type[int | float] = float,
    precision: int | None = None,
    """

    async def apply(self, value, fix=None, **kwargs):
        if not isinstance(value, (int, float)):
            if not fix and not self.fix:
                raise ValueError(
                    f"NUMERIC field must be an int or float, got {type(value).__name__}"
                )
        if fix or self.fix:
            return await self.fix_field(value)
        return value
        
    
    async def fix_field(self, value):

        value = convert.to_num(value, **self.validation_kwargs)
        if isinstance(value, (int, float)):
            return value
        raise ValueError(f"Failed to convert {value} into a numeric value")


class BooleanRule(Rule):

    async def apply(self, value, fix=None, **kwargs):
        if not isinstance(value, bool):
            if fix:
                try:
                    return await self.fix_field(value)
                except Exception as e:
                    raise e

            raise ValueError(
                f"Default value for BOOLEAN must be a bool, got {type(value).__name__}"
            )
        return value

    async def fix_field(self, value):

        value = convert.strip_lower(convert.to_str(value))
        if value in ["true", "1", "correct", "yes"]:
            return True

        elif value in ["false", "0", "incorrect", "no", "none", "n/a"]:
            return False

        raise ValueError(f"Failed to convert {value} into a boolean value")
    
    
class StringRule(Rule):

    async def apply(self, value, fix=None, **kwargs):
        if not isinstance(value, str):
            if fix or self.fix:
                try:
                    return self.fix_field(value)
                except Exception as e:
                    raise e

            raise ValueError(
                f"Default value for STRING must be a str, got {type(value).__name__}"
            )
        return value


    async def fix_field(self, value):
        try:
            return convert.to_str(value, **self.validation_kwargs)
        except Exception as e:
            raise ValueError(f"Failed to convert {value} into a string value") from e
        
        
class ActionRequestKeys(Enum):
    FUNCTION = "function"
    ARGUMENTS = "arguments"
    


class ActionRequestRule(MappingRule):
    
    def __init__(self, fields=[], validation_kwargs={"discard": False}, fix=False, strict=True):
        super().__init__(fields, validation_kwargs, fix, strict, keys=ActionRequestKeys)

    def fix_field(self, value):
        corrected = []
        if isinstance(value, str):
            value = ParseUtil.fuzzy_parse_json(value)

        try:
            value = convert.to_list(value)
            for i in value:
                i = convert.to_dict(i)
                if list(i.keys()) >= ["function", "arguments"]:
                    corrected.append(i)
                elif not self.validation_kwargs.get("discard", None):
                    raise ValueError(f"Invalid action field: {i}")
        except Exception as e:
            raise ValueError(f"Invalid action field: {e}") from e

        return corrected
    

class DEFAULT_RULES(Enum):
    CHOICE = ChoiceRule
    MAPPING = MappingRule
    NUMBER = NumberRule
    BOOL = BooleanRule
    STR = StringRule
    ACTION = ActionRequestRule

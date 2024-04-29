from pydantic import Field, field_validator
from lionagi.libs.ln_convert import is_same_dtype
from lionagi.libs.ln_parse import StringMatch
from lionagi.libs.ln_validate import validate_keys
from ..generic.abc import Rule


class ChoiceRule(Rule):
    
    keys: list = Field(
        default_factory=list, description="List of choices to choose from",
        validation_alias="choices"
    )
    
    @field_validator("keys", mode="before")
    def _validate_choices(cls, value):
        if not value:
            if not 'keys' in cls.validation_kwargs and not "choices" in cls.validation_kwargs:
                raise ValueError(f"keys not provided")
            
            value = cls.validation_kwargs.get(
                "keys", cls.validation_kwargs.get("choices", None))

        try:
            value = validate_keys(value)
            
        except Exception as e:
            raise ValueError(f"failed to get keys") from e
        
        return value

    async def apply(self, value):        
        same_dtype, dtype_ = is_same_dtype(self.keys, return_dtype=True)
        if not same_dtype:
            raise ValueError(
                f"Field type ENUM requires all choices to be of the same type, got {self.keys}"
            )

        if not isinstance(value, dtype_):
            raise ValueError(
                f"Default value for ENUM must be an instance of the {dtype_.__name__}, got {type(value).__name__}"
            )
        
        if value not in self.keys:
            if not self.fix:
                raise ValueError(f"invalid value {value} not in {self.keys}")
            else:
                value = await self.fix_field(value)
        
        return value
    
    async def fix_field(self, value):
        return StringMatch.choose_most_similar(value, self.keys)
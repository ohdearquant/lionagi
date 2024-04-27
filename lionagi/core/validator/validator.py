from pydantic import BaseModel, Field
from .rule import DEFAULT_RULES, Rule


rules_ = {
    "choice": DEFAULT_RULES.CHOICE.value,
    "actionrequest": DEFAULT_RULES.ACTION_REQUEST.value,
    "bool": DEFAULT_RULES.BOOL.value,
    "number": DEFAULT_RULES.NUMBER.value,
    "dict": DEFAULT_RULES.DICT.value,
    "str": DEFAULT_RULES.STR.value,
}

order_ = [
    "choice",
    "actionrequest",
    "bool",
    "number",
    "dict",
    "str",
]


class Validator(BaseModel):
    """
    rules contain all rules that this validator can apply to data
    the order determines which rule gets applied in what sequence.
    notice, if a rule is not present in the orders, it will not be applied.
    """

    rules: dict[str, Rule] = Field(
        default=rules_,
        description="The rules to be used for validation.",
    )

    order: list[str] = Field(
        default=order_,
        description="The order in which the rules should be applied.",
    )

    async def validate(self, value, *args, strict=False, **kwargs):

        for i in self.order:
            if i in self.rules:
                try:
                    if (
                        a := await self.rules[i].validate(value, *args, **kwargs)
                        is not None
                    ):
                        return a
                except Exception as e:
                    raise ValueError(f"failed to validate field") from e
        if strict:
            raise ValueError(f"failed to validate field")

        return value

from lionagi.libs.ln_parse import StringMatch
from lionagi.core.rule.base import Rule


class ChoiceRule(Rule):
    """
    Rule for validating that a value is within a set of predefined choices.

    Attributes:
        apply_type (str): The type of data to which the rule applies.
        keys (list): The list of valid choices.
    """
    fields: list[str] = ["selection"]

    def __init__(self, apply_type="enum", **kwargs):
        super().__init__(apply_type=apply_type, **kwargs)
        self.keys = self.validation_kwargs.get("keys", None)

    async def validate(self, value: str, *args, **kwargs) -> str:
        """
        Validate that the value is within the set of predefined choices.

        Args:
            value (str): The value to validate.
            *args: Additional arguments.
            **kwargs: Additional keyword arguments.

        Returns:
            str: The validated value.

        Raises:
            ValueError: If the value is not in the set of choices.
        """
        if not value in self.keys:
            raise ValueError(f"{value} is not in chocies {self.keys}")
        return value

    async def perform_fix(self, value):
        return StringMatch.choose_most_similar(value, self.keys)

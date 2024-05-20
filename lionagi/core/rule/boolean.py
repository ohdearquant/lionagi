from typing import Any
from lionagi.libs.ln_convert import to_str, strip_lower
from lionagi.core.rule.base import Rule


class BooleanRule(Rule):
    """
    Rule for validating that a value is a boolean.

    Attributes:
        apply_type (str): The type of data to which the rule applies.
    """

    fields: list[str] = ["action_required", "extension_required"]

    def __init__(self, apply_type="bool", **kwargs):
        super().__init__(apply_type=apply_type, **kwargs)

    async def validate(self, value: Any) -> bool:
        """
        Validate that the value is a boolean.

        Args:
            value (Any): The value to validate.

        Returns:
            bool: The validated value.

        Raises:
            ValueError: If the value is not a valid boolean.
        """
        if isinstance(value, bool):
            return value
        raise ValueError(f"Invalid boolean value.")

    async def perform_fix(self, value: Any) -> bool:
        """
        Attempt to fix the value by converting it to a boolean.

        Args:
            value (Any): The value to fix.

        Returns:
            bool: The fixed value.

        Raises:
            ValueError: If the value cannot be converted to a boolean.
        """
        value = strip_lower(to_str(value))
        if value in ["true", "1", "correct", "yes"]:
            return True

        elif value in ["false", "0", "incorrect", "no", "none", "n/a"]:
            return False

        raise ValueError(f"Failed to convert {value} into a boolean value")

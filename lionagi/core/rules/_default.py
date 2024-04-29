from enum import Enum
from .choice import ChoiceRule
from .mapping import MappingRule
from .number import NumberRule
from .boolean import BooleanRule
from .string import StringRule
from .action import ActionRequestRule


class DEFAULT_RULES(Enum):
    CHOICE = ChoiceRule
    MAPPING = MappingRule
    NUMBER = NumberRule
    BOOL = BooleanRule
    STR = StringRule
    ACTION = ActionRequestRule
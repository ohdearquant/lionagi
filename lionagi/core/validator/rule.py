from lionagi.libs import validation_funcs
from abc import abstractmethod


class Rule:

    def __init__(self, **kwargs):
        self.validation_kwargs = kwargs
        self.fix = kwargs.get("fix", False)

    @abstractmethod
    def condition(self, **kwargs):
        pass

    @abstractmethod
    async def validate(self, value, **kwargs):
        pass


class ChoiceRule(Rule):

    def condition(self, choices=None):
        return choices is not None

    def check(self, choices=None):
        if choices and not isinstance(choices, list):
            try:
                choices = [i.value for i in choices]
            except Exception as e:
                raise ValueError(f"failed to get choices") from e
        return choices

    def fix(self, value, choices=None, **kwargs):
        v_ = validation_funcs["enum"](value, choices=choices, fix_=True, **kwargs)
        return v_

    async def validate(self, value, choices=None, **kwargs):
        if self.condition(choices):
            if value in self.check(choices):
                return value
            if self.fix:
                kwargs = {**self.validation_kwargs, **kwargs}
                return self.fix(value, choices, **kwargs)
            raise ValueError(f"{value} is not in chocies {choices}")


class ActionRequestRule(Rule):

    def condition(self, annotation=None):
        return any("actionrequest" in i for i in annotation)

    async def validate(self, value, annotation=None):
        if self.condition(annotation):
            try:
                return validation_funcs["action"](value)
            except Exception as e:
                raise ValueError(f"failed to validate field") from e


class BooleanRule(Rule):

    def condition(self, annotation=None):
        return "bool" in annotation and "str" not in annotation

    async def validate(self, value, annotation=None):
        if self.condition(annotation):
            try:
                return validation_funcs["bool"](
                    value, fix_=self.fix, **self.validation_kwargs
                )
            except Exception as e:
                raise ValueError(f"failed to validate field") from e


class NumberRule(Rule):

    def condition(self, annotation=None):
        return (
            any([i in annotation for i in ["int", "float", "number"]])
            and "str" not in annotation
        )

    async def validate(self, value, annotation=None):
        if self.condition(annotation):
            if "float" in annotation:
                self.validation_kwargs["num_type"] = float
                if "precision" not in self.validation_kwargs:
                    self.validation_kwargs["precision"] = 32

            try:
                return validation_funcs["number"](
                    value, fix_=self.fix, **self.validation_kwargs
                )
            except Exception as e:
                raise ValueError(f"failed to validate field") from e


class DictRule(Rule):

    def condition(self, annotation=None):
        return "dict" in annotation

    async def validate(self, value, annotation=None, keys=None):
        if self.condition(annotation):
            if "str" not in annotation or keys:
                try:
                    return validation_funcs["dict"](
                        value, keys=keys, fix_=self.fix, **self.validation_kwargs
                    )
                except Exception as e:
                    raise ValueError(f"failed to validate field") from e
            raise ValueError(f"failed to validate field")


class StringRule(Rule):

    def condition(self, annotation=None):
        return "str" in annotation

    async def validate(self, value, annotation=None):
        if self.condition(annotation):
            try:
                return validation_funcs["str"](
                    value, fix_=self.fix, **self.validation_kwargs
                )
            except Exception as e:
                raise ValueError(f"failed to validate field") from e


from enum import Enum


class DEFAULT_RULES(Enum):
    CHOICE = ChoiceRule
    ACTION_REQUEST = ActionRequestRule
    BOOL = BooleanRule
    NUMBER = NumberRule
    DICT = DictRule
    STR = StringRule

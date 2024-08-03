from pydantic import Field


from lion_core.abc import BaseRecord


from lionagi.os.primitives import note, Node, Pile, pile, Note, Progression, prog

from lionagi.os.operator.validator.rule import Rule
from lionagi.os.operator.validator.utils import validate_rules_info


class RuleBook(Node, BaseRecord):

    active_rules: Pile[Rule] = Field(default_factory=pile)
    rules_info: Note = Field(default_factory=note)
    rules_order: Progression = Field(default_factory=prog)

    def __init__(
        self,
        rules: dict[str, dict] | None = None,
        rules_order: list[str] | None = None,
    ):

        super().__init__()
        self.rules_info = validate_rules_info(rules)
        self.rule_order = rules_order
        self.active_rules: Pile = pile({}, item_type={Rule}, strict=False)

    def initiate_rule(self, rules: str | list[str] = "all"):
        rules = [rules] if isinstance(rules, str) else rules

        if len(rules) == 1 and rules[0] == "all":
            rules = list(self.rules_info.keys())

        _active = []
        for _rule in rules:
            if _rule not in self.rules_info:
                raise ValueError(f"Invalid rule: {_rule}")

            base_config = self.rules_info.get([_rule, "base_config"], {})
            _r = self.rules_info.get([_rule, "rule"])

            if not issubclass(_r, Rule):
                raise ValueError(f"Invalid rule class for {_rule}")

            _r.base_config = {**_r.base_config, **base_config}
            _r = _r(**_r.base_config)
            _r._is_init = True

            _active.append({_rule: _r})

        self.active_rules.include(_active)
        if not self.rule_order:
            self.rule_order = prog(list(self.active_rules.order))


__all__ = ["RuleBook"]

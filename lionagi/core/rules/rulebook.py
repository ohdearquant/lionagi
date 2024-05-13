from lionagi.libs.ln_func_call import lcall
from ..rules.base import Rule

"""
rule config schema 

{
    rule_name: {
        "fields: [],
        "config": {}, 
        ...
    }
}
"""


class RuleBook:

    def __init__(
        self,
        rules: dict[str, Rule] | list[Rule] = None,
        ruleorder: list[str] = None,
        rule_config: dict[str, dict] = None,
    ):
        self.rules = rules
        self.ruleorder = ruleorder
        self.rule_config = rule_config or {k: {} for k in self.ruleorder}

    @property
    def _all_applied_log(self):
        """return all applied logs from all rules in the rulebook"""
        return lcall(self.rules.values(), lambda x: x.applied_log, flatten=True)

    @property
    def _all_invoked_log(self):
        """return all invoked logs from all rules in the rulebook"""
        return lcall(self.rules.values(), lambda x: x.invoked_log, flatten=True)
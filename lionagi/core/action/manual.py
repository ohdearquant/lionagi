import re
from typing import Dict, Union, Callable, Any


class BaseManual:
    def __init__(self, template_str: str):
        self.template_str = template_str

    def _evaluate_condition(self, match, context):
        condition, text = match.groups()
        # Future implementations might parse and evaluate the condition more thoroughly
        return text if condition in context and context[condition] else ""

    def _render_conditionals(self, context: Dict[str, Union[str, int, float]]) -> str:
        conditional_pattern = re.compile(r"\{if (.*?)\}(.*?)\{endif\}", re.DOTALL)
        return conditional_pattern.sub(
            lambda match: self._evaluate_condition(match, context), self.template_str
        )

    def _replace_callable(self, match, context):
        key = match.group(1)
        if key in context:
            value = context[key]
            return str(value() if callable(value) else value)
        return match.group(0)  # Unmatched placeholders remain unchanged.

    def _render_placeholders(
        self,
        rendered_template: str,
        context: Dict[str, Union[str, int, float, Callable]],
    ) -> str:
        return re.sub(
            r"\{(\w+)\}",
            lambda match: self._replace_callable(match, context),
            rendered_template,
        )

    def generate(self, context: Dict[str, Union[str, int, float, Callable]]) -> str:
        """
        Generates output by first processing conditionals, then rendering placeholders,
        including executing callable objects for dynamic data generation.
        """
        template_with_conditionals = self._render_conditionals(context)
        final_output = self._render_placeholders(template_with_conditionals, context)
        return final_output


# from experiments.executor.executor import SafeEvaluator

# class DecisionTreeManual:
#     def __init__(self, root):
#         self.root = root
#         self.evaluator = SafeEvaluator()

#     def evaluate(self, context):
#         return self._traverse_tree(self.root, context)

#     def _traverse_tree(self, node, context):
#         if isinstance(node, CompositeActionNode) or isinstance(node, ActionNode):
#             return node.execute(context)
#         elif isinstance(node, DecisionNode):
#             condition_result = self.evaluator.evaluate(node.condition, context)
#             next_node = node.true_branch if condition_result else node.false_branch
#             return self._traverse_tree(next_node, context)
#         else:
#             raise ValueError("Invalid node type.")

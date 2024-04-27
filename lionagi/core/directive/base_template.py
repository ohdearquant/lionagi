from typing import Any, Dict
import re

from ..evaluator.base_evaluator import BaseEvaluator


class BaseDirectiveTemplate:
    """Enhanced base template class for processing templates with conditionals and loops."""

    def __init__(self, template_str: str):
        self.template_str = template_str
        self.evaluator = BaseEvaluator()

    def _render_conditionals(self, context: Dict[str, Any]) -> str:
        """Processes conditional statements with improved logic and support for 'else'."""
        pattern = re.compile(r"\{if (.*?)\}(.*?)\{else\}(.*?)\{endif\}", re.DOTALL)

        def evaluate_condition(match):
            condition, if_text, else_text = match.groups()
            if self.evaluator.evaluate(condition, context):
                return if_text
            else:
                return else_text

        return pattern.sub(evaluate_condition, self.template_str)

    def _render_loops(self, template: str, context: Dict[str, Any]) -> str:
        """Processes loop statements within the template."""
        loop_pattern = re.compile(r"\{for (\w+) in (\w+)\}(.*?)\{endfor\}", re.DOTALL)

        def render_loop(match):
            iterator_var, collection_name, loop_body = match.groups()
            collection = context.get(collection_name, [])
            if not isinstance(collection, (list, range)):
                raise ValueError(
                    f"Expected list or range for '{collection_name}', got {type(collection).__name__}."
                )

            loop_result = ""
            for item in collection:
                loop_context = context.copy()
                loop_context[iterator_var] = item
                loop_result += self.fill(loop_body, loop_context)

            return loop_result

        return loop_pattern.sub(render_loop, template)

    def fill(self, template_str: str = "", context: Dict[str, Any] = {}) -> str:
        """Fills the template with values from context after processing conditionals and loops."""
        if not template_str:  # Use the instance's template if not provided
            template_str = self.template_str

        # First, process conditionals with 'else'
        template_with_conditionals = self._render_conditionals(template_str)
        # Then, process loops
        template_with_loops = self._render_loops(template_with_conditionals, context)
        # Finally, substitute the placeholders with context values
        try:
            return template_with_loops.format(**context)
        except KeyError as e:
            print(f"Missing key in context: {e}")
            return template_with_loops

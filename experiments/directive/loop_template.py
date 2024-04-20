import re
from experiments.directive.base_template import BaseDirectiveTemplate
from typing import Dict, Any

import re
from typing import Dict, Any


class LoopTemplate(BaseDirectiveTemplate):
    def __init__(self, template_str: str):
        super().__init__(template_str)

    def _process_loops(self, command: str, context: Dict[str, Any]) -> str:
        """
        Enhanced loop processing using a structured representation for efficiency and flexibility.
        """
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
                loop_context = {**context, iterator_var: item}
                # Here, loop_body is processed as a new template to support nested loops/conditions
                looped_template = LoopTemplate(loop_body)
                loop_result += looped_template.generate(loop_context)

            return loop_result

        return loop_pattern.sub(render_loop, command)

    def generate(self, context: Dict[str, Any]) -> str:
        """
        Generates a command by first processing conditions then loops, efficiently handling nested structures.
        """
        # Process conditions in the template
        command_with_conditions = super().generate(context)
        # Process loops in the template
        return self._process_loops(command_with_conditions, context)

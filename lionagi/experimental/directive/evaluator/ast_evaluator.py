import ast
import operator


class ASTEvaluator:
    """
    Safely evaluates expressions using AST parsing to prevent unsafe operations.
    """

    def __init__(self):
        self.allowed_operators = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
            # Additional operators can be added here as needed
        }

    def evaluate(self, expression, context):
        """
        Evaluate a condition expression within a given context using AST parsing.
        """
        try:
            tree = ast.parse(expression, mode="eval")
            return self._evaluate_node(tree.body, context)
        except Exception as e:
            raise ValueError(f"Failed to evaluate expression: {expression}. Error: {e}")

    def _evaluate_node(self, node, context):
        if isinstance(node, ast.Compare):
            left = self._evaluate_node(node.left, context)
            for operation, comparator in zip(node.ops, node.comparators):
                op_func = self.allowed_operators.get(type(operation))
                if not op_func:
                    raise ValueError(
                        f"Operation {type(operation).__name__} is not allowed."
                    )
                right = self._evaluate_node(comparator, context)
                if not op_func(left, right):
                    return False
            return True
        elif isinstance(node, ast.Name):
            return context.get(node.id)
        elif isinstance(node, ast.Constant):
            return node.n
        else:
            raise ValueError(
                "Unsupported AST node type encountered in condition evaluation."
            )
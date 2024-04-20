import ast
from .ast_evaluator import ASTEvaluator


class ASTEvaluationEngine:
    """
    Executes scripts safely using the SafeEvaluator for expression evaluation.
    """

    def __init__(self):
        self.variables = {}
        self.safe_evaluator = ASTEvaluator()
        self.functions = {
            "processData": self.process_data,
        }

    def process_data(self, data):
        # Example placeholder function for data processing
        return data * 2

    def _evaluate_expression(self, expression):
        """
        Evaluates expressions within scripts using SafeEvaluator.
        """
        # Here, 'self.variables' serves as the context for the evaluation
        return self.safe_evaluator.evaluate(expression, self.variables)

    def _assign_variable(self, var_name, value):
        """
        Assigns a value to a variable within the script's context.
        """
        self.variables[var_name] = value

    def _execute_function(self, func_name, arg):
        """
        Executes a predefined function with the given argument.
        """
        if func_name in self.functions:
            function = self.functions[func_name]
            return function(arg)
        else:
            raise ValueError(f"Function '{func_name}' is not defined.")

    def execute(self, script):
        """
        Parses and executes a script, handling variable assignments and function calls.
        """
        tree = ast.parse(script, mode="exec")
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                var_name = stmt.targets[
                    0
                ].id  # Assumes single target assignment for simplicity
                # Convert the AST node back to a string for evaluation
                value_expr = ast.unparse(stmt.value)
                value = self._evaluate_expression(value_expr)
                self._assign_variable(var_name, value)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func_name = stmt.value.func.id
                arg_expr = ast.unparse(stmt.value.args[0])
                arg = self._evaluate_expression(arg_expr)
                self._execute_function(func_name, arg)
            else:
                raise ValueError(
                    "Unsupported statement type encountered in script execution."
                )

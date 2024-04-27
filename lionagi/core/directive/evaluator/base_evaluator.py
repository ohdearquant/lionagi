import ast
import operator
from typing import Any, Dict, Tuple, Callable

from lionagi.libs.ln_convert import to_dict


class BaseEvaluator:
    """
    A class to evaluate mathematical and boolean expressions from strings using Python's AST.

    Attributes:
        allowed_operators (Dict[type, Any]): A dictionary mapping AST node types to their corresponding Python operator functions.
        cache (Dict[Tuple[str, Tuple], Any]): A dictionary used to cache the results of evaluated expressions and sub-expressions.
    """

    def __init__(self) -> None:
        """Initializes the evaluator with supported operators and an empty cache."""
        self.allowed_operators: Dict[type, Any] = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.Mod: operator.mod,
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
            ast.And: lambda x, y: x and y,
            ast.Or: lambda x, y: x or y,
            ast.Not: operator.not_,
            ast.USub: operator.neg,
        }
        self.cache: Dict[Tuple[str, Tuple], Any] = {}

    def evaluate(self, expression: str, context: Dict[str, Any]) -> Any:
        """
        Evaluates a given expression string using the provided context.

        Args:
            expression (str): The expression to evaluate.
            context (Dict[str, Any]): A dictionary mapping variable names to their values.

        Returns:
            Any: The result of the evaluated expression.

        Raises:
            ValueError: If the expression cannot be evaluated.
        """
        cache_key = (expression, tuple(sorted(context.items())))
        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            tree = ast.parse(expression, mode="eval")
            result = self._evaluate_node(tree.body, context)
            self.cache[cache_key] = result
            return result
        except Exception as e:
            raise ValueError(f"Failed to evaluate expression: {expression}. Error: {e}")

    def _evaluate_node(self, node: ast.AST, context: Dict[str, Any]) -> Any:
        """Recursively evaluates an AST node."""
        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left, context)
            op_func = self.allowed_operators[type(node.op)]
            right = self._evaluate_node(node.right, context)
            result = op_func(left, right)
        elif isinstance(node, ast.UnaryOp):
            operand = self._evaluate_node(node.operand, context)
            result = self.allowed_operators[type(node.op)](operand)
        elif isinstance(node, ast.Name):
            result = context.get(node.id, None)
        elif isinstance(node, ast.Constant):
            result = node.value
        elif isinstance(node, ast.Compare):
            left = self._evaluate_node(node.left, context)
            result = True
            for operation, comparator in zip(node.ops, node.comparators):
                op_func = self.allowed_operators[type(operation)]
                right = self._evaluate_node(comparator, context)
                result = result and op_func(left, right)
                if not result:
                    break
                left = right
        elif isinstance(node, ast.BoolOp):
            values = [self._evaluate_node(value, context) for value in node.values]
            if isinstance(node.op, ast.And):
                result = all(values)
            elif isinstance(node.op, ast.Or):
                result = any(values)
            else:
                raise ValueError("Unsupported boolean operation.")
        else:
            raise ValueError("Unsupported operation in condition.")
        return result

    def add_custom_operator(self, operator_name, operation_func):
        """Adds a custom operator to the evaluator."""
        custom_node_class = type(operator_name, (ast.AST,), {})
        if custom_node_class not in self.allowed_operators:
            self.allowed_operators[custom_node_class] = operation_func
        else:
            raise ValueError(f"Custom operator '{operator_name}' is already defined.")

    def evaluate_file(self, file_path, context, format="line"):
        """Evaluates expressions from a file."""
        if format == "line":
            with open(file_path, "r") as file:
                last_result = None
                for line in file:
                    line = line.strip()
                    if line:
                        last_result = self.evaluate(line, context)
                return last_result
        elif format == "json":
            with open(file_path, "r") as file:
                data = to_dict(file)
                last_result = None
                for expression in data:
                    last_result = self.evaluate(expression, context)
                return last_result
        else:
            raise ValueError(f"Unsupported file format: {format}")

    def validate_expression(self, expression):
        """Validates the given expression."""
        try:
            tree = ast.parse(expression, mode="eval")
            self._validate_node(tree.body)
            return True, "Expression is valid."
        except Exception as e:
            return False, f"Invalid expression: {str(e)}"

    def _validate_node(self, node):
        """Validates an AST node."""
        if isinstance(
            node, (ast.BinOp, ast.Compare, ast.BoolOp, ast.Name, ast.Constant)
        ):
            if (
                isinstance(node, ast.BinOp)
                and type(node.op) not in self.allowed_operators
            ):
                raise ValueError(
                    f"Operation {type(node.op).__name__} is not supported."
                )
        else:
            raise ValueError("Unsupported node type in expression.")


class BaseEvaluationEngine:
    def __init__(self) -> None:
        self.variables: Dict[str, Any] = {}
        self.functions: Dict[str, Callable] = {
            "print": print,
        }

    def _evaluate_expression(self, expression: str) -> Any:
        try:
            return eval(expression, {}, self.variables)
        except NameError as e:
            raise ValueError(f"Undefined variable. {e}")

    def _assign_variable(self, var_name: str, value: Any) -> None:
        self.variables[var_name] = value

    def _execute_function(self, func_name: str, *args: Any) -> None:
        if func_name in self.functions:
            self.functions[func_name](*args)
        else:
            raise ValueError(f"Function {func_name} not defined.")

    def _execute_statement(self, stmt: ast.AST) -> None:
        if isinstance(stmt, ast.Assign):
            var_name = stmt.targets[0].id
            value = self._evaluate_expression(ast.unparse(stmt.value))
            self._assign_variable(var_name, value)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func_name = stmt.value.func.id
            args = [
                self._evaluate_expression(ast.unparse(arg)) for arg in stmt.value.args
            ]
            self._execute_function(func_name, *args)
        elif isinstance(stmt, ast.For):
            iter_var = stmt.target.id
            if isinstance(stmt.iter, ast.Call) and stmt.iter.func.id == "range":
                start, end = [
                    self._evaluate_expression(ast.unparse(arg))
                    for arg in stmt.iter.args
                ]
                for i in range(start, end):
                    self.variables[iter_var] = i
                    for body_stmt in stmt.body:
                        self._execute_statement(body_stmt)

    def execute(self, script: str) -> None:
        tree = ast.parse(script)
        for stmt in tree.body:
            self._execute_statement(stmt)

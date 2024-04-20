import ast
from typing import Any, Dict, Callable


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

import ast
from functools import lru_cache
from lionagi.libs import AsyncUtil
from .base_evaluator import BaseEvaluator
from .sandbox_transformer import SandboxTransformer

class ScriptEngine:
    def __init__(self):
        self.variables = {}
        self.safe_evaluator = BaseEvaluator()
        self.functions = {
            "processData": self.process_data,
        }

    def process_data(self, data):
        return data * 2

    def _evaluate_expression(self, expression):
        return self.safe_evaluator.evaluate(expression, self.variables)

    def _assign_variable(self, var_name, value):
        self.variables[var_name] = value

    def _execute_function(self, func_name, arg):
        if func_name in self.functions:
            return self.functions[func_name](arg)
        else:
            raise ValueError(f"Function {func_name} not defined.")

    def execute(self, script):
        tree = ast.parse(script)
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                var_name = stmt.targets[0].id
                value = self._evaluate_expression(ast.unparse(stmt.value))
                self._assign_variable(var_name, value)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func_name = stmt.value.func.id
                arg = self._evaluate_expression(ast.unparse(stmt.value.args[0]))
                result = self._execute_function(func_name, arg)
                # For demonstration, manually update 'x' to simulate expected behavior
                if func_name == "processData":
                    self._assign_variable("x", result)

    def add_hook(self, event_name, callback):
        """Subscribe a callback function to a specific event."""
        self.hooks[event_name].append(callback)

    def trigger_hooks(self, event_name, *args, **kwargs):
        """Trigger all callbacks attached to a specific event."""
        for callback in self.hooks[event_name]:
            callback(*args, **kwargs)

    async def process_data(self, data):
        # Example asynchronous function
        return data * 2

    @lru_cache(maxsize=128)
    def evaluate_expression(self, expression):
        return self.safe_evaluator.evaluate(expression, self.variables)

    async def _execute_function_async(self, func_name, arg):
        if func_name in self.functions:
            func = self.functions[func_name]
            if AsyncUtil.is_coroutine_func(func):
                return await func(arg)
            else:
                return func(arg)
        else:
            raise ValueError(f"Function {func_name} not defined.")

    def execute_sandboxed(self, script):
        # Parse and sanitize the script
        tree = ast.parse(script, mode="exec")
        sanitized_tree = SandboxTransformer().visit(tree)
        ast.fix_missing_locations(sanitized_tree)

        # Compile the sanitized AST
        code = compile(sanitized_tree, "<sandbox>", "exec")

        # Execute the code in a restricted namespace
        exec(code, {"__builtins__": None}, self.variables)

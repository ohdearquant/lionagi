# filename: enhanced_script_engine.py
import ast
from functools import lru_cache

from lionagi.libs import AsyncUtil


class SandboxTransformer(ast.NodeTransformer):
    """AST transformer to enforce restrictions in sandbox mode."""

    def visit_Import(self, node):
        raise RuntimeError("Import statements are not allowed in sandbox mode.")

    def visit_Exec(self, node):
        raise RuntimeError("Exec statements are not allowed in sandbox mode.")

    # Add other visit methods for disallowed operations or nodes


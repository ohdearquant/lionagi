# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
from pathlib import Path

from pydantic import BaseModel, Field

from lionagi.libs.path_safety import resolve_workspace_path as _resolve_workspace_path
from lionagi.ln.concurrency import run_sync
from lionagi.protocols.action.tool import Tool

from ..base import LionTool

__all__ = (
    "NavRequest",
    "NavResponse",
    "NavTool",
)


class NavRequest(BaseModel):
    action: str = Field(
        ...,
        description=(
            "Navigation action. One of:\n"
            "- 'outline': List all class and function signatures in a Python file.\n"
            "- 'find_definition': Find where a symbol is defined in a Python file.\n"
            "- 'find_references': Find all references to a symbol in a Python file."
        ),
    )
    path: str = Field(..., description="Python file path (absolute or workspace-relative).")
    symbol: str | None = Field(
        None,
        description="Symbol name for 'find_definition' and 'find_references'. Not used for 'outline'.",
    )


class NavItem(BaseModel):
    kind: str = Field(..., description="'class', 'function', 'method', or 'reference'.")
    name: str = Field(..., description="Symbol name.")
    line: int = Field(..., description="1-based line number.")
    col: int = Field(..., description="0-based column offset.")
    signature: str | None = Field(None, description="Function/method signature text, if available.")


class NavResponse(BaseModel):
    success: bool
    items: list[NavItem] = Field(default_factory=list)
    error: str | None = None


def _parse_file(path: str) -> tuple[ast.Module | None, str | None]:
    try:
        src = Path(path).read_text(encoding="utf-8")
        return ast.parse(src, filename=path), None
    except SyntaxError as e:
        return None, f"SyntaxError in {path}: {e}"
    except OSError as e:
        return None, f"Cannot read {path}: {e}"


def _sig_from_args(args: ast.arguments) -> str:
    parts: list[str] = []
    n_defaults = len(args.defaults)
    n_args = len(args.args)
    for i, arg in enumerate(args.args):
        default_offset = i - (n_args - n_defaults)
        if default_offset >= 0:
            parts.append(f"{arg.arg}=...")
        else:
            parts.append(arg.arg)
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for kwarg in args.kwonlyargs:
        parts.append(kwarg.arg)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"({', '.join(parts)})"


def _outline_sync(path: str) -> NavResponse:
    tree, err = _parse_file(path)
    if err:
        return NavResponse(success=False, error=err)

    items: list[NavItem] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            items.append(
                NavItem(kind="class", name=node.name, line=node.lineno, col=node.col_offset)
            )
            self._class_stack.append(node.name)
            self.generic_visit(node)
            self._class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            sig = _sig_from_args(node.args)
            kind = "method" if self._class_stack else "function"
            name = node.name if not self._class_stack else f"{self._class_stack[-1]}.{node.name}"
            items.append(
                NavItem(kind=kind, name=name, line=node.lineno, col=node.col_offset, signature=sig)
            )
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    _Visitor().visit(tree)
    return NavResponse(success=True, items=items)


def _find_definition_sync(path: str, symbol: str) -> NavResponse:
    tree, err = _parse_file(path)
    if err:
        return NavResponse(success=False, error=err)

    items: list[NavItem] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == symbol:
            items.append(
                NavItem(kind="class", name=node.name, line=node.lineno, col=node.col_offset)
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == symbol:
            sig = _sig_from_args(node.args)
            items.append(
                NavItem(
                    kind="function",
                    name=node.name,
                    line=node.lineno,
                    col=node.col_offset,
                    signature=sig,
                )
            )
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    items.append(
                        NavItem(
                            kind="assignment", name=symbol, line=node.lineno, col=node.col_offset
                        )
                    )
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                items.append(
                    NavItem(kind="assignment", name=symbol, line=node.lineno, col=node.col_offset)
                )
    return NavResponse(success=True, items=items)


def _find_references_sync(path: str, symbol: str) -> NavResponse:
    tree, err = _parse_file(path)
    if err:
        return NavResponse(success=False, error=err)

    items: list[NavItem] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol:
            items.append(
                NavItem(kind="reference", name=symbol, line=node.lineno, col=node.col_offset)
            )
        elif isinstance(node, ast.Attribute) and node.attr == symbol:
            items.append(
                NavItem(kind="reference", name=symbol, line=node.lineno, col=node.col_offset)
            )
    return NavResponse(success=True, items=items)


class NavTool(LionTool):
    is_lion_system_tool = True
    system_tool_name = "code_nav"

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self._tool: Tool | None = None
        self.workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()

    async def handle_request(self, request: NavRequest) -> NavResponse:
        if isinstance(request, dict):
            request = NavRequest(**request)
        try:
            resolved = str(_resolve_workspace_path(request.path, self.workspace_root))
        except PermissionError as exc:
            return NavResponse(success=False, error=str(exc))

        if request.action == "outline":
            return await run_sync(_outline_sync, resolved)
        if request.action == "find_definition":
            if not request.symbol:
                return NavResponse(success=False, error="'symbol' is required for find_definition.")
            return await run_sync(_find_definition_sync, resolved, request.symbol)
        if request.action == "find_references":
            if not request.symbol:
                return NavResponse(success=False, error="'symbol' is required for find_references.")
            return await run_sync(_find_references_sync, resolved, request.symbol)
        return NavResponse(success=False, error=f"Unknown action: {request.action!r}.")

    def to_tool(self) -> Tool:
        if self._tool is None:

            async def code_nav(**kwargs):
                """Navigate Python source code without reading the full file — outline signatures, find a definition, or find references.
                Operates on a single Python file via the stdlib ast module; no external dependencies.
                """
                return (await self.handle_request(NavRequest(**kwargs))).model_dump()

            self._tool = Tool(func_callable=code_nav, request_options=NavRequest)
        return self._tool

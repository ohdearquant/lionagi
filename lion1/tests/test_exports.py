"""Every module under lionagi/ declares what it exports: `__all__`, a tuple, right below the imports,
naming every public top-level name in source order and nothing else; a name not for export starts
with `_`. A package `__init__` re-exports in the same shape, each name bound by one of its imports."""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = (REPO / "lionagi",)


def inventory(tree: ast.Module) -> tuple[list[str], set[str], int, list[ast.Assign]]:
    """(public top-level names in source order, names bound by imports, last import line, the
    `__all__` assignments)."""
    names, imported, end, alls = [], set(), 0, []
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            end = node.end_lineno or end
            imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "__all__" in targets:
                alls.append(node)
            names += [t for t in targets if not t.startswith("_")]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                names.append(node.target.id)
    return names, imported, end, alls


def test_every_module_declares_its_exports_as_a_tuple_right_below_its_imports():
    modules = [f for root in ROOTS for f in sorted(root.rglob("*.py")) if "__pycache__" not in f.parts]
    assert len(modules) >= 4  # the walk saw the package, not an empty directory
    checked = []
    for f in modules:
        tree = ast.parse(f.read_text())
        names, imported, end, alls = inventory(tree)
        if not names and not alls and f.name != "__init__.py":
            continue  # nothing public: nothing to declare
        assert len(alls) == 1, f"{f}: exactly one __all__"
        (node,) = alls
        assert isinstance(node.value, ast.Tuple), (
            f"{f}: __all__ is a tuple, not a {type(node.value).__name__}"
        )
        declared = [ast.literal_eval(e) for e in node.value.elts]
        between = [type(n).__name__ for n in tree.body if end < n.lineno < node.lineno]
        assert between == [], f"{f}: __all__ sits right below the imports, not after {between}"
        if f.name == "__init__.py":
            assert declared and set(declared) <= imported, f"{f}: a re-export names an imported binding"
            assert len(set(declared)) == len(declared), f"{f}: no name twice"
        else:
            assert declared == names, f"{f}: __all__ names every public top-level name in source order"
        checked.append(f.name)
    assert {"__init__.py", "record.py", "spec.py", "bus.py"} <= set(checked)  # both shapes

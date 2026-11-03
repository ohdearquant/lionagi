"""No code under lionagi/ or lionhub/src/ calls ``asyncio.shield`` except the helper.

On CPython 3.14.7 and 3.14.8, ``asyncio.shield`` reports the inner future's failure to the
event loop's exception handler when the awaiting caller was cancelled first, and the default
handler logs the exception text. ``lionagi.ln.concurrency.errors.shield_quietly`` is the one
place that shields an awaitable without that report, so every other site goes through it.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SOURCE_TREES = ("lionagi", "lionhub/src")
HELPER_NAME = "shield_quietly"
HELPER_MODULE = "lionagi/ln/concurrency/errors.py"


def _asyncio_shield_lines(tree: ast.AST) -> list[int]:
    """Line numbers where a module reaches ``asyncio.shield``, however it spells the access.

    Covers ``asyncio.shield``, ``asyncio.tasks.shield``, ``import asyncio as alias`` followed by
    ``alias.shield`` and ``from asyncio import shield [as alias]``. A ``shield`` that does not come
    from asyncio (the AnyIO one in ``lionagi.ln.concurrency``, a ``shield=True`` keyword) is not
    reported.
    """
    asyncio_names: set[str] = set()
    tasks_names: set[str] = set()
    shield_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio" or (alias.name == "asyncio.tasks" and not alias.asname):
                    asyncio_names.add(alias.asname or "asyncio")
                elif alias.name == "asyncio.tasks":
                    tasks_names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == "asyncio" and alias.name == "tasks":
                    tasks_names.add(alias.asname or "tasks")
                elif node.module in {"asyncio", "asyncio.tasks"} and alias.name == "shield":
                    shield_names.add(alias.asname or "shield")

    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "shield":
            owner = node.value
            if isinstance(owner, ast.Name) and owner.id in asyncio_names | tasks_names:
                lines.append(node.lineno)
            elif (
                isinstance(owner, ast.Attribute)
                and owner.attr == "tasks"
                and isinstance(owner.value, ast.Name)
                and owner.value.id in asyncio_names
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in shield_names:
            lines.append(node.lineno)
    return sorted(lines)


def _source_files() -> list[Path]:
    return sorted(path for tree in SOURCE_TREES for path in (ROOT / tree).rglob("*.py"))


def _helper_span(tree: ast.AST) -> range | None:
    """Lines of the module-level helper definition, or None when the module does not define it."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == HELPER_NAME:
            return range(node.lineno, node.end_lineno + 1)
    return None


def _scan() -> tuple[list[str], list[str]]:
    """Return (modules that define the helper, 'path:line' of every asyncio.shield outside it)."""
    definers = []
    offenders = []
    for path in _source_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        span = _helper_span(tree)
        if span is not None:
            definers.append(relative)
        offenders.extend(
            f"{relative}:{line}"
            for line in _asyncio_shield_lines(tree)
            if span is None or line not in span
        )
    return definers, offenders


def _names_in(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def test_scan_finds_the_helper_module_and_reads_both_source_trees():
    definers, _ = _scan()

    assert definers == [HELPER_MODULE]
    scanned = [path.relative_to(ROOT).as_posix() for path in _source_files()]
    assert any(path.startswith("lionagi/") for path in scanned)
    assert any(path.startswith("lionhub/src/") for path in scanned)
    # Sites known to shield an awaitable are reached by the walk and use the helper.
    assert HELPER_NAME in _names_in("lionagi/state/lifecycle/reconciler.py")
    assert HELPER_NAME in _names_in("lionhub/src/lionhub/providers/_cli_subprocess.py")


def test_detector_reports_every_spelling_of_asyncio_shield():
    spellings = [
        "import asyncio\nasyncio.shield(work())\n",
        "import asyncio as _asyncio\n_asyncio.shield(work())\n",
        "import asyncio.tasks\nasyncio.tasks.shield(work())\n",
        "import asyncio.tasks as tasks\ntasks.shield(work())\n",
        "from asyncio import tasks\ntasks.shield(work())\n",
        "from asyncio import shield\nshield(work())\n",
        "from asyncio import shield as guarded\nguarded(work())\n",
        "from asyncio.tasks import shield\nshield(work())\n",
        "import asyncio\n\n\nasync def f():\n    return await asyncio.wait_for(asyncio.shield(w()), 1)\n",
        "import asyncio\n\n\ndef f():\n    import asyncio as inner\n    return inner.shield\n",
    ]
    for source in spellings:
        assert _asyncio_shield_lines(ast.parse(source)), source


def test_detector_ignores_shields_that_are_not_asyncios():
    sources = [
        "from lionagi.ln.concurrency import shield\nshield(work)\n",
        "import anyio\nwith anyio.CancelScope(shield=True):\n    pass\n",
        "from lionagi.ln.concurrency.errors import shield_quietly\nshield_quietly(work())\n",
        "import asyncio\nasyncio.wait_for(work(), 1)\n",
        "class Wrapper:\n    def shield(self):\n        pass\n\n\nWrapper().shield()\n",
    ]
    for source in sources:
        assert _asyncio_shield_lines(ast.parse(source)) == [], source


def test_no_asyncio_shield_outside_the_helper():
    definers, offenders = _scan()

    assert definers == [HELPER_MODULE]
    assert offenders == [], (
        f"use lionagi.ln.concurrency.errors.{HELPER_NAME} instead of asyncio.shield: {offenders}"
    )

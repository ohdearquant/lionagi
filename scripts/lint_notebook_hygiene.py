"""Scan publishable notebook prose and outputs for reserved identifiers."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

RESERVED_IDENTIFIER = re.compile(r"\blambda:[a-z][a-z0-9_-]*\b")


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _notebook_text(notebook: dict[str, object]) -> Iterator[str]:
    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        raise ValueError("cells must be a list")

    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("each cell must be an object")
        if cell.get("cell_type") == "code":
            yield from _strings(cell.get("outputs", []))
        else:
            yield from _strings(cell.get("source", []))


def scan(paths: list[Path]) -> int:
    matches = False
    errors = False
    notebooks = sorted(
        path for root in paths for path in ([root] if root.is_file() else root.rglob("*.ipynb"))
    )

    for path in notebooks:
        try:
            notebook = json.loads(path.read_text())
            if not isinstance(notebook, dict):
                raise ValueError("notebook must be an object")
            if any(RESERVED_IDENTIFIER.search(text) for text in _notebook_text(notebook)):
                print(f"{path}: internal namespace identifier found")
                matches = True
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            print(f"{path}: could not scan notebook: {exc}", file=sys.stderr)
            errors = True

    if errors:
        return 2
    return int(matches)


if __name__ == "__main__":
    raise SystemExit(scan([Path(arg) for arg in sys.argv[1:]]))

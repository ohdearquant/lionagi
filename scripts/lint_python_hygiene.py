"""Scan publishable Python sources for reserved internal namespace identifiers.

Mirrors ``lint_notebook_hygiene.py``'s approach for ``.ipynb`` files: the
executable-code shape of Python's own zero-argument ``lambda:`` syntax (e.g.
``transform = lambda:x + 1``) must never trip this scan, so only comments,
docstrings, and other string literals are inspected -- never bare code. A
leaked internal actor reference (``lambda:<name>``) in a cookbook/notebook
Python file shows up in exactly those spots: narration in a comment, a
docstring, or an example string argument such as ``to="lambda:leo"`` -- never
as the bare ``lambda:`` keyword itself.
"""

from __future__ import annotations

import re
import sys
import tokenize
from pathlib import Path

RESERVED_IDENTIFIER = re.compile(r"\blambda:[a-z][a-z0-9_-]*\b")

# Token types whose text can carry publishable prose: comments, ordinary
# string literals/docstrings, and (Python 3.12+) f-string literal segments.
# Deliberately excludes tokenize.NAME/OP so bare `lambda:` closure syntax in
# actual code is never inspected.
_TEXT_TOKEN_TYPES = {tokenize.COMMENT, tokenize.STRING}
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
if _FSTRING_MIDDLE is not None:
    _TEXT_TOKEN_TYPES.add(_FSTRING_MIDDLE)


def _leaked_identifiers(source: str) -> list[str]:
    lines = iter(source.splitlines(keepends=True))
    found: list[str] = []
    for tok in tokenize.generate_tokens(lambda: next(lines, "")):
        if tok.type in _TEXT_TOKEN_TYPES:
            found.extend(RESERVED_IDENTIFIER.findall(tok.string))
    return found


def scan(paths: list[Path]) -> int:
    matches = False
    errors = False
    files = sorted(
        path for root in paths for path in ([root] if root.is_file() else root.rglob("*.py"))
    )

    for path in files:
        try:
            source = path.read_text()
            if _leaked_identifiers(source):
                print(f"{path}: internal namespace identifier found")
                matches = True
        except (
            OSError,
            UnicodeError,
            tokenize.TokenizeError,
            SyntaxError,
            IndentationError,
        ) as exc:
            print(f"{path}: could not scan python source: {exc}", file=sys.stderr)
            errors = True

    if errors:
        return 2
    return int(matches)


if __name__ == "__main__":
    raise SystemExit(scan([Path(arg) for arg in sys.argv[1:]]))

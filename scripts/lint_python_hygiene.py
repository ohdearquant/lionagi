"""Scan publishable Python sources for reserved internal namespace identifiers.

Mirrors ``lint_notebook_hygiene.py``'s approach for ``.ipynb`` files: the
executable-code shape of Python's own zero-argument ``lambda:`` syntax (e.g.
``transform = lambda:x + 1``) must never trip this scan, so only comments,
docstrings, and other string literals are inspected -- never bare code. A
leaked internal actor reference (``lambda:<name>``) in a cookbook/notebook
Python file shows up in exactly those spots: narration in a comment, a
docstring, or an example string argument such as ``to="lambda:sample-unit"`` -- never
as the bare ``lambda:`` keyword itself.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

RESERVED_IDENTIFIER = re.compile(r"\blambda:[a-z][a-z0-9_-]*\b")

# Files whose subject IS the reserved vocabulary: this scanner and the tests
# that exercise it, which have to contain the very strings the scan looks for.
# Repo-relative, exact paths -- never a prefix or a directory, because a
# directory entry would exempt files added to it later without anyone deciding
# that.
#
# Every entry is required to still match. An allowlisted file that no longer
# contains a reserved identifier is reported and fails the scan, so the list
# cannot quietly outlive its reason and start hiding a real leak behind a path
# that stopped needing the exemption.
EXPECTED_FIXTURES = frozenset(
    {
        "scripts/lint_python_hygiene.py",
        "tests/scripts/test_ci_hygiene.py",
        "tests/mcp/test_notify_failure_classification.py",
        "benchmarks/orchestration/suites/lionbench/test_data_hygiene.py",
    }
)


def _repo_relative(path: Path) -> str:
    """Path as written in EXPECTED_FIXTURES, whatever root the scan was given.

    Anchored on the working directory first, because that is what the caller
    controls: the lint entry point runs from the repo root and passes relative
    paths. Falling back to a ``.git`` search alone would silently stop matching
    in any checkout-shaped tree that has no ``.git`` -- a copied fixture tree,
    an exported archive -- and an allowlist that stops matching does not fail
    loudly, it just starts reporting its own fixtures as leaks.
    """
    resolved = path.resolve()
    for anchor in (Path.cwd().resolve(), *resolved.parents):
        if anchor != Path.cwd().resolve() and not (anchor / ".git").exists():
            continue
        try:
            return resolved.relative_to(anchor).as_posix()
        except ValueError:
            continue
    return resolved.as_posix()


# Token types whose text can carry publishable prose: comments, ordinary
# string literals/docstrings, and (Python 3.12+) f-string literal segments.
# Deliberately excludes tokenize.NAME/OP so bare `lambda:` closure syntax in
# actual code is never inspected.
_TEXT_TOKEN_TYPES = {tokenize.COMMENT, tokenize.STRING}
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)
if _FSTRING_MIDDLE is not None:
    _TEXT_TOKEN_TYPES.add(_FSTRING_MIDDLE)


def _pre312_fstring_literal_segments(token_text: str) -> list[str] | None:
    """Return literal segments when *token_text* is a pre-3.12 f-string token."""
    if _FSTRING_MIDDLE is not None:
        return None

    prefix = re.match(r"(?i:[rubf]*)", token_text)
    if prefix is None or "f" not in prefix.group().lower():
        return None

    expression = ast.parse(token_text, mode="eval")
    if not isinstance(expression.body, ast.JoinedStr):
        return None
    return [
        value.value
        for value in expression.body.values
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    ]


def _leaked_identifiers(source: str) -> list[str]:
    # Feed the tokenizer the way a file reader would, breaking on ``\n`` only.
    # ``str.splitlines`` also breaks on the Unicode line separators U+2028 and
    # U+2029 and on a handful of other control characters, none of which Python
    # treats as ending a line. A string literal containing one of those would be
    # handed to the tokenizer already cut in half, and the tokenizer would
    # correctly report an unterminated string literal in a file that is
    # perfectly valid Python.
    found: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in _TEXT_TOKEN_TYPES:
            literal_segments = _pre312_fstring_literal_segments(tok.string)
            if literal_segments is None:
                literal_segments = [tok.string]
            for text in literal_segments:
                found.extend(RESERVED_IDENTIFIER.findall(text))
    return found


def scan(paths: list[Path]) -> int:
    matches = False
    errors = False
    files = sorted(
        path for root in paths for path in ([root] if root.is_file() else root.rglob("*.py"))
    )

    scanned_fixtures: set[str] = set()
    stale_fixtures: set[str] = set()

    for path in files:
        try:
            source = path.read_text()
            leaked = bool(_leaked_identifiers(source))
            relative = _repo_relative(path)
            if relative in EXPECTED_FIXTURES:
                scanned_fixtures.add(relative)
                if not leaked:
                    stale_fixtures.add(relative)
                continue
            if leaked:
                print(f"{path}: internal namespace identifier found")
                matches = True
        except (
            OSError,
            UnicodeError,
            tokenize.TokenError,
            SyntaxError,
            IndentationError,
        ) as exc:
            print(f"{path}: could not scan python source: {exc}", file=sys.stderr)
            errors = True

    # Only fixtures this run actually reached can be judged. A scan of one
    # subtree says nothing about entries living in another, so absence here is
    # "not looked at", not "gone".
    for relative in sorted(stale_fixtures):
        print(
            f"{relative}: allowlisted as a fixture but contains no reserved "
            "identifier; remove it from EXPECTED_FIXTURES",
            file=sys.stderr,
        )
        errors = True

    if errors:
        return 2
    return int(matches)


if __name__ == "__main__":
    raise SystemExit(scan([Path(arg) for arg in sys.argv[1:]]))

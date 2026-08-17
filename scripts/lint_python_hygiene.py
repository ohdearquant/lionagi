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
# The exemption is per identifier, not per file. A file-level pass meant that
# once a path was listed, *any* reserved identifier in it was excused --
# including a genuine one added later, which is the case the scan exists to
# catch and the one least likely to be noticed in a file already full of the
# vocabulary. So each entry names the specific synthetic identifiers it is
# allowed to carry, and anything else in the same file is still reported.
#
# Values are the local part only, without the ``lambda:`` prefix. Spelling them
# in full would make this table itself an occurrence of every identifier it
# excuses, so listing a name for one fixture would silently permit it in this
# file too.
#
# Every name here is required to still match, and each is judged on its own.
# One that no longer appears is reported and fails the scan, so an exemption
# cannot quietly outlive its reason and start hiding a real leak behind a name
# the file stopped using.
EXPECTED_FIXTURES: dict[str, frozenset[str]] = {
    "scripts/lint_python_hygiene.py": frozenset({"sample-unit", "x"}),
    "tests/scripts/test_ci_hygiene.py": frozenset({"item", "sample-unit", "x"}),
    "tests/mcp/test_notify_failure_classification.py": frozenset({"y"}),
    "benchmarks/orchestration/suites/lionbench/test_data_hygiene.py": frozenset({"x"}),
}


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
    # Walked rather than read off ``values`` directly. A format spec is itself a
    # JoinedStr hanging off FormattedValue.format_spec, so literal text inside
    # one (``f"{v:some-literal}"``) is nested and never appears among the
    # top-level values. Reading only the top level left that text unscanned on
    # every interpreter below 3.12, which includes the floor this project
    # supports and tests against.
    return [
        node.value
        for node in ast.walk(expression.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
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

    stale_fixtures: dict[str, frozenset[str]] = {}

    for path in files:
        try:
            source = path.read_text()
            leaked = _leaked_identifiers(source)
            relative = _repo_relative(path)
            permitted = EXPECTED_FIXTURES.get(relative)
            if permitted is not None:
                # Local parts only, so the excused set can be compared without
                # this file having to spell the identifiers it excuses.
                seen = {identifier.partition(":")[2] for identifier in leaked}
                # Liveness is asked of each name, not of the path. Judging the
                # entry as a whole lets one surviving name hold the pass open
                # for every dead one beside it, so a name the file stopped
                # using stays excused, and the next real occurrence of it is
                # suppressed by an exemption nothing is asking for any more.
                # An entry that stopped matching is a one-line deletion here,
                # which is the cheaper half of the trade against a silent hole.
                missing = permitted - seen
                if missing:
                    stale_fixtures[relative] = frozenset(missing)
                unexpected = sorted(
                    identifier
                    for identifier in set(leaked)
                    if identifier.partition(":")[2] not in permitted
                )
                if unexpected:
                    # The case a file-level exemption used to swallow: a real
                    # leak in a file that is allowed to carry the vocabulary.
                    print(
                        f"{path}: internal namespace identifier found that this "
                        f"fixture is not allowed to carry: {', '.join(unexpected)}"
                    )
                    matches = True
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
    # "not looked at", not "gone". That is why liveness is reported from the
    # files that were opened, never from what is missing from the table.
    for relative, missing in sorted(stale_fixtures.items()):
        gone = ", ".join(sorted(missing))
        print(
            f"{relative}: allowlisted to carry {gone} but no longer does; "
            "drop from its EXPECTED_FIXTURES entry",
            file=sys.stderr,
        )
        errors = True

    if errors:
        return 2
    return int(matches)


if __name__ == "__main__":
    raise SystemExit(scan([Path(arg) for arg in sys.argv[1:]]))

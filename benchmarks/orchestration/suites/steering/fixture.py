"""ADR-0088 steering fixture: op1 drafts a Python plan, op2 executes it, a steer redirects to Rust."""

from __future__ import annotations

import re

FEATURE = "a CSV row counter"

PLAN_INSTRUCTION = (
    f"Draft a short implementation plan (bullet points only, no code) for building "
    f"{FEATURE} in Python. Name the target file (e.g. something.py)."
)

IMPLEMENT_INSTRUCTION = (
    "Implement the plan from your context. Reply with the target filename and one "
    "fenced code block containing the complete implementation. Put the full code in "
    "your reply text only — do not create, edit, or write any files on disk."
)

STEER_TEXT = "change the implementation language to Rust"

# Unfoolable per ADR-0088: extract fenced code blocks and require STRUCTURAL
# Rust evidence, not a single magic token. A block only counts as Rust code
# if it has a real `fn main(` signature AND at least two independent
# Rust-shape signals (let-bindings, use-imports, `::` paths, a `!(` macro,
# `->`, `&str`/`&mut`, a `match`/`=>` arm, or brace-and-semicolon line
# density) — a lone `fn main(` sitting inside an otherwise non-Rust block
# never counts. This is deliberately structural rather than an enumeration
# of Python shapes, since blacklisting individual Python idioms is an
# unbounded game of whack-a-mole. The Python-shape disqualifier below is a
# cheap belt-and-braces on top of that, applied to EVERY fenced block
# regardless of its language tag — a rust-tagged fence is not trusted over
# its own content. See test_fixture.py for the adversarial cases both guard
# against.
_RUST_EXT = ".rs"
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)
_PYTHON_LANG_TAGS = frozenset({"python", "py"})

_RUST_FN_MAIN_RE = re.compile(r"^[ \t]*fn\s+main\s*\(", re.MULTILINE)
_RUST_LET_RE = re.compile(r"^[ \t]*let(?:\s+mut)?\s+\w+", re.MULTILINE)
_RUST_USE_RE = re.compile(r"^[ \t]*use\s+[\w:]+", re.MULTILINE)
_RUST_MACRO_RE = re.compile(r"\w+!\(")
_RUST_REF_RE = re.compile(r"&str\b|&mut\s")
_RUST_MATCH_RE = re.compile(r"^[ \t]*match\s", re.MULTILINE)

_PY_DEF_RE = re.compile(r"^[ \t]*def\s+\w+\s*\(.*\)\s*:", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^[ \t]*import\s+\w+", re.MULTILINE)
_PY_FROM_IMPORT_RE = re.compile(r"^[ \t]*from\s+\S+\s+import\s+", re.MULTILINE)
_PY_FSTRING_RE = re.compile(r"""f['"]""")
_PY_ELIF_RE = re.compile(r"^[ \t]*elif\s", re.MULTILINE)
_PY_EXCEPT_RE = re.compile(r"^[ \t]*except\b", re.MULTILINE)
_PY_WITH_RE = re.compile(r"^[ \t]*with\s.+:\s*$", re.MULTILINE)
_PY_DECORATOR_RE = re.compile(r"^[ \t]*@\w+[\s\S]*?^[ \t]*(?:def|class)\s", re.MULTILINE)
_PY_CLASS_RE = re.compile(r"^[ \t]*class\s+\w+\s*:", re.MULTILINE)
_PY_SELF_RE = re.compile(r"\bself\.")
_PY_MAIN_GUARD_RE = re.compile(r"""__name__\s*==\s*['"]__main__['"]""")


def _looks_pythonic(body: str) -> bool:
    """Heuristic: does this fenced block's content look like real Python code?

    Anchored on line-shaped Python idioms with no Rust equivalent (`import`/
    `from ... import`/`def`/`class X:`/`elif`/`except`/`with ... :`/a
    decorator/`self.`/the `__name__` guard/a bare `print(`) so Rust's `use`
    imports and `println!(` macro are never misclassified.
    """
    return bool(
        _PY_DEF_RE.search(body)
        or _PY_IMPORT_RE.search(body)
        or _PY_FROM_IMPORT_RE.search(body)
        or "print(" in body
        or _PY_FSTRING_RE.search(body)
        or _PY_ELIF_RE.search(body)
        or _PY_EXCEPT_RE.search(body)
        or _PY_WITH_RE.search(body)
        or _PY_DECORATOR_RE.search(body)
        or _PY_CLASS_RE.search(body)
        or _PY_SELF_RE.search(body)
        or _PY_MAIN_GUARD_RE.search(body)
    )


def _brace_semicolon_majority(body: str) -> bool:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    hits = sum(1 for ln in lines if ln.endswith((";", "{", "}")))
    return hits > len(lines) / 2


def _rust_signal_count(body: str) -> int:
    """Count independent Rust-shape signals beyond a bare `fn main(` token."""
    signals = (
        _RUST_LET_RE.search(body),
        _RUST_USE_RE.search(body),
        "::" in body,
        _RUST_MACRO_RE.search(body),
        "->" in body,
        _RUST_REF_RE.search(body),
        _RUST_MATCH_RE.search(body) and "=>" in body,
        _brace_semicolon_majority(body),
    )
    return sum(1 for s in signals if s)


def _is_rust_shaped(body: str) -> bool:
    """Structural requirement: `fn main(` plus >= 2 independent Rust-shape signals."""
    return bool(_RUST_FN_MAIN_RE.search(body)) and _rust_signal_count(body) >= 2


def is_steer_adherent(text: str) -> bool:
    """True iff a fenced code block shows a genuine, structural switch to Rust."""
    fences = _FENCE_RE.findall(text)
    if not fences or _RUST_EXT not in text:
        return False

    has_rust_evidence = False
    for lang, body in fences:
        lang = lang.lower().strip()
        if lang in _PYTHON_LANG_TAGS:
            return False
        if _looks_pythonic(body):
            return False
        if _is_rust_shaped(body):
            has_rust_evidence = True

    return has_rust_evidence

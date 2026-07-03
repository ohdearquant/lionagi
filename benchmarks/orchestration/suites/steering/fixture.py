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

# Unfoolable per ADR-0088: extract fenced code blocks and require the Rust
# evidence (a real `fn main(` signature, not a stray mention in a comment or
# string) to appear inside one of them, plus the `.rs` extension somewhere in
# the reply. Any fenced block that is Python-tagged or python-shaped (import,
# print(), f-strings, a real `def` signature, ...) disqualifies the whole
# response regardless of Rust vocabulary elsewhere — see test_fixture.py for
# the adversarial cases this guards against.
_RUST_EXT = ".rs"
_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)
_RUST_FN_MAIN_RE = re.compile(r"^[ \t]*fn\s+main\s*\(", re.MULTILINE)
_PY_DEF_RE = re.compile(r"^[ \t]*def\s+\w+\s*\(.*\)\s*:", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^[ \t]*import\s+\w+", re.MULTILINE)
_PY_FSTRING_RE = re.compile(r"""f['"]""")
_RUST_LANG_TAGS = frozenset({"rust", "rs"})
_PYTHON_LANG_TAGS = frozenset({"python", "py"})


def _looks_pythonic(body: str) -> bool:
    """Heuristic: does this fenced block's content look like real Python code?"""
    return bool(
        _PY_DEF_RE.search(body)
        or _PY_IMPORT_RE.search(body)
        or "print(" in body
        or _PY_FSTRING_RE.search(body)
    )


def is_steer_adherent(text: str) -> bool:
    """True iff a fenced code block shows a genuine switch to Rust."""
    fences = _FENCE_RE.findall(text)
    if not fences or _RUST_EXT not in text:
        return False

    has_rust_evidence = False
    for lang, body in fences:
        lang = lang.lower().strip()
        if lang in _PYTHON_LANG_TAGS:
            return False
        if lang in _RUST_LANG_TAGS:
            if _RUST_FN_MAIN_RE.search(body):
                has_rust_evidence = True
            continue
        # Untagged (or an unrecognized language tag) — classify by content shape.
        if _looks_pythonic(body):
            return False
        if _RUST_FN_MAIN_RE.search(body):
            has_rust_evidence = True

    return has_rust_evidence

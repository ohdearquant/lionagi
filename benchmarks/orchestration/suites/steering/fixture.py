"""ADR-0088 steering fixture: op1 drafts a Python plan, op2 executes it, a steer redirects to Rust."""

from __future__ import annotations

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

# Unfoolable per ADR-0088: require a Rust-only token, require the file
# extension, and assert the absence of the original-language token. All
# three must hold — any one alone is foolable (see test_fixture.py).
_RUST_TOKEN = "fn main"  # noqa: S105 — a fixture token, not a credential
_RUST_EXT = ".rs"
_PYTHON_TOKEN = "def "  # noqa: S105 — a fixture token, not a credential


def is_steer_adherent(text: str) -> bool:
    """True iff op2's output shows a genuine switch to Rust."""
    return _RUST_TOKEN in text and _RUST_EXT in text and _PYTHON_TOKEN not in text

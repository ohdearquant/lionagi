"""Pins the marketplace/scripts/lint_skills.py rule that resolves backticked
source references against the tree.

Every case here breaks the subject on purpose. This is a findings-only rule: it
prints when it has something to say and is silent otherwise, so a well-formed
subject produces the same empty output whether the rule passed or never looked,
and cannot serve as a control. Each broken case is therefore paired with a case
that must stay silent, which is what shows the rule discriminating rather than
merely firing.

The tree is synthetic rather than the real repository, so these stay true when
the declared root list changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_SCRIPTS_DIR = str(_REPO_ROOT / "marketplace" / "scripts")

_PREFIXES = ("lionagi/",)


def _scan_dead_paths():
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    from lint_skills import scan_dead_paths

    return scan_dead_paths


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A tree with one declared root and one real but undeclared one."""
    (tmp_path / "lionagi").mkdir()
    (tmp_path / "lionagi" / "present.py").write_text("")
    (tmp_path / "toolbox").mkdir()
    return tmp_path


def _scan(tree: Path, body: str) -> list[str]:
    subject = tree / "subject.md"
    subject.write_text(body, encoding="utf-8")
    return _scan_dead_paths()(subject, tree, prefixes=_PREFIXES)


def _kinds(findings: list[str]) -> set[str]:
    return {f.split("]")[0].lstrip("[") for f in findings}


@pytest.mark.parametrize(
    ("case", "body", "expected"),
    [
        (
            "a longer fence is not closed by a shorter run inside it",
            "````text\n```\ninner\n```\n````\n\nBroken `lionagi/missing.py` here.\n",
            {"DEAD_PATH"},
        ),
        (
            "content inside that longer fence is still skipped",
            "````text\n```\n`lionagi/missing.py`\n```\n````\n",
            set(),
        ),
        (
            "a backtick run does not close a tilde fence",
            "~~~\n```\n~~~\n\nBroken `lionagi/missing.py` after.\n",
            {"DEAD_PATH"},
        ),
        (
            "a delimiter with trailing text is not a closer",
            "```\ncode\n``` still code\n```\n\nBroken `lionagi/missing.py` outside.\n",
            {"DEAD_PATH"},
        ),
        (
            "a fence that is never closed is reported rather than silently ending coverage",
            "```\nopened and never closed\n`lionagi/missing.py`\n",
            {"UNTERMINATED_FENCE"},
        ),
        (
            "parentheses belong to the pathname, not to a prose annotation",
            "Broken `lionagi/(missing).py` reference.\n",
            {"DEAD_PATH"},
        ),
        (
            "a path that resolves stays silent",
            "Real `lionagi/present.py` reference.\n",
            set(),
        ),
        (
            "a reference into a real directory nobody declared is reported",
            "See `toolbox/anything.md` for details.\n",
            {"UNDECLARED_ROOT"},
        ),
        (
            "a root that is not in the tree at all is the rule's stated limit",
            "A `nowhere/anything.md` reference.\n",
            set(),
        ),
        (
            "templates, globs and absolute or home-relative forms are not paths",
            "Use `runs/$RUN_ID/out.json` and `stream/{id}.jsonl` and `/api/shows` "
            "and `~/.lionagi/agents/` and `https://example.com/a.py`.\n",
            set(),
        ),
    ],
)
def test_dead_path_rule(tree: Path, case: str, body: str, expected: set[str]) -> None:
    assert _kinds(_scan(tree, body)) == expected, case


def test_a_longer_fence_bypass_would_have_been_silent(tree: Path) -> None:
    """The specific shape that made an earlier version of this rule useless.

    Tracking fences with a flag toggled by any three-marker line desynchronises
    from a document using longer delimiters: the scanner leaves a block the
    document is still inside, skips the prose that follows, and ends in the
    un-fenced state, so the unterminated-fence diagnostic does not fire either.
    A run over such a file reported a clean pass while checking nothing after
    the first block. This asserts both halves: the broken reference is found,
    and no unterminated-fence finding is produced for a document whose fences
    are in fact balanced.
    """
    findings = _scan(
        tree,
        "````text\n```\ninner\n```\n````\n\n"
        "Broken `lionagi/missing.py` here.\n\n"
        "````text\n```\nmore\n```\n````\n",
    )
    assert _kinds(findings) == {"DEAD_PATH"}, findings

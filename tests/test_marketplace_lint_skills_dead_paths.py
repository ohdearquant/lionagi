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
        # Both findings are correct here, and the second is the tell that the
        # first line really was read as prose. Once it no longer opens a fence,
        # the closing-looking line at the end is itself an opener with nothing
        # after it, so the document does end inside a block.
        (
            "a backtick line whose info string holds a backtick does not open a fence",
            "``` python ```\n`lionagi/missing.py`\n```\n",
            {"DEAD_PATH", "UNTERMINATED_FENCE"},
        ),
        (
            "the same invalid opener inside a balanced document reports only the path",
            "``` python ```\n`lionagi/missing.py`\n\n```\ncode\n```\n",
            {"DEAD_PATH"},
        ),
        (
            "a plain info string does open one, so its contents stay skipped",
            "```python\n`lionagi/missing.py`\n```\n",
            set(),
        ),
        (
            "a tilde info string may hold a backtick and still opens a fence",
            "~~~ python `x`\n`lionagi/missing.py`\n~~~\n",
            set(),
        ),
        (
            "four columns of indent begin code, so such a line opens no fence",
            "    ```\n    `lionagi/missing.py`\n    ```\n",
            {"DEAD_PATH"},
        ),
        (
            "a tab is four columns and opens no fence either",
            "\t```\n`lionagi/missing.py`\n\t```\n",
            {"DEAD_PATH"},
        ),
        (
            "up to three columns still opens one",
            "   ```\n   `lionagi/missing.py`\n   ```\n",
            set(),
        ),
        # The shape the permissive version let through in silence: the indented
        # line became the opener, the unindented prose after it was skipped as
        # though it were fence content, and the last line closed a block the
        # document was never in, so nothing at all was reported.
        (
            "an indented pseudo-fence does not swallow the prose that follows it",
            "    ```\n`lionagi/missing.py`\n   ```\n",
            {"DEAD_PATH", "UNTERMINATED_FENCE"},
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
        # str.splitlines() breaks on characters Markdown does not treat as line
        # endings, and it breaks by removing them, so a fragment that should have
        # been rejected for its leading character arrives at column zero and opens
        # a fence the document does not have.
        (
            "a form feed does not end a line, so it opens no fence",
            "\f```\n`lionagi/missing.py`\n\f```\n",
            {"DEAD_PATH"},
        ),
        (
            "nor does a vertical tab",
            "\v```\n`lionagi/missing.py`\n\v```\n",
            {"DEAD_PATH"},
        ),
        (
            "nor does a Unicode line separator",
            " ```\n`lionagi/missing.py`\n ```\n",
            {"DEAD_PATH"},
        ),
        # The other half of the same decision: splitting on newlines alone is only
        # correct because the file is read in universal-newlines mode. A bare CR
        # is the case that proves it, since a read that stopped normalizing would
        # collapse this to one line whose info string holds a backtick, and the
        # path would be reported instead of skipped. CRLF is covered by the same
        # normalization but cannot fail on its own, so it is not pinned separately.
        (
            "a CR-delimited file still reads as three lines and opens a fence",
            "```\r`lionagi/missing.py`\r```\r",
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

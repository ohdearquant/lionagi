"""Check that each moved module is described in a foldable changelog entry."""

import re
import sys
from pathlib import Path

import pytest

from lionagi import _moved

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _changelog_bullets(text):
    """Yield individual Markdown bullets, preserving indented continuation lines."""
    bullet = []
    for line in text.splitlines():
        if line.startswith("- "):
            if bullet:
                yield " ".join(bullet)
            bullet = [line]
        elif bullet and line[:1] in {" ", "\t"} and line.strip():
            bullet.append(line.strip())
        else:
            if bullet:
                yield " ".join(bullet)
            bullet = []
    if bullet:
        yield " ".join(bullet)


def test_changelog_names_every_move():
    from scripts.fold_changelog_fragments import FoldRefusedError, _parse_fragment

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    bullets = list(_changelog_bullets(changelog))
    directory = ROOT / "changelog.d"
    if directory.is_dir():
        for path in directory.iterdir():
            if not re.fullmatch(r"[0-9]+\.md", path.name) or not path.is_file():
                continue
            try:
                fragment = _parse_fragment(path)
            except FoldRefusedError:
                continue
            bullets.extend(" ".join(lines) for _, lines in fragment.bullets)
    for row in _moved.MOVED:
        line = f"| `{row.old}` | `{row.new}` | {row.removed_in} |"
        version = re.compile(rf"(?<![0-9A-Za-z.]){re.escape(row.removed_in)}(?![0-9A-Za-z]|\.\d)")
        documented = any(
            f"`{row.old}`" in bullet
            and f"`{row.new}`" in bullet
            and bullet.index(f"`{row.old}`") < bullet.index(f"`{row.new}`")
            and version.search(bullet, bullet.index(f"`{row.new}`") + len(f"`{row.new}`"))
            for bullet in bullets
        )
        assert line in changelog or documented, row


@pytest.mark.parametrize("location", ["fragment", "folded"])
def test_changelog_move_recognizes_a_single_bullet(tmp_path, monkeypatch, location):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    changelog = tmp_path / "CHANGELOG.md"
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    bullet = "- `lionagi.old` moves to `lionhub.new`; the old path will be removed in 2.0.0.\n"
    changelog.write_text(
        "## [Unreleased]\n### Changed\n\n" + (bullet if location == "folded" else "")
    )
    if location == "fragment":
        (fragments / "42.md").write_text("### Changed\n\n" + bullet)
    test_changelog_names_every_move()


@pytest.mark.parametrize(
    "bullets",
    [
        "- `lionagi.old` moves to `lionhub.other`; removal in 2.0.0.\n",
        "- `lionagi.other` moves to `lionhub.new`; removal in 2.0.0.\n",
        "- `lionagi.old` moves to `lionhub.new`; removal in 12.0.0.\n",
        "- `lionhub.new` replaces `lionagi.old`; removal in 2.0.0.\n",
        "- `lionagi.old` moves to `lionhub.new`; no removal specified.\n",
        "- Removal in 2.0.0: `lionagi.old` moves to `lionhub.new`.\n",
        "- `lionagi.old` is deprecated.\n- `lionhub.new` appears in 2.0.0.\n",
    ],
)
def test_changelog_move_rejects_incomplete_or_mismatched_bullets(tmp_path, monkeypatch, bullets):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "42.md").write_text("### Changed\n\n" + bullets)
    with pytest.raises(AssertionError):
        test_changelog_names_every_move()


def test_changelog_move_bullet_survives_real_fold(tmp_path, monkeypatch):
    from scripts.fold_changelog_fragments import main

    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [Previous]\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "42.md").write_text(
        "### Changed\n\n- `lionagi.old` moves to `lionhub.new`;\n"
        "  the old path will be removed in 2.0.0.\n"
    )
    test_changelog_names_every_move()
    assert main(tmp_path) == 0
    assert not (fragments / "42.md").exists()
    test_changelog_names_every_move()


def test_changelog_move_cannot_be_assembled_across_files_or_readme(tmp_path, monkeypatch):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n- `lionagi.old` was moved.\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "42.md").write_text("### Changed\n\n- `lionhub.new` in 2.0.0.\n")
    (fragments / "README.md").write_text(
        "- `lionagi.old` moves to `lionhub.new` and is removed in 2.0.0.\n"
    )
    with pytest.raises(AssertionError):
        test_changelog_names_every_move()


def test_invalid_fragment_cannot_prove_a_move(tmp_path, monkeypatch):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "42.md").write_text("- `lionagi.old` moves to `lionhub.new`; removal in 2.0.0.\n")
    with pytest.raises(AssertionError):
        test_changelog_names_every_move()


def test_non_numbered_fragment_cannot_prove_a_move(tmp_path, monkeypatch):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "move.md").write_text(
        "### Changed\n\n- `lionagi.old` moves to `lionhub.new`; removal in 2.0.0.\n"
    )
    with pytest.raises(AssertionError):
        test_changelog_names_every_move()


def test_invalid_move_fragment_is_refused_by_fold(tmp_path, monkeypatch):
    from scripts.fold_changelog_fragments import main

    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    changelog = tmp_path / "CHANGELOG.md"
    original = "## [Unreleased]\n\n## [Previous]\n"
    changelog.write_text(original)
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    fragment = fragments / "42.md"
    invalid = "- `lionagi.old` moves to `lionhub.new`; removal in 2.0.0.\n"
    fragment.write_text(invalid)
    with pytest.raises(AssertionError):
        test_changelog_names_every_move()
    assert main(tmp_path) == 1
    assert changelog.read_text() == original
    assert fragment.read_text() == invalid

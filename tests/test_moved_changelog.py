"""Check that each moved module is described in a foldable changelog entry."""

import re
import sys
from pathlib import Path

import pytest

from lionagi import _moved

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _changelog_bullets(text):
    """Yield top-level bullets without nested items or fenced code."""
    bullet = []
    fence = None
    nested = False
    for line in text.splitlines():
        if fence is not None:
            char, length = fence
            if re.fullmatch(r"[ \t]*" + re.escape(char) + "{" + str(length) + r",}[ \t]*", line):
                fence = None
            continue
        fence_open = re.match(r"[ \t]*(`{3,}|~{3,})", line)
        if fence_open:
            if line.startswith(fence_open.group(1)) and bullet:
                yield " ".join(bullet)
                bullet = []
            marker = fence_open.group(1)
            fence = (marker[0], len(marker))
            continue
        if line.startswith("- "):
            if bullet:
                yield " ".join(bullet)
            bullet = [line]
            nested = False
        elif re.match(r"[ \t]+(?:[-+*]|[0-9]+[.)])[ \t]+", line):
            if bullet:
                yield " ".join(bullet)
            bullet = []
            nested = True
        elif bullet and not nested and line[:1] in {" ", "\t"} and line.strip():
            bullet.append(line.strip())
        else:
            if bullet:
                yield " ".join(bullet)
            bullet = []
    if bullet:
        yield " ".join(bullet)


def _unfenced_lines(text):
    """Exclude fenced examples when accepting historical changelog table rows."""
    fence = None
    for line in text.splitlines():
        if fence is not None:
            char, length = fence
            if re.fullmatch(r"[ \t]*" + re.escape(char) + "{" + str(length) + r",}[ \t]*", line):
                fence = None
            continue
        opening = re.match(r"[ \t]*(`{3,}|~{3,})", line)
        if opening:
            marker = opening.group(1)
            fence = (marker[0], len(marker))
        else:
            yield line


def test_changelog_names_every_move():
    from scripts.fold_changelog_fragments import FoldRefusedError, _parse_fragment

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    bullets = list(_changelog_bullets(changelog))
    historical_lines = list(_unfenced_lines(changelog))
    directory = ROOT / "changelog.d"
    if directory.is_dir():
        for path in directory.iterdir():
            if not re.fullmatch(r"[0-9]+\.md", path.name) or not path.is_file():
                continue
            try:
                fragment = _parse_fragment(path)
            except FoldRefusedError:
                continue
            for _, lines in fragment.bullets:
                bullets.extend(_changelog_bullets("\n".join(lines)))
    for row in _moved.MOVED:
        line = f"| `{row.old}` | `{row.new}` | {row.removed_in} |"
        version = re.compile(
            rf"(?<![0-9A-Za-z.]){re.escape(row.removed_in)}"
            r"(?![-+0-9A-Za-z]|\.\d)"
        )
        documented = any(
            f"`{row.old}`" in bullet
            and f"`{row.new}`" in bullet
            and bullet.index(f"`{row.old}`") < bullet.index(f"`{row.new}`")
            and version.search(bullet, bullet.index(f"`{row.new}`") + len(f"`{row.new}`"))
            for bullet in bullets
        )
        assert any(line in item for item in historical_lines) or documented, row


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


@pytest.mark.parametrize("location", ["fragment", "folded"])
@pytest.mark.parametrize(
    "case", ["nested", "fenced", "fenced_deep", "prerelease", "local", "extra_component"]
)
def test_changelog_move_rejects_nonbullet_or_inexact_version(tmp_path, monkeypatch, location, case):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "changelog.d").mkdir()
    statements = {
        "nested": "- `lionagi.old` moved.\n  - `lionhub.new` removal in 2.0.0.\n",
        "fenced": (
            "- Example:\n  ```\n  `lionagi.old` to `lionhub.new` in 2.0.0.\n  ```\n"
            if location == "fragment"
            else "```md\n- `lionagi.old` to `lionhub.new` in 2.0.0.\n```\n"
        ),
        "fenced_deep": "- Example:\n    ```\n    `lionagi.old` to `lionhub.new` in 2.0.0.\n    ```\n",
        "prerelease": "- `lionagi.old` to `lionhub.new` in 2.0.0-rc1.\n",
        "local": "- `lionagi.old` to `lionhub.new` in 2.0.0+local.\n",
        "extra_component": "- `lionagi.old` to `lionhub.new` in 2.0.0.1.\n",
    }
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [Unreleased]\n\n" + (statements[case] if location == "folded" else ""))
    if location == "fragment":
        (tmp_path / "changelog.d" / "42.md").write_text("### Changed\n\n" + statements[case])
    with pytest.raises(AssertionError):
        test_changelog_names_every_move()


@pytest.mark.parametrize("fenced", [False, True], ids=["historical-table", "fenced-table"])
def test_only_unfenced_historical_table_proves_a_move(tmp_path, monkeypatch, fenced):
    row = _moved.Moved("lionagi.old", "lionhub.new", "0.37.0", "2.0.0")
    monkeypatch.setattr(_moved, "MOVED", (row,))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    table = "| `lionagi.old` | `lionhub.new` | 2.0.0 |\n"
    if fenced:
        table = "```markdown\n" + table + "```\n"
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n" + table)
    if fenced:
        with pytest.raises(AssertionError):
            test_changelog_names_every_move()
    else:
        test_changelog_names_every_move()

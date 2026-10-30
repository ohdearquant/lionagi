"""Folding per-pull-request changelog fragments into the Unreleased block of CHANGELOG.md."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fold_changelog_fragments",
    Path(__file__).resolve().parents[2] / "scripts" / "fold_changelog_fragments.py",
)
fold = importlib.util.module_from_spec(_SPEC)
# dataclasses resolves string annotations through sys.modules, so register before executing.
sys.modules[_SPEC.name] = fold
_SPEC.loader.exec_module(fold)


def text(*lines: str) -> str:
    """The lines joined into a file that ends in a newline."""
    return "\n".join(lines) + "\n"


def changelog(*unreleased: str) -> str:
    """A CHANGELOG.md whose Unreleased block holds ``unreleased``, followed by a release."""
    return text(
        "# Changelog",
        "",
        "All notable changes are documented here.",
        "",
        "## [Unreleased]",
        "",
        *unreleased,
        "## [0.1.0] - 2026-01-01",
        "",
        "### Fixed",
        "",
        "- Old fix.",
    )


def lay_out(root: Path, changelog_text: str, fragments: dict[str, str]) -> bytes:
    """Write CHANGELOG.md and the fragments under ``root``; return the changelog's bytes."""
    (root / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    directory = root / "changelog.d"
    directory.mkdir()
    for name, body in fragments.items():
        (directory / name).write_text(body, encoding="utf-8")
    return (root / "CHANGELOG.md").read_bytes()


def fragment_names(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "changelog.d").iterdir())


ADDED_AND_CHANGED = (
    "### Added",
    "",
    "- Existing added.",
    "",
    "### Changed",
    "",
    "- Existing changed.",
    "",
)


def test_two_fragments_fold_under_their_headings_in_number_order(tmp_path):
    # 10.md is written first and sorts first by name; number order puts 9.md ahead of it.
    lay_out(
        tmp_path,
        changelog(*ADDED_AND_CHANGED),
        {
            "10.md": text(
                "### Added",
                "",
                "- Ten added.",
                "",
                "### Changed",
                "",
                "- Ten changed.",
                "",
                "### Fixed",
                "",
                "- Ten fixed.",
            ),
            "9.md": text(
                "### Added",
                "",
                "- Nine added.",
                "",
                "### Fixed",
                "",
                "- Nine fixed.",
            ),
            "README.md": text("The convention."),
        },
    )

    assert fold.main(tmp_path) == 0

    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == changelog(
        "### Added",
        "",
        "- Existing added.",
        "",
        "- Nine added.",
        "",
        "- Ten added.",
        "",
        "### Changed",
        "",
        "- Existing changed.",
        "",
        "- Ten changed.",
        "",
        "### Fixed",
        "",
        "- Nine fixed.",
        "",
        "- Ten fixed.",
        "",
    )
    assert fragment_names(tmp_path) == ["README.md"]


def test_missing_headings_are_created_in_keep_a_changelog_order(tmp_path):
    lay_out(
        tmp_path,
        changelog("### Changed", "", "- Existing changed.", ""),
        {
            "1.md": text("### Security", "", "- Sec.", "", "### Added", "", "- Add."),
            "2.md": text("### Deprecated", "", "- Dep."),
        },
    )

    assert fold.main(tmp_path) == 0

    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == changelog(
        "### Added",
        "",
        "- Add.",
        "",
        "### Changed",
        "",
        "- Existing changed.",
        "",
        "### Deprecated",
        "",
        "- Dep.",
        "",
        "### Security",
        "",
        "- Sec.",
        "",
    )


def test_an_unreleased_block_at_the_end_of_the_file_folds_cleanly(tmp_path):
    lay_out(
        tmp_path,
        text("# Changelog", "", "## [Unreleased]", "", "### Added", "", "- Existing added."),
        {"3.md": text("### Fixed", "", "- New fix.")},
    )

    assert fold.main(tmp_path) == 0

    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == text(
        "# Changelog",
        "",
        "## [Unreleased]",
        "",
        "### Added",
        "",
        "- Existing added.",
        "",
        "### Fixed",
        "",
        "- New fix.",
    )


def test_a_multi_line_bullet_keeps_its_continuation_lines(tmp_path):
    lay_out(
        tmp_path,
        changelog("### Fixed", "", "- Existing fix.", ""),
        {
            "5.md": text(
                "### Fixed",
                "",
                "- First line of the bullet",
                "  continues here",
                "    and here, deeper.",
                "",
                "- Second bullet.",
            )
        },
    )

    assert fold.main(tmp_path) == 0

    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == changelog(
        "### Fixed",
        "",
        "- Existing fix.",
        "",
        "- First line of the bullet",
        "  continues here",
        "    and here, deeper.",
        "",
        "- Second bullet.",
        "",
    )


@pytest.mark.parametrize("with_directory", [True, False])
def test_a_run_with_no_fragments_changes_nothing(tmp_path, with_directory):
    before = changelog(*ADDED_AND_CHANGED)
    (tmp_path / "CHANGELOG.md").write_text(before, encoding="utf-8")
    if with_directory:
        (tmp_path / "changelog.d").mkdir()
        readme = tmp_path / "changelog.d" / "README.md"
        readme.write_text(text("The convention."), encoding="utf-8")
    before_bytes = (tmp_path / "CHANGELOG.md").read_bytes()

    assert fold.main(tmp_path) == 0

    assert (tmp_path / "CHANGELOG.md").read_bytes() == before_bytes
    if with_directory:
        assert fragment_names(tmp_path) == ["README.md"]


GOOD_FRAGMENT = text("### Added", "", "- Fine.")


@pytest.mark.parametrize(
    ("changelog_text", "bad", "culprit"),
    [
        pytest.param(
            changelog(*ADDED_AND_CHANGED),
            {"2.md": text("### Docs", "", "- Not a Keep a Changelog heading.")},
            "2.md",
            id="unknown-heading",
        ),
        pytest.param(changelog(*ADDED_AND_CHANGED), {"2.md": ""}, "2.md", id="empty-fragment"),
        pytest.param(
            changelog(*ADDED_AND_CHANGED),
            {"2.md": text("### Added", "")},
            "2.md",
            id="heading-without-bullets",
        ),
        pytest.param(
            changelog(*ADDED_AND_CHANGED),
            {"2.md": text("Intro prose.", "", "### Added", "", "- Item.")},
            "2.md",
            id="text-before-a-heading",
        ),
        pytest.param(
            changelog(*ADDED_AND_CHANGED),
            {"2.md": text("### Added", "", "Prose that is not a bullet.")},
            "2.md",
            id="text-that-is-not-a-bullet",
        ),
        pytest.param(
            changelog(*ADDED_AND_CHANGED),
            {"notes.txt": "stray"},
            "notes.txt",
            id="stray-file",
        ),
        pytest.param(
            text("# Changelog", "", "## [0.1.0] - 2026-01-01"),
            {},
            "CHANGELOG.md",
            id="missing-unreleased-block",
        ),
    ],
)
def test_a_refusal_changes_nothing(tmp_path, capsys, changelog_text, bad, culprit):
    fragments = {"1.md": GOOD_FRAGMENT, **bad}
    before_bytes = lay_out(tmp_path, changelog_text, fragments)

    assert fold.main(tmp_path) != 0

    message = capsys.readouterr().err.strip().splitlines()
    assert len(message) == 1
    assert culprit in message[0]
    assert (tmp_path / "CHANGELOG.md").read_bytes() == before_bytes
    assert fragment_names(tmp_path) == sorted(fragments)

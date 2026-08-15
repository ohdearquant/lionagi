"""Verify docs/adr/ numbering stays unique and heading-consistent."""

from pathlib import Path

import pytest

from scripts.check_adr_numbering import (
    _MAX_TITLE_BYTES as MAX_TITLE_BYTES,
)
from scripts.check_adr_numbering import (
    DEFAULT_ADR_DIR,
    check_dir,
)


def _write_adr(adr_dir: Path, name: str, heading_number: str) -> None:
    (adr_dir / name).write_text(f"# ADR-{heading_number}: Some decision\n\nBody.\n")


def test_current_corpus_has_zero_numbering_errors():
    errors = check_dir(DEFAULT_ADR_DIR)
    assert errors == [], "\n".join(errors)


def test_duplicate_number_fails_naming_both_files(tmp_path: Path):
    _write_adr(tmp_path, "ADR-0116-editor-client-capability-expansion.md", "0116")
    _write_adr(tmp_path, "ADR-0116-normalized-progression-membership.md", "0116")
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "ADR-0116-editor-client-capability-expansion.md" in errors[0]
    assert "ADR-0116-normalized-progression-membership.md" in errors[0]


def test_heading_number_must_match_filename(tmp_path: Path):
    _write_adr(tmp_path, "ADR-0002-second-decision.md", "0001")
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "heading says ADR-0001" in errors[0]
    assert "filename says ADR-0002" in errors[0]


def test_missing_title_heading_fails(tmp_path: Path):
    (tmp_path / "ADR-0003-headless.md").write_text("Body without a title.\n")
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "first line is not a '# ADR-NNNN: Human Title' heading" in errors[0]


def test_title_must_be_the_first_line(tmp_path: Path):
    """A correct heading further down does not satisfy the first-line rule."""
    (tmp_path / "ADR-0004-late-heading.md").write_text(
        "Not a title line\n\n# ADR-0004: Title arrives late\n"
    )
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "first line is not a" in errors[0]


def test_heading_inside_a_code_fence_does_not_count(tmp_path: Path):
    (tmp_path / "ADR-0005-fenced.md").write_text(
        "Some stray prose.\n\n```\n# ADR-0005: Only inside a fence\n```\n"
    )
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "first line is not a" in errors[0]


def test_symlinked_record_is_rejected(tmp_path: Path):
    target = tmp_path / "real.md"
    target.write_text("# ADR-0006: Real record\n")
    (tmp_path / "ADR-0006-linked.md").symlink_to(target)
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "is a symlink" in errors[0]


@pytest.mark.parametrize("title_bytes", [MAX_TITLE_BYTES - 1, MAX_TITLE_BYTES, 8192])
def test_a_title_longer_than_the_read_bound_is_still_valid(tmp_path: Path, title_bytes: int):
    """The read bound protects against unbounded files; it is not a title-length limit."""
    heading = "# ADR-0007: "
    (tmp_path / "ADR-0007-long-title.md").write_text(
        heading + "x" * (title_bytes - len(heading)) + "\n\nBody.\n"
    )
    assert check_dir(tmp_path) == []


def test_an_unterminated_long_first_line_is_still_read_as_a_title(tmp_path: Path):
    """No trailing newline is a formatting nit, not grounds to reject the heading."""
    (tmp_path / "ADR-0007-unterminated.md").write_text("# ADR-0007: " + "x" * 8192)
    assert check_dir(tmp_path) == []


def test_a_long_wrong_first_line_reports_a_capped_excerpt(tmp_path: Path):
    """A truncated first line must not dump thousands of characters into the log."""
    (tmp_path / "ADR-0008-long-junk.md").write_text("y" * 8192)
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "first line is not a" in errors[0]
    assert "chars)" in errors[0]
    assert len(errors[0]) < 300


def test_a_non_utf8_first_line_says_so(tmp_path: Path):
    """The unreadable-as-text case is named, not collapsed into 'found: None'."""
    (tmp_path / "ADR-0009-binary.md").write_bytes(b"# ADR-0009: \xff\xfe not text\n")
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "not valid UTF-8" in errors[0]


def test_malformed_filename_fails(tmp_path: Path):
    _write_adr(tmp_path, "ADR-116-three-digit-number.md", "0116")
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "does not match ADR-NNNN-<slug>.md" in errors[0]


def test_empty_directory_is_an_error_not_a_pass(tmp_path: Path):
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "no ADR-*.md files found" in errors[0]


def test_clean_corpus_passes(tmp_path: Path):
    _write_adr(tmp_path, "ADR-0001-first-decision.md", "0001")
    _write_adr(tmp_path, "ADR-0002-second-decision.md", "0002")
    assert check_dir(tmp_path) == []

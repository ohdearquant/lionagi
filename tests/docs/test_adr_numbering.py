"""Verify docs/adr/ numbering stays unique and heading-consistent."""

from pathlib import Path

from scripts.check_adr_numbering import DEFAULT_ADR_DIR, check_dir


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


def test_a_first_line_longer_than_the_read_bound_is_not_a_title(tmp_path: Path):
    """The title is read from a bounded prefix, so an unterminated first line fails closed."""
    (tmp_path / "ADR-0007-unterminated.md").write_text("# ADR-0007: " + "x" * 8192)
    errors = check_dir(tmp_path)
    assert len(errors) == 1
    assert "first line is not a" in errors[0]


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

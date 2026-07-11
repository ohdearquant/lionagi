"""Verify docs/adr/ status-set literals stay aligned with the lifecycle policy registry."""

from pathlib import Path

import pytest

from scripts.check_adr_status_sets import DEFAULT_ADR_DIR, check_file, check_paths

DOCS_ADR_DIR = DEFAULT_ADR_DIR


def test_current_corpus_has_zero_mismatches():
    errors = check_paths([DOCS_ADR_DIR])
    assert errors == [], "\n\n".join(errors)


def test_schedule_run_terminal_frozenset_missing_timed_out(tmp_path: Path):
    doc = tmp_path / "drift.md"
    doc.write_text(
        "```python\n"
        "SCHEDULE_RUN_TERMINAL_STATUSES = frozenset({\n"
        '    "completed", "failed", "skipped", "cancelled"\n'
        "})\n"
        "```\n"
    )
    errors = check_file(doc)
    assert len(errors) == 1
    assert "schedule_run" in errors[0]
    assert "timed_out" in errors[0]


def test_schedule_run_wait_table_row_missing_timed_out(tmp_path: Path):
    doc = tmp_path / "drift.md"
    doc.write_text(
        "| Kind | Terminal statuses | Success statuses |\n"
        "|------|-------------------|-------------------|\n"
        "| `schedule_run` | `completed`, `failed`, `skipped`, `cancelled` | `completed` |\n"
    )
    errors = check_file(doc)
    assert len(errors) == 1
    assert "schedule_run" in errors[0]
    assert "timed_out" in errors[0]


def test_vocabulary_block_containing_pending(tmp_path: Path):
    doc = tmp_path / "drift.md"
    doc.write_text(
        "```python\n"
        "VALID_SCHEDULE_RUN_STATUSES = frozenset({\n"
        '    "queued", "waiting_dependency", "running", "retry_wait",\n'
        '    "completed", "failed", "timed_out", "skipped", "cancelled", "pending"\n'
        "})\n"
        "```\n"
    )
    errors = check_file(doc)
    assert len(errors) == 1
    assert "schedule_run" in errors[0]
    assert "pending" in errors[0]


def test_unknown_symbol_is_skipped_not_errored(tmp_path: Path):
    doc = tmp_path / "clean.md"
    doc.write_text('```python\nSOME_UNRELATED_STATUSES = frozenset({\n    "foo", "bar"\n})\n```\n')
    assert check_file(doc) == []


def test_prose_terminal_cell_is_not_treated_as_enumeration(tmp_path: Path):
    doc = tmp_path / "prose.md"
    doc.write_text(
        "| Entity | Declared/current values | Terminal set |\n"
        "|--------|--------------------------|---------------|\n"
        "| `session` | `running`, `completed` | all except `running` |\n"
    )
    assert check_file(doc) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

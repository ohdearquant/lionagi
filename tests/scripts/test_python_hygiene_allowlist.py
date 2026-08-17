# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
#
# SPDX-License-Identifier: Apache-2.0

"""The fixture allowlist in the Python publication-hygiene scanner.

The scanner is pointed at the source trees, which contain files whose subject
IS the reserved vocabulary -- the scanner itself and the tests exercising it.
Those are exempted by exact path. An exemption is a hole by construction, so
these tests pin both directions: that it suppresses what it is meant to, and
that it fails loudly once the reason for it goes away.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER = REPO_ROOT / "scripts" / "lint_python_hygiene.py"

# Written apart so this file is not itself a fixture the scanner must exempt.
RESERVED = "lambda:" + "sample-unit"

# U+2028 LINE SEPARATOR, written as an escape so this file carries none itself.
LINE_SEPARATOR = "\u2028"


def _fake_repo(tmp_path: Path) -> Path:
    """A tree the scanner resolves repo-relative paths against."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _scan(*targets: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER), *(str(t) for t in targets)],
        capture_output=True,
        text=True,
    )


def test_a_reserved_identifier_in_an_ordinary_file_is_reported(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    target = repo / "lionagi" / "service.py"
    target.parent.mkdir(parents=True)
    target.write_text(f'CONFIG = {{"deliver_to": "{RESERVED}"}}\n')

    result = _scan(repo / "lionagi")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "internal namespace identifier found" in result.stdout


def test_python_closure_syntax_is_never_reported(tmp_path: Path) -> None:
    # The reason the source trees are safe to scan at all: a line-oriented
    # matcher cannot tell this from a leaked identifier, and the tokenizer can.
    repo = _fake_repo(tmp_path)
    target = repo / "lionagi" / "closures.py"
    target.parent.mkdir(parents=True)
    closure = "transform = lambda:" + "x + 1\nother = lambda: 42\n"
    target.write_text(closure)

    result = _scan(repo / "lionagi")

    assert result.returncode == 0, result.stdout + result.stderr


def test_an_allowlisted_fixture_carrying_the_vocabulary_is_exempt(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    target = repo / "tests" / "scripts" / "test_ci_hygiene.py"
    target.parent.mkdir(parents=True)
    target.write_text(f'SAMPLE = "{RESERVED}"\n')

    result = _scan(repo / "tests")

    assert result.returncode == 0, result.stdout + result.stderr


def test_an_allowlisted_fixture_that_stopped_carrying_it_fails_the_scan(
    tmp_path: Path,
) -> None:
    # The exemption outliving its reason is the failure mode that matters: the
    # path keeps its pass while no longer needing it, and the next real leak
    # written into that file goes unreported. Removing the entry is the fix,
    # so the scanner has to say so rather than stay quiet.
    repo = _fake_repo(tmp_path)
    target = repo / "tests" / "scripts" / "test_ci_hygiene.py"
    target.parent.mkdir(parents=True)
    target.write_text("SAMPLE = 'nothing reserved here'\n")

    result = _scan(repo / "tests")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "remove it from EXPECTED_FIXTURES" in result.stderr


def test_a_fixture_path_is_exempt_only_at_its_exact_location(tmp_path: Path) -> None:
    # The allowlist holds paths, not names. A file that merely shares a
    # basename with an allowlisted one gets no exemption, so moving or copying
    # a fixture cannot carry the exemption along with it.
    repo = _fake_repo(tmp_path)
    target = repo / "lionagi" / "test_ci_hygiene.py"
    target.parent.mkdir(parents=True)
    target.write_text(f'SAMPLE = "{RESERVED}"\n')

    result = _scan(repo / "lionagi")

    assert result.returncode == 1, result.stdout + result.stderr


def test_a_unicode_line_separator_inside_a_literal_does_not_blind_the_scan(
    tmp_path: Path,
) -> None:
    # U+2028 is a line boundary to ``str.splitlines`` and is not one to Python.
    # Splitting the source that way hands the tokenizer a string literal already
    # cut in half, and everything after the cut stops being inspected. The
    # failure is silent: the scan returns clean on a file that leaks. Written as
    # an escape so this test file carries no separator of its own.
    repo = _fake_repo(tmp_path)
    target = repo / "lionagi" / "separator.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        f'DOC = "first half{LINE_SEPARATOR}then {RESERVED} here"\n',
        encoding="utf-8",
    )

    result = _scan(repo / "lionagi")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "internal namespace identifier found" in result.stdout


def test_an_untokenizable_file_is_reported_rather_than_crashing_the_scan(
    tmp_path: Path,
) -> None:
    # Forces the except clause to be evaluated. An except tuple naming an
    # attribute that does not exist is a valid module until something raises, so
    # only a test that actually raises can tell the name is wrong. Reaching the
    # handler is the point here; the exit code and message are what it does once
    # it gets there.
    repo = _fake_repo(tmp_path)
    target = repo / "lionagi" / "broken.py"
    target.parent.mkdir(parents=True)
    target.write_text("values = [1, 2,\n")

    result = _scan(repo / "lionagi")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "could not scan python source" in result.stderr


def test_the_shipped_source_trees_are_clean_under_the_widened_scope() -> None:
    # Guards the widening itself: if this fails, either a real identifier
    # landed in the source trees or an allowlisted fixture went stale.
    result = _scan(
        *(REPO_ROOT / name for name in ("lionagi", "tests", "scripts", "benchmarks", "marketplace"))
    )

    assert result.returncode == 0, result.stdout + result.stderr

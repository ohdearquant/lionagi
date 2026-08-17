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

# Every identifier tests/scripts/test_ci_hygiene.py is allowlisted for.
PERMITTED_NAMES = ("item", "sample-unit", "x")


def _fixture_carrying(*names: str) -> str:
    """Source for a synthetic fixture at the allowlisted path.

    Kept as a helper because liveness is per identifier: a fixture written
    with only one of the permitted names is reported for the two it dropped,
    and the scan exits 2 for a reason unrelated to what the test measures.
    The prefix is joined in at runtime so this file spells no identifier of
    its own and needs no exemption.
    """
    prefix = "lambda:"
    return "".join(f'SAMPLE_{index} = "{prefix}{name}"\n' for index, name in enumerate(names))


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
    target.write_text(_fixture_carrying(*PERMITTED_NAMES))

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
    assert "EXPECTED_FIXTURES" in result.stderr
    # Reported per identifier rather than per file: this fixture is allowlisted
    # for three, and a message naming only the file would leave which exemption
    # to delete as a guess.
    for name in ("item", "sample-unit", "x"):
        assert name in result.stderr, result.stderr


def test_one_surviving_name_does_not_keep_the_others_authorized(tmp_path: Path) -> None:
    # Liveness judged per file rather than per identifier passes this case:
    # two of the three names are still here, so the entry as a whole looks
    # live, and the one that left stays excused. Nothing then reports it, and
    # the next genuine occurrence of that identifier in this file is suppressed
    # by an exemption no longer covering anything.
    repo = _fake_repo(tmp_path)
    target = repo / "tests" / "scripts" / "test_ci_hygiene.py"
    target.parent.mkdir(parents=True)
    surviving = tuple(name for name in PERMITTED_NAMES if name != "item")
    target.write_text(_fixture_carrying(*surviving))

    result = _scan(repo / "tests")

    assert result.returncode == 2, result.stdout + result.stderr
    # Compared as the exact reported list rather than by substring: one of the
    # names is a single character, so asking whether it appears anywhere in the
    # message answers about the wording and not about the finding.
    reported = result.stderr.split("carry ", 1)[-1].split(" but", 1)[0]
    assert reported == "item", result.stderr


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


def test_every_tracked_python_tree_is_in_the_ci_scan_list() -> None:
    """The scan list is hand-written, so the thing to guard is its completeness.

    A tree that is simply absent from the list is scanned by nothing and
    reports nothing, which is indistinguishable from a tree that is clean. That
    is how examples/ sat outside the gate: eleven tracked files, no finding, no
    signal that they were never read.

    Derived from what git actually tracks rather than from a second hand-written
    list, because two hand-written lists drift into agreeing with each other and
    not with the repo.
    """
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if tracked.returncode != 0:
        import pytest

        pytest.skip("not a git checkout")

    trees = {
        line.split("/", 1)[0]
        for line in tracked.stdout.splitlines()
        if line.endswith(".py") and "/" in line
    }
    assert trees, "no tracked python trees found -- the enumeration is broken, not the repo"

    ci_sh = (REPO_ROOT / "scripts" / "ci.sh").read_text()
    scan_line = next(
        (line for line in ci_sh.splitlines() if "docs/" in line and "lionagi/" in line),
        None,
    )
    assert scan_line is not None, "could not locate the hygiene scan list in ci.sh"

    missing = sorted(tree for tree in trees if f"{tree}/" not in scan_line)
    assert not missing, (
        f"tracked python trees absent from the ci.sh hygiene scan list: {missing}. "
        "They are scanned by nothing, which reads exactly like being clean."
    )


def test_an_allowlisted_fixture_does_not_excuse_an_identifier_it_is_not_listed_for(
    tmp_path: Path,
) -> None:
    """The exemption is per identifier, and this is why.

    A file-level pass excused every reserved identifier in an allowlisted file,
    so a genuine leak written into one was suppressed. That is the least
    visible place for a leak to land: the file is already full of the
    vocabulary, so nothing about it looks out of place on a read.
    """
    repo = _fake_repo(tmp_path)
    target = repo / "tests" / "scripts" / "test_ci_hygiene.py"
    target.parent.mkdir(parents=True)
    # The identifiers it IS allowlisted for, beside one it is not. Both are
    # joined at runtime the way RESERVED is, so the written fixture carries
    # each as a single literal while this file carries neither. All three
    # permitted names are present so the liveness check has nothing to say and
    # the exit code reports only the unlisted identifier.
    unlisted = "lambda:" + "not-a-listed-fixture-name"
    target.write_text(f'{_fixture_carrying(*PERMITTED_NAMES)}LEAK = "{unlisted}"\n')

    result = _scan(repo / "tests")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not allowed to carry" in result.stdout
    assert "not-a-listed-fixture-name" in result.stdout
    # The permitted one must not be named as an offender.
    assert "sample-unit" not in result.stdout.split("carry:", 1)[-1]


def test_a_reserved_identifier_nested_in_an_fstring_format_spec_is_reported(
    tmp_path: Path,
) -> None:
    """Format specs are a nested JoinedStr, not part of the top-level values.

    On interpreters below 3.12 the tokenizer hands the whole f-string over as
    one token and the literal segments are recovered by parsing it. Reading
    only the top-level values left anything inside a format spec unscanned, and
    3.10 is the floor this project supports and runs in CI.
    """
    repo = _fake_repo(tmp_path)
    target = repo / "lionagi" / "render.py"
    target.parent.mkdir(parents=True)
    target.write_text(f'value = 1\nrendered = f"{{value:{RESERVED}}}"\n')

    result = _scan(repo / "lionagi")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "internal namespace identifier found" in result.stdout


def test_an_ordinary_fstring_still_reports_and_closure_syntax_still_does_not(
    tmp_path: Path,
) -> None:
    """The control for the walk: widening what is inspected must not start
    inspecting code, and must not stop inspecting the ordinary case."""
    repo = _fake_repo(tmp_path)

    ordinary = repo / "lionagi" / "ordinary.py"
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text(f'name = "x"\nmsg = f"routing {{name}} to {RESERVED}"\n')
    assert _scan(repo / "lionagi").returncode == 1

    # Concatenated at runtime, the same way RESERVED and the closure fixture
    # above are. Written as one literal, the keyword and the name that follows
    # it would sit in a single string token here, and the scan reports that --
    # comments included, which is why this note spells neither. The only other
    # cure is an allowlist entry, and a hole is worth more than this costs.
    closure = "rows = []\nkey = lambda:" + "rows\nsorted(rows, key=lambda:" + "rows)\n"
    ordinary.write_text(closure)
    result = _scan(repo / "lionagi")
    assert result.returncode == 0, result.stdout + result.stderr

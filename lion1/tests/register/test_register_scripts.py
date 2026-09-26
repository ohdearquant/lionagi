"""The register scripts on synthetic records: what adr_lint refuses and what adr_fmt leaves alone.

Each case builds a two-record corpus in a temporary directory beside a copy of the scripts, so the
checked-in records are never touched, and runs the script as the pre-commit hook does: as a file,
in a subprocess, from the corpus root.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "docs" / "adr" / "_scripts"
_spec = importlib.util.spec_from_file_location("_md", SCRIPTS / "_md.py")
assert _spec is not None and _spec.loader is not None
_md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_md)
CONTROL = Path("docs/adr/901-control/ADR-0901-the-control.md")
TARGET = Path("docs/adr/902-target/ADR-0902-the-target.md")
CLAIM = """### C1: Probe contract _(enforced: mechanical)_ ^c1

- **Subject**: every probe.
- **Violated when**: a required field is missing.

"""
OWED = CLAIM.replace("enforced: mechanical", "enforced: mechanical; owed, S2")
DECISION = """### D1: Validate the probe ^d1

Serves C1. The checker validates each field.

- **Landing evidence**: synthetic control only.

"""
ROW = "- **S1**: Validation costs time.\n"
BAD_LINK = "[[ADR-0902-the-target#^c99|ADR-0902/C99]]"
GOOD_LINK = "[[ADR-0902-the-target#^c1|ADR-0902/C1]]"
C2_LINK = "[[ADR-0902-the-target#^c2|ADR-0902/C2]]"
ITEM_C2 = "### C2: Example _(enforced: process)_ ^c2\n\n"
AREA = {901: "control", 902: "target"}  # `area` is the record folder's suffix


def record(
    number: int,
    *,
    context: str = "",
    claim: str = CLAIM,
    decision: str = DECISION,
    consequences: str = ROW,
    dependency: str | None = None,
    extra_claim: str = "",
) -> str:
    deps = "depends_on: []" if dependency is None else f'depends_on:\n  - "{dependency}"'
    return f"""---
adr: ADR-{number:04d}
status: draft
liveness: operating
date: "2026-09-26"
area: {AREA[number]}
kind: new
{deps}
tags: []
---

# ADR-{number:04d}: Synthetic probe

## Context

{context}The probe is local. Source: synthetic fixture.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | Input is text. | fixture | reject it |

## Claims

{claim}{extra_claim}## Decisions

{decision}## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Skip validation | missing fields would be accepted |

## Consequences

{consequences}"""


def corpus(root: Path, control: str, target: str = record(902)) -> None:
    scripts = root / "docs" / "adr" / "_scripts"
    scripts.mkdir(parents=True)
    for name in ("_md.py", "adr_fmt.py", "adr_lint.py", "gen_adr_index.py"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    for path, text in ((CONTROL, control), (TARGET, target)):
        (root / path).parent.mkdir(parents=True)
        (root / path).write_text(text)


def run(root: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(Path("docs/adr/_scripts") / script), *args]
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=60, check=False)


FENCED_HEADINGS = "```text\n## Decisions\n### C77: Example _(enforced: process)_ ^c77\n```\n\n"

SEPARATORS = {  # what str.splitlines breaks a line at, besides a line feed and a carriage return
    "line separator": "\u2028",
    "paragraph separator": "\u2029",
    "next line": "\x85",
    "form feed": "\x0c",
    "vertical tab": "\x0b",
    "file separator": "\x1c",
    "group separator": "\x1d",
    "record separator": "\x1e",
}


def split_value(sep: str) -> str:
    """The control record with `sep` inside its liveness value, followed by what a splitlines reader
    takes for a second key."""
    return record(901).replace("liveness: operating\n", f"liveness: operating{sep}status: withdrawn\n", 1)


LINT = {
    "a valid record": (record(901), record(902), 0, ""),
    "a claim under Decisions": (
        record(901, claim="", decision=CLAIM + DECISION),
        record(902),
        1,
        "belongs in Claims",
    ),
    "a decision under Claims": (
        record(901, claim=CLAIM + DECISION, decision=""),
        record(902),
        1,
        "belongs in Decisions",
    ),
    "headings inside a fence are text": (record(901, context=FENCED_HEADINGS), record(902), 0, ""),
    "an owed row that is absent": (record(901, claim=OWED), record(902), 1, "owed row S2"),
    "an owed row only inside a fence in Context": (
        record(901, claim=OWED, context="```text\n- **S2**: Example only.\n```\n\n"),
        record(902),
        1,
        "owed row S2",
    ),
    "an owed row outside Consequences": (
        record(901, claim=OWED, context="- **S2**: This is not a consequence.\n\n"),
        record(902),
        1,
        "owed row S2",
    ),
    "an owed row in Consequences": (
        record(901, claim=OWED, consequences=ROW + "- **S2**: The mechanism is owed.\n"),
        record(902),
        0,
        "",
    ),
    "an owed row only inside a tilde fence": (
        record(901, claim=OWED, consequences="~~~text\n- **S2**: Example only.\n~~~\n\n" + ROW),
        record(902),
        1,
        "owed row S2",
    ),
    "a longer fence holding a shorter one, then the real row": (
        record(
            901,
            claim=OWED,
            consequences="````text\n```\n- **S2**: Example only.\n````\n\n" + ROW + "- **S2**: A real row.\n",
        ),
        record(902),
        0,
        "",
    ),
    "a frontmatter link to no item": (record(901, dependency=BAD_LINK), record(902), 1, "names no item"),
    "a frontmatter link to an item": (record(901, dependency=GOOD_LINK), record(902), 0, ""),
    "a frontmatter link to no record": (
        record(901, dependency="[[ADR-0999-missing|ADR-0999]]"),
        record(902),
        1,
        "unknown record",
    ),
    "a body link to an anchor inside a fence": (
        record(901, extra_claim=f"See {BAD_LINK}.\n\n"),
        record(902, context="```text\n### Example ^c99\n```\n\n"),
        1,
        "names no item",
    ),
    "a body link to an anchor on a heading that is not an item": (
        record(901, extra_claim=f"See {BAD_LINK}.\n\n"),
        record(902, context="### Example ^c99\n\n"),
        1,
        "names no item",
    ),
    "a body link to an item": (record(901, extra_claim=f"See {GOOD_LINK}.\n\n"), record(902), 0, ""),
    "a body link whose anchor appears only as a substring": (
        record(901, extra_claim=f"See {BAD_LINK}.\n\n"),
        record(902, context="Literal ^c99suffix.\n\n"),
        1,
        "names no item",
    ),
    "a body link to a well-formed item inside a fence": (
        record(901, extra_claim=f"See {C2_LINK}.\n\n"),
        record(902, extra_claim="```text\n" + ITEM_C2 + "```\n\n"),
        1,
        "names no item",
    ),
    "a line feed starts an item": (
        record(901, extra_claim=f"See {C2_LINK}.\n\n"),
        record(902, extra_claim="Narrative.\n" + ITEM_C2),
        0,
        "",
    ),
    "tildes inside a line are text": (record(901, context="Narrative. ~~~\n\n"), record(902), 0, ""),
}
# each character str.splitlines breaks a line at and the record scripts do not: for them a line ends at
# a line feed, so text after one of these is still the same heading, item, row, fence line or value
LINT |= {
    f"a {name} inside a line does not start an item": (
        record(901, extra_claim=f"See {C2_LINK}.\n\n"),
        record(902, extra_claim=f"Narrative.{sep}" + ITEM_C2),
        1,
        "names no item",
    )
    for name, sep in SEPARATORS.items()
}
LINT |= {
    f"a {name} inside a line does not open a fence": (
        record(901, context=f"Narrative.{sep}~~~\n\n"),
        record(902),
        0,
        "",
    )
    for name, sep in SEPARATORS.items()
}
LINT |= {
    f"a {name} inside a frontmatter value is part of the value": (
        split_value(sep),
        record(902),
        1,
        "liveness",
    )
    for name, sep in SEPARATORS.items()
}


@pytest.mark.parametrize("case", LINT, ids=LINT)
def test_adr_lint_reads_a_record_as_written(tmp_path: Path, case: str) -> None:
    control, target, exit_code, diagnostic = LINT[case]
    corpus(tmp_path, control, target)
    done = run(tmp_path, "adr_lint.py")
    assert done.returncode == exit_code, done.stdout + done.stderr
    assert diagnostic in done.stdout + done.stderr


CODE = "x = 1\nprint(x)\n"
FMT = {
    "prose is rewrapped": ("one\ntwo\n", "one two\n"),
    "a backtick fence": (f"```python\n{CODE}```\n", None),
    "a tilde fence": (f"~~~python\n{CODE}~~~\n", None),
    "a fence indented one space": (" ```python\n x = 1\n print(x)\n ```\n", None),
    "a fence indented two spaces": ("  ```python\n  x = 1\n  print(x)\n  ```\n", None),
    "a fence indented three spaces": ("   ```python\n   x = 1\n   print(x)\n   ```\n", None),
    "a shorter delimiter inside a longer fence": ("````text\n```\nalpha\nbeta\n````\n", None),
    "a closer with trailing text does not close": ("```text\n```not-a-close\nalpha\nbeta\n```\n", None),
    "a long info string": ("```" + "language " * 30 + f"\n{CODE}```\n", None),
    "a longer closer": (f"```python\n{CODE}`````\n", None),
    "a long fence with nothing inside it": (f"````python\n{CODE}````\n", None),
    "an unclosed tilde fence runs to the end": (f"~~~python\n{CODE}", None),
    "an unclosed backtick fence runs to the end": (f"```python\n{CODE}", None),
    "the other delimiter inside a fence is text": ("```text\n~~~\nalpha\nbeta\n```\n", None),
}


@pytest.mark.parametrize("case", FMT, ids=FMT)
def test_adr_fmt_leaves_fenced_code_alone_and_is_idempotent(tmp_path: Path, case: str) -> None:
    text, expected = FMT[case]
    corpus(tmp_path, text)
    first = run(tmp_path, "adr_fmt.py", str(CONTROL))
    assert first.returncode == 0, first.stdout + first.stderr
    assert (tmp_path / CONTROL).read_text() == (text if expected is None else expected)
    again = run(tmp_path, "adr_fmt.py", str(CONTROL))
    assert again.returncode == 0 and (tmp_path / CONTROL).read_text() == (
        text if expected is None else expected
    )


@pytest.mark.parametrize("name", SEPARATORS, ids=SEPARATORS)
def test_gen_adr_index_reads_the_frontmatter_the_linter_reads(tmp_path: Path, name: str) -> None:
    sep = SEPARATORS[name]
    corpus(tmp_path, split_value(sep))
    done = run(tmp_path, "gen_adr_index.py")
    assert done.returncode == 0, done.stdout + done.stderr
    index = (tmp_path / "docs" / "adr" / "INDEX.md").read_text()
    link = "[ADR-0901](901-control/ADR-0901-the-control.md)"
    assert f"| {link} | 901-control | draft | operating{sep}status: withdrawn | none |" in index


@pytest.mark.parametrize("name", SEPARATORS, ids=SEPARATORS)
def test_a_line_ends_at_a_line_feed_only(name: str) -> None:
    sep = SEPARATORS[name]
    text = f"one{sep}two\r\nthree\n"
    assert _md.physical_lines(text) == [f"one{sep}two", "three"]
    assert _md.mask_fenced_code(text) == text
    assert text.splitlines() == ["one", "two", "three"]  # the splitter these scripts no longer use


def test_physical_lines_at_the_edges() -> None:
    assert _md.physical_lines("") == []
    assert _md.physical_lines("a") == ["a"]
    assert _md.physical_lines("a\n") == ["a"]
    assert _md.physical_lines("a\n\n") == ["a", ""]
    assert _md.physical_lines("a\r\nb") == ["a", "b"]
    assert _md.mask_fenced_code("a\n```\nb\n```\nc") == "a\n\n\n\nc"  # fence lines are masked too

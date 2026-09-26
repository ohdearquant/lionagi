"""The register scripts on synthetic records: what adr_lint refuses and what adr_fmt leaves alone.

Each case builds a two-record corpus in a temporary directory beside a copy of the scripts, so the
checked-in records are never touched, and runs the script as the pre-commit hook does: as a file,
in a subprocess, from the corpus root.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "docs" / "adr" / "_scripts"
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
    for name in ("_md.py", "adr_fmt.py", "adr_lint.py"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    for path, text in ((CONTROL, control), (TARGET, target)):
        (root / path).parent.mkdir(parents=True)
        (root / path).write_text(text)


def run(root: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(Path("docs/adr/_scripts") / script), *args]
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=60, check=False)


FENCED_HEADINGS = "```text\n## Decisions\n### C77: Example _(enforced: process)_ ^c77\n```\n\n"

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

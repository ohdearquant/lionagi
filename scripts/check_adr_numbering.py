"""Check that ADR numbers in docs/adr/ are unique and match their headings.

Two ADRs can claim the same number through a clean merge: each branch adds its
own file, git sees no textual conflict, and the merged tree carries both. The
collision is only visible by listing the directory at the merge result, so this
check must run in CI (which checks out the PR merge commit), not just locally
on a branch.

Three properties are asserted over ``docs/adr/ADR-*.md``:

- every filename matches ``ADR-NNNN-<slug>.md`` (four-digit number);
- no two files share a number — a failure names both filenames, since the fix
  is renumbering one of them and the reviewer needs to know which two collided;
- the ``# ADR-NNNN:`` title heading carries the same number as the filename,
  which is the same drift class and equally invisible in a diff.

Usage: ``uv run scripts/check_adr_numbering.py``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "adr"

_FILENAME_RE = re.compile(r"^ADR-(\d{4})-[a-z0-9][a-z0-9-]*\.md$")
_HEADING_RE = re.compile(r"^# ADR-(\d{4}):", re.M)


def check_dir(adr_dir: Path) -> list[str]:
    """Return one error string per numbering defect in *adr_dir* (empty = clean)."""
    errors: list[str] = []
    by_number: dict[str, list[str]] = {}
    paths = sorted(adr_dir.glob("ADR-*.md"))
    if not paths:
        return [f"{adr_dir}: no ADR-*.md files found — wrong directory or empty checkout"]
    for path in paths:
        match = _FILENAME_RE.match(path.name)
        if match is None:
            errors.append(
                f"{path.name}: filename does not match ADR-NNNN-<slug>.md "
                "(four-digit number, lowercase kebab-case slug)"
            )
            continue
        number = match.group(1)
        by_number.setdefault(number, []).append(path.name)
        heading = _HEADING_RE.search(path.read_text(encoding="utf-8"))
        if heading is None:
            errors.append(f"{path.name}: no '# ADR-NNNN:' title heading found")
        elif heading.group(1) != number:
            errors.append(
                f"{path.name}: heading says ADR-{heading.group(1)} "
                f"but the filename says ADR-{number}"
            )
    for number, names in sorted(by_number.items()):
        if len(names) > 1:
            errors.append(
                f"ADR-{number} is claimed by {len(names)} files: {', '.join(names)} "
                "— renumber all but one to the next free number"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("adr_dir", nargs="?", type=Path, default=DEFAULT_ADR_DIR)
    args = parser.parse_args(argv)
    errors = check_dir(args.adr_dir)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"{len(errors)} ADR numbering error(s)", file=sys.stderr)
        return 1
    print(f"ADR numbering OK ({args.adr_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

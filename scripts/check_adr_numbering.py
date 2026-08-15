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
- the first line is ``# ADR-NNNN: Human Title`` carrying the filename's number,
  which is the same drift class and equally invisible in a diff.

The title is read from the first physical line only, per the ADR style standard
(docs/governance/standards/adr-style.md). Scanning the whole document would let
a matching heading further down — including one inside a code fence — stand in
for a missing or misnumbered title.

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
_HEADING_RE = re.compile(r"^# ADR-(\d{4}):")

# The title line is short by construction, so the first line is read from a
# bounded prefix. This also keeps the check from streaming a file that never
# ends (a character device, say) until the CI job times out.
_MAX_TITLE_BYTES = 4096


def _title_line(path: Path) -> str | None:
    """Return the file's first line, or None if it is unreadable as text."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_MAX_TITLE_BYTES)
    except OSError:
        return None
    line, newline, _ = head.partition(b"\n")
    if not newline and len(head) == _MAX_TITLE_BYTES:
        # No line break within the bounded prefix: whatever this is, it is not
        # a title line, and the rest of the file must not be read to find out.
        return None
    try:
        return line.decode("utf-8").rstrip("\r")
    except UnicodeDecodeError:
        return None


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
        if path.is_symlink():
            errors.append(f"{path.name}: is a symlink; ADR records must be regular files")
            continue
        first_line = _title_line(path)
        heading = _HEADING_RE.match(first_line) if first_line is not None else None
        if heading is None:
            errors.append(
                f"{path.name}: first line is not a '# ADR-NNNN: Human Title' heading "
                f"(found: {first_line!r})"
            )
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

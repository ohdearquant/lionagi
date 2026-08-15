"""Check that ADR numbers in docs/adr/ are unique and match their headings.

Two ADRs can claim the same number through a clean merge: each branch adds its
own file, git sees no textual conflict, and the merged tree carries both. The
collision is only visible by listing the directory at the merge result, so this
check must run in CI (which checks out the PR merge commit), not just locally
on a branch. tests/docs/test_adr_numbering.py is what carries it there: the
docs job runs that suite on every pull request.

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
import codecs
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "adr"

_FILENAME_RE = re.compile(r"^ADR-(\d{4})-[a-z0-9][a-z0-9-]*\.md$")
_HEADING_RE = re.compile(r"^# ADR-(\d{4}):")

# The first line is read from a bounded prefix, which keeps the check from
# streaming a file that never ends (a character device, say) until the CI job
# times out. The bound is not a title-length limit: a first line longer than
# this comes back truncated, and the heading pattern is decided by the line's
# opening characters, so a long title still passes.
_MAX_TITLE_BYTES = 4096

# Long enough to show the defect, short enough to keep a truncated first line
# from filling the CI log.
_MAX_SHOWN_CHARS = 80


def _read_title_line(path: Path) -> tuple[str | None, str | None]:
    """Return ``(first line, reason it could not be read)``; exactly one is None.

    The line may be truncated at ``_MAX_TITLE_BYTES``. Callers must only use it
    to match a prefix pattern, never to validate the line's full content.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(_MAX_TITLE_BYTES)
    except OSError as exc:
        return None, f"unreadable: {exc.strerror or exc}"
    line, newline, _ = head.partition(b"\n")
    # A truncated read can split a multi-byte character, which is not the same
    # defect as a file that is genuinely not UTF-8. Decoding non-final leaves
    # an incomplete trailing sequence buffered instead of raising on it.
    complete = bool(newline) or len(head) < _MAX_TITLE_BYTES
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(line, final=complete)
    except UnicodeDecodeError:
        return None, "first line is not valid UTF-8"
    return text.rstrip("\r"), None


def _show(line: str) -> str:
    """Render a first line for an error message, capped so a long one stays readable."""
    if len(line) > _MAX_SHOWN_CHARS:
        return f"{line[:_MAX_SHOWN_CHARS]!r}... ({len(line)} chars)"
    return repr(line)


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
        first_line, read_error = _read_title_line(path)
        heading = _HEADING_RE.match(first_line) if first_line is not None else None
        if heading is None:
            found = read_error if first_line is None else f"found: {_show(first_line)}"
            errors.append(
                f"{path.name}: first line is not a '# ADR-NNNN: Human Title' heading ({found})"
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

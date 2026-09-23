"""Rewrap the prose of an ADR corpus at 100 columns; everything else is left byte for byte.

Rewrapped: paragraphs, and bullets (`- ...`) with their two-space continuation lines. Untouched:
frontmatter, headings, tables, fenced code, blank lines, blockquotes. Idempotent.

    uv run python docs/adr/_scripts/adr_fmt.py [files...]     # no files: the whole corpus
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # docs/adr
WIDTH = 100


def wrap(lines: list[str]) -> list[str]:
    text = " ".join(line.strip() for line in lines)
    if text.startswith("- "):
        return textwrap.wrap(
            text,
            WIDTH,
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    return textwrap.wrap(text, WIDTH, break_long_words=False, break_on_hyphens=False)


def fmt(text: str) -> str:
    out: list[str] = []
    lines = text.split("\n")
    i = 0
    # frontmatter
    if lines and lines[0] == "---":
        j = lines.index("---", 1)
        out.extend(lines[: j + 1])
        i = j + 1
    unit: list[str] = []
    in_code = False

    def flush() -> None:
        if unit:
            out.extend(wrap(unit))
            unit.clear()

    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("```"):
            flush()
            in_code = not in_code
            out.append(line)
            continue
        if in_code or not line.strip() or line.startswith(("#", "|", ">")):
            flush()
            out.append(line)
            continue
        if line.startswith("- "):
            flush()
            unit.append(line)
            continue
        if line.startswith("  ") and unit and unit[0].startswith("- "):
            unit.append(line)
            continue
        if unit and unit[0].startswith("- "):
            flush()
        unit.append(line)
    flush()
    return "\n".join(out)


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or sorted(ROOT.glob("*/ADR-*.md"))
    changed = 0
    for p in files:
        before = p.read_text()
        after = fmt(before)
        if after != before:
            p.write_text(after)
            changed += 1
            print(f"rewrapped {p.relative_to(ROOT.parent.parent)}")
    print(f"adr_fmt: {changed} file(s) changed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

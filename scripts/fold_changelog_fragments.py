"""Fold per-pull-request changelog fragments into the Unreleased block of CHANGELOG.md.

Each pull request adds ``changelog.d/<pull request number>.md`` instead of editing
CHANGELOG.md. A fragment holds Keep a Changelog headings (``### Added``, ``### Changed``,
``### Deprecated``, ``### Removed``, ``### Fixed``, ``### Security``), each followed by
``- `` bullets; a bullet may continue on indented lines.

Run from the repository root: ``python scripts/fold_changelog_fragments.py``. The bullets are
appended under the matching heading of the ``## [Unreleased]`` block, in ascending pull request
number order, a missing heading is created in the order above, and the folded fragments are
deleted. Every fragment is validated before anything is written, so a refusal leaves
CHANGELOG.md and every fragment untouched.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

HEADINGS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
README = "README.md"

_FRAGMENT_NAME = re.compile(r"[0-9]+\.md")
_UNRELEASED = "## [Unreleased]"


class FoldRefusedError(Exception):
    """A fragment or CHANGELOG.md the fold will not touch; the message names the file."""


@dataclass
class Fragment:
    """One parsed fragment: its pull request number and its bullets in file order."""

    path: Path
    number: int
    bullets: list[tuple[str, list[str]]]  # (heading, the bullet's lines)


@dataclass
class _Chunk:
    """The lines before the first heading (``line`` is None), or one ``### `` section."""

    line: str | None
    name: str | None
    body: list[str]  # lines below the heading, without the trailing blank lines
    tail: list[str]  # the blank lines that followed the body


def _read(path: Path, label: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FoldRefusedError(f"{label}: cannot be read ({type(exc).__name__})") from exc


def _parse_fragment(path: Path) -> Fragment:
    label = f"{FRAGMENT_DIR}/{path.name}"
    bullets: list[tuple[str, list[str]]] = []
    heading: str | None = None
    has_bullet = False
    current: list[str] | None = None  # the bullet an indented line may still continue
    for raw in _read(path, label).splitlines():
        line = raw.rstrip()
        if not line:
            current = None
        elif line.startswith("#"):
            name = line[4:].strip() if line.startswith("### ") else None
            if name not in HEADINGS:
                raise FoldRefusedError(f"{label}: unknown heading {line!r}")
            if heading is not None and not has_bullet:
                raise FoldRefusedError(f"{label}: no bullets under '### {heading}'")
            heading, has_bullet, current = name, False, None
        elif heading is None:
            raise FoldRefusedError(f"{label}: text outside a heading")
        elif line.startswith("- "):
            current = [line]
            bullets.append((heading, current))
            has_bullet = True
        elif line[0] in " \t" and current is not None:
            current.append(line)
        else:
            raise FoldRefusedError(
                f"{label}: text under '### {heading}' that is not part of a bullet"
            )
    if heading is None:
        raise FoldRefusedError(f"{label}: no bullets")
    if not has_bullet:
        raise FoldRefusedError(f"{label}: no bullets under '### {heading}'")
    return Fragment(path, int(path.stem), bullets)


def _discover(root: Path) -> list[Fragment]:
    """Every fragment under ``root``, parsed, in ascending pull request number order."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    fragments = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.name == README:
            continue
        if not (_FRAGMENT_NAME.fullmatch(path.name) and path.is_file()):
            raise FoldRefusedError(
                f"{FRAGMENT_DIR}/{path.name}: not a fragment (expected <number>.md)"
            )
        fragments.append(_parse_fragment(path))
    fragments.sort(key=lambda fragment: (fragment.number, fragment.path.name))
    return fragments


def _split_block(block: list[str]) -> list[_Chunk]:
    chunks = [_Chunk(None, None, [], [])]
    for line in block:
        if line.startswith("### "):
            chunks.append(_Chunk(line, line[4:].strip(), [], []))
        else:
            chunks[-1].body.append(line)
    for chunk in chunks:
        while chunk.body and not chunk.body[-1].strip():
            chunk.tail.insert(0, chunk.body.pop())
    return chunks


def _insert_chunk(chunks: list[_Chunk], name: str) -> _Chunk:
    """Add an empty ``### name`` section before the first present heading that follows it."""
    rank = HEADINGS.index(name)
    position = next(
        (
            i
            for i, chunk in enumerate(chunks)
            if chunk.name in HEADINGS and HEADINGS.index(chunk.name) > rank
        ),
        len(chunks),
    )
    created = _Chunk(f"### {name}", name, [], [""])
    if position == len(chunks):
        # The block's closing blank lines move to the new last section.
        created.tail, chunks[-1].tail = chunks[-1].tail, [""]
    chunks.insert(position, created)
    return created


def _append_bullets(chunk: _Chunk, bullets: list[list[str]]) -> None:
    body = chunk.body or [""]
    for bullet in bullets:
        if body[-1]:
            body.append("")
        body.extend(bullet)
    chunk.body = body


def _fold_text(text: str, merged: dict[str, list[list[str]]]) -> str:
    """CHANGELOG.md's text with ``merged`` bullets added to its Unreleased block."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == _UNRELEASED), None)
    if start is None:
        raise FoldRefusedError(f"{CHANGELOG}: no '{_UNRELEASED}' block")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    chunks = _split_block(lines[start + 1 : end])
    for name in HEADINGS:
        if name in merged:
            chunk = next((c for c in chunks if c.name == name), None)
            _append_bullets(chunk or _insert_chunk(chunks, name), merged[name])
    block: list[str] = []
    for chunk in chunks:
        if chunk.line is not None:
            block.append(chunk.line)
        block += chunk.body + chunk.tail
    folded = "\n".join(lines[: start + 1] + block + lines[end:])
    return folded + "\n" if text.endswith("\n") else folded


def main(root: Path | None = None) -> int:
    """Fold the fragments under ``root`` (the working directory by default); return an exit code."""
    root = Path.cwd() if root is None else Path(root)
    try:
        fragments = _discover(root)
        if not fragments:
            print("No changelog fragments to fold.")
            return 0
        changelog = root / CHANGELOG
        text = _read(changelog, CHANGELOG)
        merged: dict[str, list[list[str]]] = {}
        for fragment in fragments:
            for heading, bullet in fragment.bullets:
                merged.setdefault(heading, []).append(bullet)
        folded = _fold_text(text, merged)
    except FoldRefusedError as refusal:
        print(f"fold_changelog_fragments: {refusal}", file=sys.stderr)
        return 1
    staging = changelog.with_name(CHANGELOG + ".tmp")
    staging.write_bytes(folded.encode("utf-8"))
    os.replace(staging, changelog)
    for fragment in fragments:
        fragment.path.unlink()
    print(f"Folded {len(fragments)} changelog fragment(s) into {CHANGELOG}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
